from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import psutil
import pytest
import yaml

from atw.config import AtwPaths, Config, load_config, resolve_allowed_roots
from atw.monitoring.evidence import EvidenceMonitor
from atw.monitoring.gpu import GpuSnapshot
from atw.monitoring.process import ProcessMonitor
from atw.monitoring.supervisor import Supervisor
from atw.rules.engine import RuleEngine
from atw.storage.db import Database
from atw.storage.repository import Repository
from atw.tasks.service import TaskService
from atw.utils.timeutil import utc_now

# On Windows a venv ``python.exe`` is a redirector that spawns the real
# interpreter as a child.  Tests spawn the base interpreter directly so a
# process tree has one root; production handles wrapper+child trees explicitly.
PYTHON = getattr(sys, "_base_executable", sys.executable)


@pytest.fixture
def atw_home(tmp_path: Path) -> tuple[Config, AtwPaths]:
    home = tmp_path / "atw-home"
    (home / "config").mkdir(parents=True)
    config = {
        "instance_id": "test-node",
        "node_label": "test-node / CPU",
        "scan_interval_seconds": 1,
        "sample_interval_seconds": 1,
        "outbox_interval_seconds": 1,
        "finalize_grace_seconds": 0,
        "allowed_read_roots": [str(tmp_path)],
        "max_json_bytes": 262144,
        "notification": {
            "provider": "mock",
            "rate_limit_per_minute": 15,
            "retry_base_seconds": 1,
            "retry_max_seconds": 10,
            "max_attempts": 0,
        },
        "health_summary": {"enabled": False, "time": "08:00"},
    }
    (home / "config" / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )
    cfg, paths = load_config(home=home)
    return cfg, paths


class UnavailableGpu:
    def sample(self, pids):
        return GpuSnapshot(available=False, reason="test: no GPU")

    def capability(self):
        return {"available": False, "reason": "test: no GPU", "devices": []}


@pytest.fixture
def supervisor(atw_home, repo) -> Supervisor:
    cfg, paths = atw_home
    return Supervisor(
        cfg,
        paths,
        repo,
        RuleEngine(),
        ProcessMonitor(create_time_tolerance=0.05),
        EvidenceMonitor(roots=resolve_allowed_roots(cfg)),
        UnavailableGpu(),  # type: ignore[arg-type]
        channel="mock",
    )


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met before timeout")


