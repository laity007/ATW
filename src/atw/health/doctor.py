"""``atw doctor`` diagnostics with secret-free output."""

from __future__ import annotations

import importlib
import os
import platform
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

from atw import __version__
from atw.config import AtwPaths, Config, dingtalk_credentials
from atw.health.bootid import current_boot_identity
from atw.monitoring.gpu import GpuMonitor
from atw.service.lock import SingleInstanceLock
from atw.storage.db import Database
from atw.storage.repository import Repository
from atw.utils.timeutil import utc_now


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _service_running(paths: AtwPaths) -> bool:
    lock = SingleInstanceLock(paths.lock_file)
    if lock.acquire():
        lock.release()
        return False
    return True


def _writable(directory: Path) -> tuple[bool, str]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".atw-doctor-", delete=True):
            pass
        return True, "可写"
    except OSError as exc:
        return False, f"不可写: {type(exc).__name__}: {exc}"


def run_doctor(cfg: Config, paths: AtwPaths) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    py_ok = sys.version_info >= (3, 11)
    checks.append(
        _check(
            "python",
            "PASS" if py_ok else "FAIL",
            f"{platform.python_version()} (>=3.11 required)",
        )
    )
    for module in ("psutil", "pydantic", "yaml", "httpx", "typer", "watchdog"):
        try:
            importlib.import_module(module)
            checks.append(_check(f"dependency:{module}", "PASS", "import ok"))
        except Exception as exc:
            checks.append(
                _check(f"dependency:{module}", "FAIL", f"import failed: {type(exc).__name__}")
            )
    try:
        import pynvml  # noqa: F401

        checks.append(_check("dependency:pynvml", "PASS", "import ok"))
    except Exception:
        checks.append(_check("dependency:pynvml", "WARN", "缺失；GPU 指标将降级为 unavailable"))

    if paths.config_file.exists():
        checks.append(_check("config", "PASS", f"loaded {paths.config_file}"))
    else:
        checks.append(_check("config", "WARN", f"未找到 {paths.config_file}，使用默认配置"))
    for root in cfg.allowed_read_roots:
        p = Path(root).expanduser()
        checks.append(
            _check(
                "allowed_root",
                "PASS" if p.exists() and p.is_dir() else "WARN",
                f"{p} ({'存在' if p.exists() else '不存在'})",
            )
        )
    for name, directory in (
        ("inbox", paths.inbox),
        ("accepted", paths.accepted),
        ("rejected", paths.rejected),
        ("archive", paths.archive),
        ("receipts", paths.receipts),
        ("data", paths.data_dir),
        ("logs", paths.logs_dir),
    ):
        ok, detail = _writable(directory)
        checks.append(_check(f"path:{name}", "PASS" if ok else "FAIL", detail))

    db_ok = False
    db_detail = "未检查"
    try:
        db = Database(paths.db_file)
        db.connect()
        integrity = db.integrity_check()
        repo = Repository(db)
        counts = repo.counts()
        db_detail = (
            f"integrity={integrity}, tasks={counts['tasks']}, monitoring={counts['monitoring']}"
        )
        db_ok = integrity == "ok"
        db.close()
    except Exception as exc:
        db_detail = f"数据库错误: {type(exc).__name__}: {exc}"
    checks.append(_check("database", "PASS" if db_ok else "FAIL", db_detail))

    try:
        running = _service_running(paths)
        checks.append(
            _check(
                "service_lock",
                "PASS" if running else "WARN",
                "服务正在运行" if running else "服务未运行（锁可用）",
            )
        )
    except Exception as exc:
        checks.append(_check("service_lock", "FAIL", f"锁检查失败: {type(exc).__name__}"))

    webhook, secret = dingtalk_credentials(cfg)
    configured = bool(webhook and secret)
    provider = cfg.notification.provider
    effective = "dingtalk" if configured else ("mock" if provider == "auto" else provider)
    checks.append(
        _check(
            "notification",
            "PASS" if (effective == "dingtalk" or provider == "mock") else "WARN",
            f"provider={provider}, effective={effective}, credentials={'configured' if configured else 'missing'}",
        )
    )
    if configured:
        token_len = 0
        try:
            from urllib.parse import parse_qs, urlparse

            token_len = len(
                (parse_qs(urlparse(webhook or "").query).get("access_token") or [""])[0]
            )
        except Exception:
            pass
        checks.append(
            _check(
                "notification_credentials",
                "PASS",
                f"webhook 已配置（token 长度 {token_len}，内容已隐藏），secret 已配置",
            )
        )

    gpu = GpuMonitor().capability()
    checks.append(
        _check(
            "gpu",
            "PASS" if gpu.get("available") else "WARN",
            "NVML 可用，设备数 " + str(len(gpu.get("devices") or []))
            if gpu.get("available")
            else str(gpu.get("reason") or "NVML 不可用"),
        )
    )
    boot = current_boot_identity()
    checks.append(
        _check(
            "boot_identity",
            "PASS",
            f"confidence={boot.confidence}, detail={boot.detail}",
        )
    )
    try:
        db2 = Database(paths.db_file)
        db2.connect()
        last = Repository(db2).last_session()
        db2.close()
        if last:
            checks.append(
                _check(
                    "last_session",
                    "PASS",
                    f"id={last['session_id']}, started={last['started_at']}, "
                    f"ended={last['ended_at']}, clean={last['clean_shutdown']}",
                )
            )
        else:
            checks.append(_check("last_session", "WARN", "尚无服务会话（首次安装）"))
    except Exception as exc:
        checks.append(_check("last_session", "WARN", f"读取失败: {type(exc).__name__}"))

    if os.name != "nt":
        try:
            mode = paths.root.stat().st_mode & 0o777
            ok = (mode & 0o077) == 0
            checks.append(
                _check(
                    "permissions",
                    "PASS" if ok else "WARN",
                    f"ATW_HOME mode={oct(mode)}" + ("" if ok else "（建议 0700，仅当前用户）"),
                )
            )
        except OSError as exc:
            checks.append(_check("permissions", "WARN", f"无法检查: {type(exc).__name__}"))

    statuses = {c["status"] for c in checks}
    overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
    return {
        "protocol": "atw.doctor.v1",
        "atw_version": __version__,
        "generated_at": utc_now().isoformat(),
        "overall": overall,
        "system": {
            "platform": platform.platform(),
            "hostname": socket.gethostname(),
            "python": platform.python_version(),
            "user": os.environ.get("USERNAME") or os.environ.get("USER"),
            "instance_id": cfg.instance_id,
        },
        "paths": {
            "home": str(paths.root),
            "config": str(paths.config_file),
            "inbox": str(paths.inbox),
            "db": str(paths.db_file),
            "log": str(paths.log_file),
        },
        "checks": checks,
    }
