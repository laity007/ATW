"""ATW CLI.  All ``--json`` output is pure JSON on stdout."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
import typer
from typer import Context

from atw import PROTOCOL_PROGRESS, PROTOCOL_TASK, __version__
from atw.config import AtwPaths, Config, load_config, resolve_allowed_roots
from atw.errors import (
    FILE_TOO_LARGE,
    PROCESS_NOT_FOUND,
    RECEIPT_TIMEOUT,
    REQUEST_CONFLICT,
    SCHEMA_VALIDATION_FAILED,
    AtwError,
)
from atw.health.bootid import current_boot_identity
from atw.health.doctor import run_doctor
from atw.intake.receipts import ReceiptStore
from atw.intake.submit import active_protocols, read_request, submit_request
from atw.models.progress import Acceptance, ItemReport, State
from atw.models.task import Receipt, Target, parse_request_json, request_content_hash
from atw.monitoring.process import ProcessMonitor
from atw.monitoring.progress import (
    PROGRESS_ADAPTER,
    PROGRESS_KEY,
    load_plan,
    normalize_progress,
    sample_progress,
    write_report,
)
from atw.notifications.dingtalk import DingTalkNotifier
from atw.notifications.factory import build_notifier
from atw.notifications.templates import format_blocker, render_test_message
from atw.service.lock import SingleInstanceLock
from atw.service.runner import ServiceRunner
from atw.storage.db import Database
from atw.storage.repository import Repository
from atw.utils.atomic import atomic_write_bytes, json_dumps
from atw.utils.pathsafe import validate_read_path
from atw.utils.timeutil import dt_to_z, epoch_to_dt, parse_iso, utc_now

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Agent Task Watcher — 只监督与通知，不控制计算进程。",
)
progress_app = typer.Typer(no_args_is_help=True)
app.add_typer(progress_app, name="progress")


class CliState:
    home: Path | None = None
    config: Path | None = None


STATE = CliState()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, sqlite3.Row):
        return dict(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def _emit_json(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, default=_json_default))


def _load(create_dirs: bool = True) -> tuple[Config, AtwPaths]:
    cfg, paths = load_config(STATE.home, STATE.config, create_dirs=create_dirs)
    return cfg, paths


def _load_or_exit(json_mode: bool, create_dirs: bool = True) -> tuple[Config, AtwPaths]:
    try:
        return _load(create_dirs=create_dirs)
    except AtwError as exc:
        if json_mode:
            _emit_json({"ok": False, "error": exc.as_dict()})
        else:
            typer.echo(f"错误 {exc.code}: {exc.message}", err=True)
        raise typer.Exit(2) from None


def _repo(paths: AtwPaths) -> tuple[Repository, Database]:
    db = Database(paths.db_file)
    db.connect()
    return Repository(db), db


def _service_running(paths: AtwPaths) -> bool:
    lock = SingleInstanceLock(paths.lock_file)
    if lock.acquire():
        lock.release()
        return False
    return True


def _task_summary(record) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "name": record.name,
        "state": record.state,
        "revision": record.revision,
        "project": record.project,
        "accepted_at": record.accepted_at.isoformat() if record.accepted_at else None,
        "finalized_at": record.finalized_at.isoformat() if record.finalized_at else None,
        "finalize_reason": record.finalize_reason,
        "run_started_at": record.run_started_at.isoformat() if record.run_started_at else None,
    }


@app.callback(invoke_without_command=True)
def _main(
    ctx: Context,
    home: Path | None = typer.Option(None, "--home", help="覆盖 ATW_HOME（默认平台目录）"),
    config: Path | None = typer.Option(None, "--config", help="显式配置文件路径"),
    version: bool = typer.Option(False, "--version", help="打印版本并退出"),
) -> None:
    STATE.home = home
    STATE.config = config
    if version:
        typer.echo(f"atw {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


@app.command("status")
def status(json_output: bool = typer.Option(False, "--json")) -> None:
    """服务、任务与通知队列概览。"""
    cfg, paths = _load_or_exit(json_output)
    repo, db = _repo(paths)
    try:
        counts = repo.counts()
        running = _service_running(paths)
        boot = current_boot_identity()
        last = repo.last_session()
        session = (
            {
                "session_id": last["session_id"],
                "started_at": last["started_at"],
                "ended_at": last["ended_at"],
                "clean_shutdown": bool(last["clean_shutdown"]),
                "boot_id": last["boot_id"],
                "boot_confidence": last["boot_confidence"],
            }
            if last
            else None
        )
        from atw.monitoring.gpu import GpuMonitor

        data = {
            "protocol": "atw.status.v1",
            "atw_version": __version__,
            "generated_at": utc_now().isoformat(),
            "instance_id": cfg.instance_id,
            "node_label": cfg.node_label,
            "service_running": running,
            "boot": {
                "boot_id": boot.boot_id,
                "confidence": boot.confidence,
                "detail": boot.detail,
            },
            "last_session": session,
            "active_protocols": active_protocols(paths, repo),
            "counts": counts,
            "notification": {
                "provider": cfg.notification.provider,
                "rate_limit_per_minute": cfg.notification.rate_limit_per_minute,
            },
            "gpu": GpuMonitor().capability(),
            "paths": {
                "home": str(paths.root),
                "inbox": str(paths.inbox),
                "db": str(paths.db_file),
                "log": str(paths.log_file),
            },
        }
        if json_output:
            _emit_json(data)
            return
        typer.echo(f"ATW {__version__}  instance={cfg.instance_id}")
        typer.echo(f"服务运行: {'是' if running else '否'}")
        typer.echo("任务: 总数={tasks} 监控中={monitoring} 状态={states}".format(**counts))
        typer.echo(f"通知队列: {counts['outbox']}")
        typer.echo(f"GPU: {'可用' if data['gpu'].get('available') else data['gpu'].get('reason')}")
        typer.echo(f"ATW_HOME: {paths.root}")
    except Exception as exc:
        if json_output:
            _emit_json({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(exc)}})
        else:
            typer.echo(f"错误: {exc}", err=True)
        raise typer.Exit(2) from None
    finally:
        db.close()


@app.command("list")
def list_tasks(
    state_filter: str | None = typer.Option(
        None, "--state", help="MONITORING/SUCCESS/FAILED/UNKNOWN/INTERRUPTED"
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """列出任务（只读）。"""
    cfg, paths = _load_or_exit(json_output)
    valid = {"MONITORING", "SUCCESS", "FAILED", "UNKNOWN", "INTERRUPTED"}
    if state_filter and state_filter.upper() not in valid:
        if json_output:
            _emit_json(
                {
                    "ok": False,
                    "error": {"code": "INVALID_ARGUMENT", "message": f"未知状态 {state_filter}"},
                }
            )
        else:
            typer.echo(f"未知状态: {state_filter}", err=True)
        raise typer.Exit(2)
    repo, db = _repo(paths)
    try:
        records = repo.list_tasks(state_filter.upper() if state_filter else None)
        data = [_task_summary(record) for record in records]
        if json_output:
            _emit_json({"protocol": "atw.tasks.v1", "tasks": data, "count": len(data)})
            return
        if not data:
            typer.echo("无任务")
            return
        for item in data:
            typer.echo(
                f"{item['state']:<11} {item['task_id']:<28} rev={item['revision']} {item['name']}"
            )
    finally:
        db.close()


@app.command("show")
def show(
    task_id: str = typer.Argument(...), json_output: bool = typer.Option(False, "--json")
) -> None:
    """显示单个任务详情、版本、事件与进程。"""
    cfg, paths = _load_or_exit(json_output)
    repo, db = _repo(paths)
    try:
        record = repo.get_task(task_id)
        if record is None:
            message = {"code": "TASK_NOT_FOUND", "message": f"任务不存在: {task_id}"}
            if json_output:
                _emit_json({"ok": False, "error": message})
            else:
                typer.echo(message["message"], err=True)
            raise typer.Exit(1)
        revisions = [
            {
                "revision": row["revision"],
                "request_id": row["request_id"],
                "operation": row["operation"],
                "changes": json.loads(row["changes_json"]) if row["changes_json"] else None,
                "created_at": row["created_at"],
            }
            for row in repo.revisions(task_id)
        ]
        processes = [dict(row) for row in repo.list_processes(task_id)]
        observations = [dict(row) for row in repo.observations(task_id, limit=10)]
        events = [
            {
                "event_type": row["event_type"],
                "event_instance": row["event_instance"],
                "ts": row["ts"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in repo.recent_events(task_id, limit=20)
        ]
        data = {
            "task": record.snapshot(),
            "finalize_diagnostics": record.runtime.get("finalize_diagnostics"),
            "progress": record.runtime.get(PROGRESS_KEY),
            "revisions": revisions,
            "processes": processes,
            "observations": observations,
            "events": events,
        }
        if json_output:
            _emit_json(data)
            return
        typer.echo(f"任务: {record.task_id}  {record.name}")
        typer.echo(f"状态: {record.state}  revision={record.revision}")
        work = data["progress"]
        if work and work.get("sample"):
            sample = work["sample"]
            value = sample["percent"]
            typer.echo(f"执行进度: {f'{value:.1f}%' if value is not None else '未知'} ({sample['status']})")
        typer.echo(f"项目: {record.project or '-'}")
        typer.echo(
            f"运行开始: {record.run_started_at.isoformat() if record.run_started_at else '-'}"
        )
        typer.echo(f"受理: {record.accepted_at.isoformat() if record.accepted_at else '-'}")
        typer.echo(
            f"终态: {record.state if record.terminal else '-'}  {record.finalize_reason or ''}"
        )
        if record.state == "UNKNOWN":
            diagnostics = record.runtime.get("finalize_diagnostics")
            if diagnostics:
                for check in diagnostics["blockers"]:
                    typer.echo(f"  {format_blocker(check)}")
            elif record.finalize_evidence:
                typer.echo(f"判断证据: {record.finalize_evidence}")
        if record.evidence:
            typer.echo(f"运行目录: {record.evidence.run_directory}")
            if record.evidence.log:
                typer.echo(f"日志: {record.evidence.log.path}")
            for path in record.evidence.result_files:
                typer.echo(f"结果: {path}")
        typer.echo("进程:")
        for proc in processes:
            typer.echo(
                f"  pid={proc['pid']} create={proc['create_time_epoch']} role={proc['role']} "
                f"alive={bool(proc['alive'])}"
            )
        typer.echo("版本:")
        for rev in revisions:
            typer.echo(f"  rev={rev['revision']} {rev['operation']} {rev['request_id']}")
        if events:
            typer.echo("最近事件:")
            for event in events[:10]:
                typer.echo(f"  {event['ts']} {event['event_type']} {event['event_instance']}")
    finally:
        db.close()


@app.command("history")
def history(
    limit: int = typer.Option(20, "--limit", min=1, max=500),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """历史终态任务。"""
    cfg, paths = _load_or_exit(json_output)
    repo, db = _repo(paths)
    try:
        records = repo.history(limit)
        data = [_task_summary(record) for record in records]
        if json_output:
            _emit_json({"protocol": "atw.history.v1", "tasks": data, "count": len(data)})
            return
        if not data:
            typer.echo("无历史任务")
            return
        for item in data:
            typer.echo(
                f"{item['finalized_at'] or '-'}  {item['state']:<11} "
                f"{item['task_id']:<28} {item['name']}"
            )
    finally:
        db.close()


@app.command("validate")
def validate(
    file: Path = typer.Argument(..., exists=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """离线校验 AFHP/1 JSON（不登记、不访问进程）。"""
    cfg, paths = _load_or_exit(json_output, create_dirs=False)
    try:
        raw = file.read_bytes()
    except OSError as exc:
        if json_output:
            _emit_json({"valid": False, "error": {"code": "READ_FAILED", "message": str(exc)}})
        else:
            typer.echo(f"读取失败: {exc}", err=True)
        raise typer.Exit(1) from None
    try:
        model, data = parse_request_json(raw)
    except AtwError as exc:
        if json_output:
            _emit_json({"valid": False, "error": exc.as_dict()})
        else:
            typer.echo(f"INVALID: {exc.code}: {exc.message}", err=True)
        raise typer.Exit(1) from None
    roots = resolve_allowed_roots(cfg)
    path_errors: list[str] = []
    source = model if model.operation == "register" else model.changes
    evidence = source.evidence
    if evidence is not None:
        for field_name, value, is_dir in (
            ("run_directory", evidence.run_directory, True),
            ("log.path", evidence.log.path if evidence.log else None, False),
        ):
            if not value:
                continue
            try:
                validate_read_path(value, roots, field=field_name, expect_directory=is_dir)
            except AtwError as exc:
                path_errors.append(f"{field_name}: {exc.message}")
        for index, value in enumerate(evidence.result_files):
            try:
                validate_read_path(value, roots, field=f"result_files[{index}]")
            except AtwError as exc:
                path_errors.append(f"result_files[{index}]: {exc.message}")
        if getattr(source, "progress", None) is not None:
            try:
                normalize_progress(source.progress, roots, evidence.run_directory)
            except (AtwError, ValueError) as exc:
                path_errors.append(str(exc))
    result = {
        "valid": True,
        "operation": model.operation,
        "request_id": model.request_id,
        "task_id": model.task_id,
        "revision": model.revision,
        "protocol": model.protocol,
        "path_warnings": path_errors,
    }
    if json_output:
        _emit_json(result)
        return
    typer.echo(
        f"VALID {result['operation']} request={result['request_id']} task={result['task_id']} "
        f"revision={result['revision']}"
    )
    for warning in path_errors:
        typer.echo(f"路径警告: {warning}")


@app.command("receipt")
def receipt(
    request_id: str = typer.Argument(...),
    wait: int = typer.Option(0, "--wait", min=0, max=3600),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查询回执；--wait 秒内轮询。rejected 退出码 1，超时退出码 2。"""
    cfg, paths = _load_or_exit(json_output)
    store = ReceiptStore(paths.receipts)
    data = store.wait(request_id, wait) if wait > 0 else store.read(request_id)
    try:
        repo, db = _repo(paths)
        try:
            row = repo.get_request(request_id)
            if row is not None:
                canonical = Receipt.model_validate_json(row["receipt_json"])
                if data != canonical.to_json_dict():
                    data = canonical.to_json_dict()
                    store.write(canonical)
        finally:
            db.close()
    except (sqlite3.Error, ValueError, OSError):
        pass
    if data is None:
        payload = {
            "protocol": "atw.receipt.v1",
            "request_id": request_id,
            "status": "timeout",
            "monitoring": False,
            "error": {
                "code": RECEIPT_TIMEOUT,
                "message": f"{wait} 秒内未收到回执" if wait else "回执不存在",
            },
        }
        if json_output:
            _emit_json(payload)
            typer.echo("", err=True)
        else:
            typer.echo(f"RECEIPT_TIMEOUT: {payload['error']['message']}", err=True)
        raise typer.Exit(2)
    if json_output:
        _emit_json(data)
    else:
        typer.echo(
            f"{data.get('status')} request={data.get('request_id')} "
            f"task={data.get('task_id')} monitoring={data.get('monitoring')}"
        )
        bound = data.get("bound_process") or {}
        if bound:
            typer.echo(f"PID={bound.get('pid')} create_time={bound.get('create_time_epoch')}")
        if data.get("error"):
            typer.echo(
                f"错误: {data['error'].get('code')}: {data['error'].get('message')}", err=True
            )
    if data.get("status") != "accepted":
        raise typer.Exit(1)