def drive_until_terminal(supervisor, repo, task_id: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    task = repo.get_task(task_id)
    while time.time() < deadline:
        supervisor.tick()
        task = repo.get_task(task_id)
        if task is not None and task.terminal:
            return task
        time.sleep(0.2)
    raise AssertionError(f"task {task_id} not terminal; state={task.state if task else None}")


@pytest.fixture
def register_task(atw_home, task_service, tmp_path: Path):
    def _do(proc, run: Path, request_id: str = "req-1", task_id: str = "task-1", **kwargs):
        data = make_register(
            tmp_path=tmp_path,
            proc=proc,
            request_id=request_id,
            task_id=task_id,
            run_dir=run,
            **kwargs,
        )
        path = atomic_submit(atw_home[1], data)
        receipt = task_service.process_file(path)
        return data, receipt

    return _do


@pytest.fixture
def repo(atw_home) -> Repository:
    _, paths = atw_home
    db = Database(paths.db_file)
    db.connect()
    yield Repository(db)
    db.close()


@pytest.fixture
def task_service(atw_home, repo) -> TaskService:
    cfg, paths = atw_home
    return TaskService(
        cfg,
        paths,
        repo,
        ProcessMonitor(create_time_tolerance=0.05),
        session_id="session-test",
        boot_id="boot-test",
        channel="mock",
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_submit(paths: AtwPaths, data: dict[str, Any]) -> Path:
    name = data["request_id"]
    tmp = paths.inbox / f"{name}.json.tmp"
    final = paths.inbox / f"{name}.json"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def make_register(
    *,
    tmp_path: Path,
    proc: subprocess.Popen,
    request_id: str,
    task_id: str,
    name: str = "unit test task",
    log_name: str = "run.log",
    result_name: str = "results/result.csv",
    success_log: str = "Simulation completed",
    failure_log: str | None = None,
    extra_rules: dict[str, Any] | None = None,
    evidence_paths: tuple[str, str] | None = None,
    expires_delta: int = 3600,
    create_time: float | None = None,
    run_dir: Path | None = None,
    notifications: dict[str, Any] | None = None,
) -> dict[str, Any]:
    create_time = create_time if create_time is not None else psutil.Process(proc.pid).create_time()
    run_dir = run_dir or (tmp_path / "runs" / task_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path, result_path = (
        evidence_paths if evidence_paths else (str(run_dir / log_name), str(run_dir / result_name))
    )
    success = {
        "all": [
            {"kind": "process_tree_exited"},
            {"kind": "log_contains", "value": success_log},
            {"kind": "file_exists_fresh", "path": result_path},
        ]
    }
    failure = {
        "any": [{"kind": "log_contains", "value": failure_log}] if failure_log else [],
        "finalize": "on_process_exit",
    }
    if not failure["any"]:
        failure = None
    rules: dict[str, Any] = {
        "success": success,
        "failure": failure,
        "warnings": [],
        "max_runtime_seconds": 3600,
    }
    if extra_rules:
        rules.update(extra_rules)
    return {
        "protocol": "atw.task.v1",
        "operation": "register",
        "request_id": request_id,
        "task_id": task_id,
        "revision": 1,
        "expires_at": (utc_now() + timedelta(seconds=expires_delta))
        .isoformat()
        .replace("+00:00", "Z"),
        "task": {
            "name": name,
            "project": "tests",
            "run_started_at": (utc_now() - timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
        },
        "target": {
            "kind": "pid",
            "pid": proc.pid,
            "create_time_epoch": create_time,
            "tree": {"enabled": True},
        },
        "evidence": {
            "run_directory": str(run_dir),
            "log": {"path": log_path, "scope": "current_run", "encoding": "utf-8"},
            "result_files": [result_path],
        },
        "rules": rules,
        "notifications": notifications
        or {
            "on_accepted": True,
            "on_success": True,
            "on_failure": True,
            "on_unknown": True,
            "on_interrupted": True,
            "on_warning": True,
            "progress_interval_minutes": 0,
            "warning_repeat_minutes": 30,
        },
    }


WORKER_SCRIPT = r"""
import pathlib, sys, time
run = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
(run / "ready").write_text("1", encoding="utf-8")
while not (run / "go").exists():
    time.sleep(0.02)
log = run / "run.log"
log.parent.mkdir(parents=True, exist_ok=True)
log.write_text("task started\n", encoding="utf-8")
if mode == "success":
    time.sleep(0.4)
    log.write_text("task started\nSimulation completed\n", encoding="utf-8")
    (run / "results").mkdir(exist_ok=True)
    (run / "results" / "result.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    time.sleep(0.4)
elif mode == "failure":
    time.sleep(0.2)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("CUDA out of memory\n")
    time.sleep(0.4)
elif mode == "unknown":
    time.sleep(0.4)
elif mode == "long":
    time.sleep(30)
elif mode == "idle":
    time.sleep(3)
else:
    raise SystemExit(2)
"""


@pytest.fixture
def spawn_worker(tmp_path: Path):
    procs: list[subprocess.Popen] = []
    dirs: list[Path] = []

    def _spawn(mode: str = "success", name: str = "worker") -> tuple[subprocess.Popen, Path]:
        run = tmp_path / "runs" / f"{name}-{time.time_ns()}"
        run.mkdir(parents=True, exist_ok=True)
        script = tmp_path / "worker.py"
        if not script.exists():
            script.write_text(WORKER_SCRIPT, encoding="utf-8")
        proc = subprocess.Popen(
            [PYTHON, str(script), str(run), mode],
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        procs.append(proc)
        dirs.append(run)
        ready = run / "ready"
        deadline = time.time() + 10
        while not ready.exists() and time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(proc.stderr.read().decode(errors="replace"))
            time.sleep(0.02)
        return proc, run

    def _go(run: Path) -> None:
        (run / "go").write_text("1", encoding="utf-8")

    _spawn.go = _go  # type: ignore[attr-defined]
    yield _spawn

    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc.stderr:
            proc.stderr.close()
