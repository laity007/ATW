from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

from atw.storage.db import Database
from conftest import atomic_submit, make_register, wait_until

ROOT = Path(__file__).resolve().parents[2]
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)
VENV_SITE = sysconfig.get_paths()["purelib"]


def _env() -> dict[str, str]:
    env = os.environ.copy()
    parts = [str(ROOT / "src"), VENV_SITE]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("ATW_HOME", None)
    env.pop("ATW_CONFIG", None)
    return env


def _start(home: Path, *, min_sessions: int = 1) -> subprocess.Popen:
    proc = subprocess.Popen(
        [BASE_PYTHON, "-m", "atw", "--home", str(home), "service", "run"],
        cwd=str(ROOT),
        env=_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    db_path = home / "data" / "atw.db"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"service exited early code={proc.returncode}")
        if db_path.exists():
            try:
                db = Database(db_path)
                db.connect()
                row = db.query_one("SELECT COUNT(*) AS n FROM service_sessions")
                db.close()
                if row and int(row["n"]) >= min_sessions:
                    return proc
            except Exception:
                pass
        time.sleep(0.1)
    raise RuntimeError("service did not start")


def _stop(proc: subprocess.Popen, *, hard: bool = False) -> None:
    if proc.poll() is not None:
        return
    if hard:
        proc.kill()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _receipt(home: Path, request_id: str) -> dict:
    path = home / "receipts" / f"{request_id}.json"
    wait_until(path.exists, timeout=20)
    return json.loads(path.read_text(encoding="utf-8"))


def _task_state(home: Path, task_id: str) -> str | None:
    db = Database(home / "data" / "atw.db")
    db.connect()
    try:
        row = db.query_one("SELECT state FROM tasks WHERE task_id=?", (task_id,))
        return row["state"] if row else None
    finally:
        db.close()


def test_service_hot_intake_success_and_mock_notification(atw_home, spawn_worker, tmp_path):
    cfg, paths = atw_home
    home = paths.root
    service = _start(home)
    proc = None
    try:
        proc, run = spawn_worker("success", "e2e-success")
        data = make_register(
            tmp_path=tmp_path,
            proc=proc,
            request_id="req-e2e-success",
            task_id="task-e2e-success",
            run_dir=run,
        )
        atomic_submit(paths, data)
        receipt = _receipt(home, "req-e2e-success")
        assert receipt["status"] == "accepted"
        assert receipt["monitoring"] is True
        assert receipt["bound_process"]["pid"] == proc.pid
        (run / "go").write_text("1", encoding="utf-8")
        wait_until(lambda: proc.poll() is not None, timeout=20)
        wait_until(lambda: _task_state(home, "task-e2e-success") == "SUCCESS", timeout=30)
        sink = paths.mock_notifications_file

        def _titles() -> list[str]:
            if not sink.exists():
                return []
            return [
                json.loads(line)["title"]
                for line in sink.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        wait_until(
            lambda: (
                any("已接管" in t for t in _titles()) and any("成功完成" in t for t in _titles())
            ),
            timeout=20,
        )
        titles = _titles()
        assert any("已接管" in title for title in titles), titles
        assert any("成功完成" in title for title in titles), titles
        assert all(title.startswith("[ATW]") for title in titles)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        _stop(service)


def test_service_hard_restart_recovers_and_does_not_duplicate_accept(atw_home, spawn_worker):
    cfg, paths = atw_home
    home = paths.root
    service = _start(home)
    proc = None
    try:
        proc, run = spawn_worker("long", "e2e-restart")
        # The ATW_HOME is under tmp_path; allowed root is tmp_path.  The run
        # directory is also under tmp_path, so register uses tmp_path.
        data = make_register(
            tmp_path=paths.root.parent,
            proc=proc,
            request_id="req-e2e-restart",
            task_id="task-e2e-restart",
            run_dir=run,
        )
        atomic_submit(paths, data)
        receipt = _receipt(home, "req-e2e-restart")
        assert receipt["status"] == "accepted"
        (run / "go").write_text("1", encoding="utf-8")
        wait_until(lambda: _task_state(home, "task-e2e-restart") == "MONITORING", timeout=10)
        time.sleep(1.5)
        _stop(service, hard=True)
        service = _start(home, min_sessions=2)
        wait_until(lambda: _task_state(home, "task-e2e-restart") == "MONITORING", timeout=20)
        db = Database(home / "data" / "atw.db")
        db.connect()
        try:
            accepted_count = db.query_one(
                "SELECT COUNT(*) AS n FROM events WHERE task_id=? AND event_type='MONITOR_ACCEPTED'",
                ("task-e2e-restart",),
            )["n"]
            recovered = db.query_one(
                "SELECT COUNT(*) AS n FROM events WHERE event_type='SERVICE_RECOVERED'"
            )["n"]
        finally:
            db.close()
        assert int(accepted_count) == 1
        assert int(recovered) == 1
        proc.terminate()
        proc.wait(timeout=10)
        wait_until(
            lambda: _task_state(home, "task-e2e-restart") in {"UNKNOWN", "SUCCESS", "FAILED"},
            timeout=30,
        )
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        _stop(service)


def test_five_parallel_tasks_are_isolated(atw_home, spawn_worker, tmp_path):
    cfg, paths = atw_home
    home = paths.root
    service = _start(home)
    procs = []
    runs = []
    try:
        for index in range(5):
            proc, run = spawn_worker("success", f"e2e-parallel-{index}")
            procs.append(proc)
            runs.append(run)
            data = make_register(
                tmp_path=tmp_path,
                proc=proc,
                request_id=f"req-par-{index}",
                task_id=f"task-par-{index}",
                run_dir=run,
                name=f"parallel task {index}",
            )
            atomic_submit(paths, data)
        for index in range(5):
            receipt = _receipt(home, f"req-par-{index}")
            assert receipt["status"] == "accepted"
            assert receipt["task_id"] == f"task-par-{index}"
        for run in runs:
            (run / "go").write_text("1", encoding="utf-8")
        wait_until(
            lambda: all(_task_state(home, f"task-par-{index}") == "SUCCESS" for index in range(5)),
            timeout=40,
        )
        db = Database(home / "data" / "atw.db")
        db.connect()
        try:
            rows = db.query_all(
                "SELECT task_id, state FROM tasks WHERE task_id LIKE 'task-par-%' ORDER BY task_id"
            )
        finally:
            db.close()
        assert len(rows) == 5
        assert {row["state"] for row in rows} == {"SUCCESS"}
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        _stop(service)
