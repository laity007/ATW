#!/usr/bin/env python3
"""Build an AFHP/1 register request for the atw-task-handoff Skill.

Stdlib-only so it can run on an AI workstation.  It creates directories only
when --ensure-dirs is passed; it never creates fake result files.

Examples:
  # user supplied both paths
  build_task_json.py --task-id t1 --pid 123 --create-time 1.5 \
      --log /data/run.log --result /data/result.csv --project-dir /data

  # user supplied only the log path
  build_task_json.py --task-id t2 --pid 124 --create-time 2.5 \
      --log /data/run.log --project-dir /data

  # user chose defaults
  build_task_json.py --task-id t3 --pid 125 --create-time 3.5 \
      --project-dir /data --ensure-dirs
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _now() -> datetime:
    return datetime.now(UTC)


def _z(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _posix_abs(value: str) -> bool:
    return value.startswith("/")


def _abs(path: str, field: str) -> str:
    """Accept Linux absolute paths even when the AI runs on Windows."""
    if _posix_abs(path):
        return posixpath.normpath(path)
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise SystemExit(f"{field} 必须是绝对路径: {path}")
    return str(p)


def _join(base: str, *parts: str) -> str:
    if _posix_abs(base):
        return posixpath.normpath(posixpath.join(base, *parts))
    return str(Path(base).joinpath(*parts))


def _is_local(path: str) -> bool:
    if _posix_abs(path):
        return os.name != "nt"
    return True


def build(args: argparse.Namespace) -> dict:
    if not ID_RE.match(args.task_id):
        raise SystemExit(f"非法 task_id: {args.task_id}")
    request_id = args.request_id or f"req-{args.task_id}-{uuid.uuid4().hex[:10]}"
    if not ID_RE.match(request_id):
        raise SystemExit(f"非法 request_id: {request_id}")
    if args.kind == "pid":
        if not args.pid or args.create_time is None:
            raise SystemExit("target.kind=pid 需要 --pid 与 --create-time")
        target = {
            "kind": "pid",
            "pid": args.pid,
            "create_time_epoch": args.create_time,
            "tree": {"enabled": True},
        }
    else:
        if not args.process_name or not args.cmdline_contains:
            raise SystemExit("target.kind=match 需要 --process-name 与至少一个 --cmdline-contains")
        target = {
            "kind": "match",
            "name": args.process_name,
            "cmdline_contains": args.cmdline_contains,
            "match_mode": "unique",
            "tree": {"enabled": True},
        }

    project_dir = args.project_dir or str(Path.cwd())
    run_dir = args.run_dir or _join(project_dir, "runs", args.task_id)
    run_dir = _abs(run_dir, "--run-dir")
    log_path = _abs(args.log, "--log") if args.log else _join(run_dir, "run.log")
    result_path = (
        _abs(args.result, "--result") if args.result else _join(run_dir, "results", "result.csv")
    )
    if args.ensure_dirs:
        if _is_local(run_dir):
            Path(run_dir).mkdir(parents=True, exist_ok=True)
            Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        else:
            print(
                "提示: 目标路径属于远程 POSIX 主机；--ensure-dirs 不在本地创建，"
                "请在 B 端 SSH 中创建目录。",
                file=sys.stderr,
            )

    failure_markers = args.failure_log or [
        "Traceback (most recent call last)",
        "CUDA out of memory",
        "RuntimeError",
    ]
    rules = {
        "success": {
            "all": [
                {"kind": "process_tree_exited"},
                {"kind": "log_contains", "value": args.success_log},
                {"kind": "file_exists_fresh", "path": result_path},
            ]
        },
        "failure": {
            "any": [{"kind": "log_contains", "value": marker} for marker in failure_markers],
            "finalize": args.finalize,
        },
        "warnings": [],
        "max_runtime_seconds": args.max_runtime_seconds,
        "max_runtime_action": "warn",
    }
    if args.warning:
        rules["warnings"] = [
            {
                "id": "cpu-idle",
                "kind": "cpu_below",
                "percent": args.warning_percent,
                "duration_seconds": args.warning_seconds,
            }
        ]
    return {
        "protocol": "atw.task.v1",
        "operation": "register",
        "request_id": request_id,
        "task_id": args.task_id,
        "revision": 1,
        "expires_at": _z(_now() + timedelta(minutes=args.expires_minutes)),
        "task": {
            "name": args.name,
            "project": args.project,
            "run_started_at": _z(_now()),
            "description": args.description,
        },
        "target": target,
        "evidence": {
            "run_directory": run_dir,
            "log": {"path": log_path, "scope": "current_run", "encoding": "utf-8"},
            "result_files": [result_path],
        },
        "rules": rules,
        "notifications": {
            "on_accepted": True,
            "on_success": True,
            "on_failure": True,
            "on_unknown": True,
            "on_interrupted": True,
            "on_warning": True,
            "progress_interval_minutes": args.progress_minutes,
            "warning_repeat_minutes": args.warning_repeat_minutes,
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an ATW AFHP/1 register JSON")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--name", required=True)
    parser.add_argument("--project", default=None)
    parser.add_argument("--description", default=None)
    parser.add_argument("--project-dir", default=None)
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--log", default=None, help="explicit log path (absolute)")
    parser.add_argument("--result", default=None, help="explicit result path (absolute)")
    parser.add_argument("--kind", choices=["pid", "match"], default="pid")
    parser.add_argument("--pid", type=int, default=None)
    parser.add_argument("--create-time", type=float, default=None)
    parser.add_argument("--process-name", default=None)
    parser.add_argument("--cmdline-contains", action="append", default=None)
    parser.add_argument("--success-log", default="Simulation completed")
    parser.add_argument("--failure-log", action="append", default=None)
    parser.add_argument(
        "--finalize", choices=["immediate", "on_process_exit"], default="on_process_exit"
    )
    parser.add_argument("--max-runtime-seconds", type=int, default=None)
    parser.add_argument("--progress-minutes", type=int, default=30)
    parser.add_argument("--warning-repeat-minutes", type=int, default=30)
    parser.add_argument("--warning", action="store_true")
    parser.add_argument("--warning-percent", type=float, default=2.0)
    parser.add_argument("--warning-seconds", type=int, default=900)
    parser.add_argument("--expires-minutes", type=int, default=60)
    parser.add_argument("--ensure-dirs", action="store_true")
    parser.add_argument("--output", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    request = build(args)
    text = json.dumps(request, ensure_ascii=False, indent=2) + "\n"
    # Best-effort schema validation when the ATW package is importable.
    try:
        from atw.models.task import parse_request_json  # type: ignore

        parse_request_json(text.encode("utf-8"))
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover - defensive for CLI users
        print(f"警告: 本地 schema 校验未通过: {exc}", file=sys.stderr)
        return 1
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
