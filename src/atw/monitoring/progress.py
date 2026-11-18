"""Read-only progress sampling, independent from terminal evidence."""

from __future__ import annotations

import csv
import math
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from atw.errors import UNSAFE_PATH, AtwError
from atw.models.progress import (
    CSV_LIMIT,
    REPORT_LIMIT,
    CsvProgress,
    CsvSource,
    ItemReport,
    ProgressSpec,
    QueueItem,
    QueuePlan,
    QueueProgress,
)
from atw.service.lock import SingleInstanceLock
from atw.utils.atomic import atomic_write_bytes
from atw.utils.pathsafe import validate_read_path
from atw.utils.timeutil import dt_to_z, parse_iso

PROGRESS_KEY = "work_progress_v1"
PROGRESS_ADAPTER = TypeAdapter(ProgressSpec)
SAMPLE_BUDGET = 16 * 1024 * 1024


def source_path(raw: str, roots: list[Path], run_dir: str, *, relative: bool = False) -> Path:
    run = validate_read_path(run_dir, roots, expect_directory=True, require_exists=True)
    path = Path(raw)
    if relative and not path.is_absolute():
        path = run / path
    resolved = validate_read_path(str(path), [run], field="progress.path")
    if resolved == run:
        raise AtwError(UNSAFE_PATH, "进度来源必须是运行目录内的文件")
    return resolved


def read_bounded(path: Path, limit: int) -> bytes:
    # O_NONBLOCK avoids blocking on FIFOs if a path changes between stat and open.
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("来源不是普通文件")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("来源文件类型无效或超过大小限制")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("来源文件超过大小限制")
    return data


def _attempt_path(source: CsvSource, attempt: int) -> str:
    if attempt != 1 and "{attempt}" not in source.path:
        raise ValueError("重试 CSV 需要独立的 {attempt} 路径")
    return source.path.replace("{attempt}", str(attempt))


def normalize_progress(
    spec: ProgressSpec, roots: list[Path], run_dir: str, *, relative: bool = False
) -> ProgressSpec:
    def resolve(path: str) -> str:
        return str(source_path(path, roots, run_dir, relative=relative))

    if isinstance(spec, CsvProgress):
        return spec.model_copy(update={"path": resolve(spec.path)})
    items = []
    reports = set()
    for item in spec.items:
        report = resolve(item.report)
        key = os.path.normcase(report)
        if key in reports:
            raise ValueError("每个子任务必须使用独立的状态文件")
        reports.add(key)
        counter = item.csv
        if counter is not None:
            template = str(Path(run_dir) / counter.path) if relative else counter.path
            resolve(template.replace("{attempt}", "1"))
            counter = counter.model_copy(update={"path": template})
        items.append(item.model_copy(update={"report": report, "csv": counter}))
    return spec.model_copy(update={"items": items})


def load_plan(path: Path, roots: list[Path], run_dir: str) -> tuple[str, QueueProgress]:
    file = source_path(str(path), roots, run_dir, relative=True)
    plan = QueuePlan.model_validate_json(read_bounded(file, 256 * 1024))
    spec = QueueProgress(kind="queue", items=plan.items, stale_after_seconds=plan.stale_after_seconds)
    return plan.task_id, normalize_progress(spec, roots, run_dir, relative=True)


def check_report(report: ItemReport, task_id: str, item: QueueItem) -> None:
    if report.task_id != task_id or report.item_id != item.id:
        raise ValueError("进度状态文件不属于本次运行和子任务")
    if report.state == "skipped" and item.required:
        raise ValueError("必做子任务不能标记为跳过")
    if report.completed is not None and report.completed > item.total:
        raise ValueError("完成量超出计划总量")
    if report.state == "completed" and report.completed not in {None, item.total}:
        raise ValueError("完成状态的计数必须等于计划总量")
    if item.csv is None and report.state in {"running", "failed"} and report.completed is None:
        raise ValueError("该子任务必须提供完成量")


