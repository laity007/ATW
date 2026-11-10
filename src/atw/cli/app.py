"""ATW CLI.  All ``--json`` output is pure JSON on stdout."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from typer import Context

from atw import __version__
from atw.config import AtwPaths, Config, load_config, resolve_allowed_roots
from atw.errors import RECEIPT_TIMEOUT, AtwError
from atw.health.bootid import current_boot_identity
from atw.health.doctor import run_doctor
from atw.intake.receipts import ReceiptStore
from atw.models.task import Receipt, parse_request_json
from atw.notifications.factory import build_notifier
from atw.notifications.templates import render_test_message
from atw.service.lock import SingleInstanceLock
from atw.service.runner import ServiceRunner
from atw.storage.db import Database
from atw.storage.repository import Repository
from atw.utils.pathsafe import validate_read_path
from atw.utils.timeutil import utc_now

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Agent Task Watcher V1.0 — 只监督与通知，不控制计算进程。",
)


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
        typer.echo(f"项目: {record.project or '-'}")
        typer.echo(
            f"运行开始: {record.run_started_at.isoformat() if record.run_started_at else '-'}"
        )
        typer.echo(f"受理: {record.accepted_at.isoformat() if record.accepted_at else '-'}")
        typer.echo(
            f"终态: {record.state if record.terminal else '-'}  {record.finalize_reason or ''}"
        )
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
    evidence = getattr(model, "evidence", None)
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
    if data is None:
        try:
            repo, db = _repo(paths)
            try:
                row = repo.get_request(request_id)
                if row is not None:
                    data = json.loads(row["receipt_json"])
                    Receipt.model_validate(data)
                    store.write(Receipt.model_validate(data))
            finally:
                db.close()
        except Exception:
            data = None
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
        }
        if json_output:
            _emit_json(payload)
        else:
            typer.echo(
                f"{'OK' if result.ok else 'FAIL'} channel={notifier.channel}"
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
        recent = []
        for row in repo.recent_notifications(limit):
            try:
                payload = json.loads(row["payload_json"])
            except ValueError:
                payload = {}
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
                    "last_error": row["last_error"],
                    "title": payload.get("title"),
                }
            )
        data = {
            "protocol": "atw.notifications.v1",
            "counts": counts,
            "recent": recent,
            "provider": cfg.notification.provider,
            "rate_limit_per_minute": cfg.notification.rate_limit_per_minute,
        }
        if json_output:
            _emit_json(data)
            return
        typer.echo(f"通知队列: {counts}")
        for item in recent:
            typer.echo(
                f"{item['created_at']} {item['status']:<9} {item['event_type']:<18} "
                f"{item['task_id'] or '-'} {item['title'] or ''}"
            )
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