def _submission_error(exc: Exception, json_output: bool, request_id: str | None = None) -> None:
    error = exc if isinstance(exc, AtwError) else AtwError(
        SCHEMA_VALIDATION_FAILED, str(exc)
    )
    timeout = error.code == RECEIPT_TIMEOUT
    if json_output:
        _emit_json({
            "request_id": request_id, "status": "timeout" if timeout else "rejected",
            "monitoring": False, "error": error.as_dict(),
        })
    else:
        typer.echo(str(error), err=True)
    raise typer.Exit(2 if timeout else 1) from None


def _submission_result(receipt_data: Receipt, json_output: bool) -> None:
    if json_output:
        _emit_json(receipt_data.to_json_dict())
    else:
        typer.echo(f"{receipt_data.status} request={receipt_data.request_id} task={receipt_data.task_id}")
        if receipt_data.error:
            typer.echo(f"{receipt_data.error.code}: {receipt_data.error.message}", err=True)
    if receipt_data.status != "accepted" or not receipt_data.monitoring:
        raise typer.Exit(1)


@app.command("submit")
def submit(
    file: Path,
    wait: int = typer.Option(30, min=0, max=3600),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """原子投递已准备好的请求，并按请求内容核对回执。"""
    cfg, paths = _load_or_exit(json_output)
    request_id = None
    try:
        raw = read_request(file, cfg.max_json_bytes)
        request_id = parse_request_json(raw)[0].request_id
        repo, db = _repo(paths)
        try:
            result = submit_request(raw, cfg, paths, repo, wait)
        finally:
            db.close()
    except (AtwError, OSError, ValueError, sqlite3.Error) as exc:
        _submission_error(exc, json_output, request_id)
        return
    _submission_result(result, json_output)


@app.command("handoff")
def handoff(
    task_id: str = typer.Option(...),
    name: str = typer.Option(...),
    run_dir: Path = typer.Option(...),
    log: Path = typer.Option(...),
    result: Path = typer.Option(...),
    success_log: str = typer.Option(...),
    pid: int | None = typer.Option(None, min=1),
    create_time: float | None = typer.Option(None),
    process_name: str | None = typer.Option(None),
    cmdline_contains: list[str] | None = typer.Option(None),
    request_id: str | None = typer.Option(None),
    project: str | None = typer.Option(None),
    run_started_at: str | None = typer.Option(None),
    failure_log: list[str] | None = typer.Option(None),
    progress_csv: Path | None = typer.Option(None),
    progress_column: str | None = typer.Option(None),
    progress_plan: Path | None = typer.Option(None),
    progress_unknown: str | None = typer.Option(None),
    counter_base: int = typer.Option(1, min=0, max=1),
    total: int | None = typer.Option(None, min=1),
    unit: str = typer.Option("epoch"),
    progress_minutes: int = typer.Option(30, min=0, max=10080),
    request_out: Path | None = typer.Option(None),
    dry_run: bool = typer.Option(False),
    wait: int = typer.Option(30, min=0, max=3600),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """接管已启动的任务。按 PID 或唯一进程匹配绑定。"""
    cfg, paths = _load_or_exit(json_output, create_dirs=not dry_run)
    try:
        roots = resolve_allowed_roots(cfg)
        run = validate_read_path(str(run_dir.resolve()), roots, require_exists=True, expect_directory=True)
        log_path = validate_read_path(str(log.resolve()), roots, field="log")
        result_path = validate_read_path(str(result.resolve()), roots, field="result")
        if not success_log.strip():
            raise ValueError("必须指定真实的成功标记")
        if sum(value is not None for value in (progress_csv, progress_plan, progress_unknown)) != 1:
            raise ValueError("请选择一个进度来源，或用 --progress-unknown 说明原因")
        if progress_unknown is not None and not progress_unknown.strip():
            raise ValueError("--progress-unknown 需要原因")
        saved = None
        stored = None
        if request_out and request_out.exists():
            saved = read_request(request_out, cfg.max_json_bytes)
            _, stored = parse_request_json(saved)
        request_id = request_id or (stored["request_id"] if stored else f"req-{uuid4().hex}")
        monitor = ProcessMonitor(create_time_tolerance=cfg.process_create_time_tolerance_seconds)
        description = f"进度未知：{progress_unknown}" if progress_unknown else ""
        if pid is not None and not process_name and not cmdline_contains:
            observed = create_time
            if observed is None:
                observed = stored["target"].get("create_time_epoch") if stored else None
            if observed is None:
                observed = psutil.Process(pid).create_time()
            target = Target(kind="pid", pid=pid, create_time_epoch=observed)
            identity = monitor.validate_pid_target(target).identity if not stored else None
        elif pid is None and create_time is None and process_name and cmdline_contains:
            match = Target(
                kind="match", name=process_name, cmdline_contains=cmdline_contains, match_mode="unique"
            )
            fingerprint = hashlib.sha256(match.model_dump_json().encode("utf-8")).hexdigest()
            description = "\n".join(filter(None, [description, f"进程匹配摘要：{fingerprint}"]))
            identity = monitor.match_target(match).identity if not stored else None
            target = Target.model_validate(stored["target"]) if stored else Target(
                kind="pid", pid=identity.pid, create_time_epoch=identity.create_time_epoch
            )
        else:
            raise ValueError("请选择 --pid 或 --process-name 加 --cmdline-contains")
        if run_started_at is not None:
            start = datetime.fromisoformat(run_started_at)
            if start.tzinfo is None:
                raise ValueError("--run-started-at 必须是含时区的 ISO 8601 时间")
        elif stored:
            start = parse_iso(stored["task"]["run_started_at"])
        else:
            start = epoch_to_dt(identity.create_time_epoch)
        progress = None
        if progress_csv is not None:
            progress = PROGRESS_ADAPTER.validate_python({
                "kind": "csv", "path": str(progress_csv.resolve()), "column": progress_column,
                "counter_base": counter_base, "total": total, "unit": unit,
            })
            progress = normalize_progress(progress, roots, str(run))
        if progress_plan is not None:
            plan_id, progress = load_plan(progress_plan.resolve(), roots, str(run))
            if plan_id != task_id:
                raise ValueError("进度清单 task_id 与本次任务不符")
        request = {
            "protocol": PROTOCOL_PROGRESS if progress is not None else PROTOCOL_TASK,
            "operation": "register", "request_id": request_id, "task_id": task_id, "revision": 1,
            "expires_at": stored["expires_at"] if stored else dt_to_z(utc_now() + timedelta(hours=1)),
            "task": {
                "name": name, "project": project, "run_started_at": dt_to_z(start),
                "description": description or None,
            },
            "target": target.model_dump(mode="json", exclude_none=True),
            "evidence": {
                "run_directory": str(run), "log": {"path": str(log_path)},
                "result_files": [str(result_path)],
            },
            "rules": {
                "success": {"all": [
                    {"kind": "process_tree_exited"},
                    {"kind": "log_contains", "value": success_log},
                    {"kind": "file_exists_fresh", "path": str(result_path)},
                ]},
                "failure": {"any": [
                    {"kind": "log_contains", "value": marker} for marker in failure_log
                ]} if failure_log else None,
            },
            "notifications": {"progress_interval_minutes": progress_minutes},
        }
        if progress is not None:
            request["progress"] = progress.model_dump(mode="json", exclude_none=True)
        raw = json_dumps(request).encode("utf-8")
        if len(raw) > cfg.max_json_bytes:
            raise AtwError(FILE_TOO_LARGE, "请求超过服务配置的字节限制")
        model, data = parse_request_json(raw)
        if stored and request_content_hash(stored) != request_content_hash(data):
            raise AtwError(REQUEST_CONFLICT, "参数与保存的原请求不同")
        if dry_run:
            _emit_json({
                "request": data,
                "run_start_source": "explicit" if run_started_at else "saved" if stored else "process",
                "progress": sample_progress(
                    progress, task_id=task_id, run_dir=str(run), roots=roots,
                    started_at=start, now=utc_now(),
                ) if progress else None,
            })
            return
        if request_out and saved is None:
            atomic_write_bytes(request_out, raw, replace=False, secure_parent=False)
        repo, db = _repo(paths)
        try:
            response = submit_request(saved or raw, cfg, paths, repo, wait)
        finally:
            db.close()
        if response.status == "accepted":
            bound = response.bound_process
            if (
                response.request_id != model.request_id or response.task_id != task_id
                or response.revision != 1 or bound is None
                or (identity and (bound.pid != identity.pid or
                    abs(bound.create_time_epoch - identity.create_time_epoch) >
                    cfg.process_create_time_tolerance_seconds))
            ):
                raise AtwError("PROCESS_IDENTITY_MISMATCH", "回执与交接时确认的进程或任务不一致")
            monitor.validate_pid_target(Target(
                kind="pid", pid=bound.pid, create_time_epoch=bound.create_time_epoch
            ))
    except (AtwError, OSError, ValueError, sqlite3.Error, psutil.Error) as exc:
        if isinstance(exc, psutil.NoSuchProcess):
            exc = AtwError(PROCESS_NOT_FOUND, str(exc))
        _submission_error(exc, json_output, request_id)
        return
    _submission_result(response, json_output)


@progress_app.command("report")
def report_progress(
    plan: Path = typer.Option(...),
    item: str = typer.Option(...),
    attempt: int = typer.Option(..., min=1),
    state: State = typer.Option(...),
    completed: int | None = typer.Option(None, min=0),
    acceptance: Acceptance | None = typer.Option(None),
    run_dir: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """写入本次运行的单个子任务状态。"""
    cfg, _ = _load_or_exit(json_output, create_dirs=False)
    try:
        roots = resolve_allowed_roots(cfg)
        run = str((run_dir or plan.parent).resolve())
        task_id, queue = load_plan(plan.resolve(), roots, run)
        entry = next((entry for entry in queue.items if entry.id == item), None)
        if entry is None:
            raise ValueError("清单中没有该子任务")
        report = ItemReport(
            task_id=task_id, item_id=item, attempt=attempt, state=state, completed=completed,
            acceptance=acceptance, updated_at=utc_now(),
        )
        write_report(report, entry, roots, run)
    except (AtwError, OSError, ValueError) as exc:
        _submission_error(exc, json_output)
        return
    if json_output:
        _emit_json(report.model_dump(mode="json", exclude_none=True))
    else:
        typer.echo(f"{item} · {state} · attempt {attempt}")


@app.command("doctor")
def doctor(json_output: bool = typer.Option(False, "--json")) -> None:
    """系统能力、依赖、权限与配置诊断（自动脱敏）。"""
    cfg, paths = _load_or_exit(json_output)
    result = run_doctor(cfg, paths)
    if json_output:
        _emit_json(result)
    else:
        typer.echo(f"ATW doctor: {result['overall']}")
        for check in result["checks"]:
            typer.echo(f"  [{check['status']}] {check['name']}: {check['detail']}")
    raise typer.Exit(0 if result["overall"] != "FAIL" else 1)


@app.command("notify")
def notify_test(
    json_output: bool = typer.Option(False, "--json"),
    target: str = typer.Argument("test"),
) -> None:
    """发送一条测试消息（mock 或真实钉钉），不接触计算进程。"""
    if target != "test":
        typer.echo("仅支持: atw notify test", err=True)
        raise typer.Exit(2)
    cfg, paths = _load_or_exit(json_output)
    notifier = None
    try:
        notifier = build_notifier(cfg, paths.mock_notifications_file)
        message = render_test_message(
            node_label=cfg.node_label or cfg.instance_id, provider=notifier.channel
        )
        result = notifier.send(message)
        payload = {
            "ok": result.ok,
            "channel": notifier.channel,
            "title": message.title,
            "error": result.error,
            "error_code": result.error_code,
            "route": result.route or (
                notifier.route if isinstance(notifier, DingTalkNotifier) else "local"
            ),
        }
        if json_output:
            _emit_json(payload)
        else:
            typer.echo(
                f"{'OK' if result.ok else 'FAIL'} channel={notifier.channel} route={payload['route']}"
                + (f" [{result.error_code}]" if result.error_code else "")
                + (f" error={result.error}" if result.error else "")
            )
        raise typer.Exit(0 if result.ok else 1)
    except AtwError as exc:
        if json_output:
            _emit_json({"ok": False, "error": exc.as_dict()})
        else:
            typer.echo(f"错误 {exc.code}: {exc.message}", err=True)
        raise typer.Exit(1) from None
    finally:
        if notifier is not None:
            notifier.close()


@app.command("notifications")
def notifications(
    limit: int = typer.Option(20, "--limit", min=1, max=200),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """通知队列状态与最近消息（不含密钥）。"""
    cfg, paths = _load_or_exit(json_output)
    repo, db = _repo(paths)
    try:
        counts = repo.outbox_counts()
        session = repo.last_session()
        route_raw = repo.get_state("notification_route")
        service_route = json.loads(route_raw) if route_raw and session else None
        if service_route and service_route["session_id"] == session["session_id"]:
            service_route.update(started_at=session["started_at"], ended_at=session["ended_at"])
        else:
            service_route = None
        recent = []
        for row in repo.recent_notifications(limit):
            try:
                payload = json.loads(row["payload_json"])
            except ValueError:
                payload = {}
            last_failure = None
            if row["failed_at"]:
                last_failure = {
                    "at": row["failed_at"],
                    "code": row["failure_code"],
                    "summary": row["failure_summary"],
                    "route": row["failure_route"],
                }
            elif row["last_error"]:
                last_failure = {
                    "at": None,
                    "code": "legacy_error",
                    "summary": "旧版投递错误，缺少脱敏诊断",
                    "route": None,
                }
            recent.append(
                {
                    "id": row["id"],
                    "task_id": row["task_id"],
                    "event_type": row["event_type"],
                    "channel": row["channel"],
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "created_at": row["created_at"],
                    "delivered_at": row["delivered_at"],
                    "last_error": last_failure["summary"] if last_failure and row["last_error"] else None,
                    "last_failure": last_failure,
                    "next_attempt_at": row["next_attempt_at"] if row["status"] == "pending" else None,
                    "title": payload.get("title"),
                }
            )
        data = {
            "protocol": "atw.notifications.v1",
            "counts": counts,
            "recent": recent,
            "provider": cfg.notification.provider,
            "service_route": service_route,
            "rate_limit_per_minute": cfg.notification.rate_limit_per_minute,
        }
        if json_output:
            _emit_json(data)
            return
        typer.echo(f"通知队列: {counts}")
        if service_route:
            typer.echo(
                f"最近服务会话路由: {service_route['channel']} {service_route['route']}"
                f"（启动 {service_route['started_at']}）"
            )
        else:
            typer.echo("最近服务会话路由: 未记录")
        for item in recent:
            typer.echo(
                f"{item['created_at']} {item['status']:<9} {item['event_type']:<18} "
                f"{item['task_id'] or '-'} {item['title'] or ''}"
            )
            if failure := item["last_failure"]:
                typer.echo(
                    f"  最近失败 {failure['at'] or '-'} [{failure['code']}] "
                    f"{failure['summary']}  路由 {failure['route'] or '-'}"
                )
            if retry := item["next_attempt_at"]:
                typer.echo(f"  下次可重试: {retry}")
    finally:
        db.close()


@app.command("service")
def service(
    action: str = typer.Argument("run", help="目前仅支持 run"),
    max_seconds: float | None = typer.Option(
        None, "--max-seconds", hidden=True, help="测试用：运行指定秒数后退出"
    ),
) -> None:
    """前台运行常驻服务（systemd / Task Scheduler 调用 `atw service run`）。"""
    if action != "run":
        typer.echo("仅支持: atw service run", err=True)
        raise typer.Exit(2)
    cfg, paths = _load_or_exit(False)
    code = ServiceRunner(cfg, paths).run(max_seconds=max_seconds)
    raise typer.Exit(code)


def main() -> None:
    # Make --json output UTF-8 on Windows regardless of the console code page.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