def check_transition(old: ItemReport, new: ItemReport) -> None:
    if new.attempt < old.attempt:
        raise ValueError("旧 attempt 更新已拒绝")
    if new.attempt == old.attempt:
        terminal = {"completed", "failed", "skipped"}
        if (
            (old.state in terminal and new.state != old.state)
            or (old.state == "running" and new.state == "queued")
            or (
                old.completed is not None and new.completed is not None
                and new.completed < old.completed
            )
        ):
            raise ValueError("同一 attempt 内的状态或计数不能回退")


def write_report(
    report: ItemReport, item: QueueItem, roots: list[Path], run_dir: str
) -> None:
    check_report(report, report.task_id, item)
    path = source_path(item.report, roots, run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = source_path(str(path.with_name(path.name + ".lock")), roots, run_dir)
    lock = SingleInstanceLock(lock_path, secure_parent=False)
    if not lock.acquire():
        raise ValueError("子任务进度正在更新，请重试")
    try:
        try:
            previous = ItemReport.model_validate_json(read_bounded(path, REPORT_LIMIT))
        except FileNotFoundError:
            previous = None
        if previous:
            check_report(previous, report.task_id, item)
            check_transition(previous, report)
            if previous.attempt == report.attempt and report.acceptance is None:
                report.acceptance = previous.acceptance
        atomic_write_bytes(
            path, report.model_dump_json(exclude_none=True).encode("utf-8"), secure_parent=False
        )
    finally:
        lock.release()


def _csv_count(
    path: Path, source: CsvSource, total: int, previous: dict, budget: list[int]
) -> tuple[int, list[int], float]:
    info = path.stat()
    signature = [info.st_size, info.st_mtime_ns, info.st_ino]
    if previous.get("csv_signature") == signature and previous.get("completed") is not None:
        return previous["completed"], signature, info.st_mtime
    if info.st_size > CSV_LIMIT:
        raise ValueError("CSV 超过大小限制")
    if info.st_size > budget[0]:
        raise ValueError("本次 CSV 采样读取预算已用完")
    budget[0] -= info.st_size
    raw = read_bounded(path, info.st_size)
    complete = raw[:raw.rfind(b"\n") + 1].decode("utf-8-sig")
    rows = csv.reader(complete.splitlines(), strict=True)
    header = [value.strip() for value in next(rows, [])]
    if header.count(source.column) != 1:
        raise ValueError(f"CSV 缺少唯一计数列 {source.column}")
    index = header.index(source.column)
    last = None
    for row in rows:
        if row:
            last = row
    if last is None or len(last) != len(header):
        raise ValueError("CSV 暂无有效完整数据行")
    count = int(last[index]) + (1 - source.counter_base)
    if not 0 <= count <= total:
        raise ValueError("CSV 完成量超出计划总量")
    return count, signature, info.st_mtime


def sample_progress(
    spec: ProgressSpec,
    *,
    task_id: str,
    run_dir: str,
    roots: list[Path],
    started_at: datetime,
    now: datetime,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    old = {item["id"]: item for item in (previous or {}).get("items", [])}
    entries = (
        [QueueItem(id="task", total=spec.total, unit=spec.unit, report=spec.path, csv=spec)]
        if isinstance(spec, CsvProgress)
        else spec.items
    )
    items = []
    budget = [SAMPLE_BUDGET]
    deferred = (previous or {}).get("deferred")
    offset = next((index for index, item in enumerate(entries) if item.id == deferred), 0)
    next_deferred = None
    for item in entries[offset:] + entries[:offset]:
        prior = old.get(item.id, {})
        current: dict[str, Any] = {
            "id": item.id, "total": item.total, "unit": item.unit,
            "weight": item.weight, "required": item.required,
            "state": None, "completed": None, "source_status": "unknown",
        }
        try:
            report = None
            if isinstance(spec, QueueProgress):
                path = source_path(item.report, roots, run_dir)
                report = ItemReport.model_validate_json(read_bounded(path, REPORT_LIMIT))
                check_report(report, task_id, item)
                if report.updated_at < started_at or (report.updated_at - now).total_seconds() > 300:
                    raise ValueError("状态时间不属于本次运行")
                if report.attempt < (prior.get("attempt") or 0):
                    raise ValueError("状态文件来自旧 attempt")
                if prior.get("attempt") != report.attempt:
                    prior = {}
                current.update(attempt=report.attempt, state=report.state, acceptance=report.acceptance)
            if report and report.state in {"queued", "skipped", "completed"}:
                count = item.total if report.state == "completed" else 0
                source_at = report.updated_at
            elif item.csv is not None and (report is None or report.completed is None):
                attempt = report.attempt if report else 1
                path = source_path(_attempt_path(item.csv, attempt), roots, run_dir)
                if path.stat().st_mtime < started_at.timestamp():
                    raise ValueError("CSV 早于本次运行")
                count, signature, source_epoch = _csv_count(path, item.csv, item.total, prior, budget)
                current["csv_signature"] = signature
                source_at = datetime.fromtimestamp(source_epoch, tz=now.tzinfo)
            else:
                assert report is not None
                count = report.completed
                source_at = report.updated_at
            if prior.get("completed") is not None and count < prior["completed"]:
                raise ValueError("同一 attempt 的计数回退")
            if report is None:
                current["state"] = "running"
            current.update(completed=count, source_at=dt_to_z(source_at), source_status="live")
            if (
                spec.stale_after_seconds and current["state"] == "running"
                and (now - source_at).total_seconds() > spec.stale_after_seconds
            ):
                current.update(source_status="snapshot", reason="来源超过更新时间阈值")
        except (AtwError, OSError, ValueError, csv.Error) as exc:
            if current.get("attempt") is None and prior.get("attempt") is not None:
                current.update(
                    attempt=prior["attempt"], state=prior.get("state"),
                    acceptance=prior.get("acceptance"),
                )
            if prior.get("completed") is not None:
                for key in ("completed", "source_at", "csv_signature", "attempt"):
                    if key in prior:
                        current[key] = prior[key]
                current["source_status"] = "snapshot"
            current["reason"] = (str(exc).splitlines() or [type(exc).__name__])[0][:200]
            if current["reason"] == "本次 CSV 采样读取预算已用完" and next_deferred is None:
                next_deferred = item.id
        items.append(current)
    if offset:
        items = items[-offset:] + items[:-offset]
    complete = all(item["completed"] is not None for item in items)
    percent = None
    if complete:
        # Rescale first so a sum of individually finite weights cannot overflow.
        scale = max(item["weight"] for item in items)
        weights = [item["weight"] / scale for item in items]
        percent = 100 * sum(
            weight * (item["completed"] / item["total"])
            for weight, item in zip(weights, items, strict=True)
        ) / sum(weights)
        if all(item["completed"] == item["total"] for item in items):
            percent = 100.0
        else:
            percent = min(percent, math.nextafter(100.0, 0.0))
    snapshots = [item["source_at"] for item in items if item["source_status"] == "snapshot"]
    return {
        "kind": spec.kind,
        "status": ("snapshot" if snapshots else "live") if complete else "unknown",
        "percent": percent,
        "sampled_at": dt_to_z(now),
        "snapshot_at": min(snapshots, key=parse_iso) if snapshots else None,
        "accepted": sum(item.get("acceptance") == "accepted" for item in items),
        "acceptance_available": any(item.get("acceptance") is not None for item in items),
        "required_accepted": sum(
            item["required"] and item.get("acceptance") == "accepted" for item in items
        ),
        "required_total": sum(item["required"] for item in items),
        "total_items": len(items),
        "deferred": next_deferred,
        "items": items,
    }
