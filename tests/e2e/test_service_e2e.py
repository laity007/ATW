from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import sysconfig
import threading
import time
from contextlib import closing, suppress
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path

import pytest
import yaml

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


def _start(
    home: Path, *, min_sessions: int = 1, env_overrides: dict[str, str] | None = None
) -> subprocess.Popen:
    proc = subprocess.Popen(
        [BASE_PYTHON, "-m", "atw", "--home", str(home), "service", "run"],
        cwd=str(ROOT),
        env={**_env(), **(env_overrides or {})},
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


def _query(home: Path, sql: str, params: tuple = ()) -> list[tuple]:
    db_uri = (home / "data" / "atw.db").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(db_uri, uri=True)) as conn:
        return conn.execute(sql, params).fetchall()


def _set_config(home: Path, **changes) -> None:
    path = home / "config" / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config.update(changes)
    path.write_text(yaml.safe_dump(config), encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGTERM to verify graceful shutdown")
def test_dingtalk_timeouts_leave_sampling_and_intake_running(atw_home, spawn_worker, tmp_path):
    _, paths = atw_home
    home = paths.root
    _set_config(
        home,
        scan_interval_seconds=5,
        sample_interval_seconds=10,
        outbox_interval_seconds=2,
        notification={
            "provider": "dingtalk",
            "timeout_seconds": 20,
            "retry_base_seconds": 5,
        },
    )
    entered = threading.Event()
    healthy = threading.Event()

    class DingTalkHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            entered.set()
            if healthy.wait(timeout=70):
                with suppress(OSError):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"errcode":0}')

        def log_message(self, format, *args):
            pass

    def submit(task_id: str) -> float:
        proc, run = spawn_worker("long", task_id)
        data = make_register(
            tmp_path=tmp_path, proc=proc, request_id=task_id, task_id=task_id, run_dir=run
        )
        submitted = time.monotonic()
        atomic_submit(paths, data)
        assert _receipt(home, task_id)["status"] == "accepted"
        return time.monotonic() - submitted

    def notices() -> list[tuple]:
        return _query(
            home,
            "SELECT task_id,status,attempts,delivered_at FROM notification_outbox "
            "WHERE event_type='MONITOR_ACCEPTED' ORDER BY id",
        )

    with ThreadingHTTPServer(("127.0.0.1", 0), DingTalkHandler) as server:
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        env = {
            "ATW_DINGTALK_WEBHOOK": f"http://127.0.0.1:{server.server_port}/test?access_token=lab-token",
            "ATW_DINGTALK_SECRET": "local-test",
            "HTTP_PROXY": "http://lab-user:lab-password@127.0.0.1:1",
            "NO_PROXY": "127.0.0.1",
        }
        service = None
        try:
            service = _start(home, env_overrides=env)
            submit("first")
            assert entered.wait(timeout=10)
            receipt_delay = submit("during-timeout")
            assert receipt_delay <= 15, receipt_delay
            wait_until(lambda: all(row[2] >= 1 for row in notices()), timeout=50)
            assert [row[1] for row in notices()] == ["pending", "pending"]
            samples = [
                datetime.fromisoformat(ts)
                for (ts,) in _query(
                    home, "SELECT ts FROM observations WHERE task_id='first' ORDER BY ts"
                )
            ]
            assert len(samples) >= 4
            max_gap = max((later - earlier).total_seconds() for earlier, later in pairwise(samples))
            assert max_gap <= 15, max_gap
            healthy.set()
            wait_until(lambda: all(row[1] == "delivered" for row in notices()), timeout=28)
            first, second = notices()
            assert first[3] <= second[3]
            output = subprocess.check_output(
                [BASE_PYTHON, "-m", "atw", "--home", str(home), "notifications", "--json"],
                cwd=ROOT, env=_env(), text=True, encoding="utf-8",
            )
            assert not any(secret in output for secret in ("lab-token", "local-test", "lab-password", "sign="))
            diagnostics = json.loads(output)
            assert diagnostics["service_route"]["route"] == "direct (NO_PROXY)"
            assert len(diagnostics["recent"]) == 2
            for item in diagnostics["recent"]:
                assert item["last_failure"]["code"] == "timeout"
                assert item["last_failure"]["route"] == "direct (NO_PROXY)"
                assert item["next_attempt_at"] is None

            # Leave a third response blocked while shutting down the service.
            healthy.clear()
            entered.clear()
            submit("during-shutdown")
            assert entered.wait(timeout=10)
            stopped_at = time.monotonic()
            service.terminate()
            assert service.wait(timeout=8) == 0
            stop_delay = time.monotonic() - stopped_at
            assert notices()[-1] == ("during-shutdown", "pending", 0, None)
            healthy.set()
            service = _start(home, min_sessions=2, env_overrides=env)
            wait_until(lambda: notices()[-1][1] == "delivered", timeout=15)
            print(
                f"receipt={receipt_delay:.3f}s max_sample_gap={max_gap:.3f}s "
                f"shutdown={stop_delay:.3f}s delivered_after_retry=2 recovered_pending=1"
            )
        finally:
            healthy.set()
            if service is not None:
                _stop(service)
            server.shutdown()
            server_thread.join(timeout=5)


def test_restart_waits_for_late_evidence_and_finalizes_once(atw_home, spawn_worker, tmp_path):
    _, paths = atw_home
    home = paths.root
    _set_config(home, sample_interval_seconds=10, finalize_grace_seconds=15)
    service = _start(home)
    try:
        workers = {}
        for task_id in ("late-evidence", "missing-evidence"):
            proc, run = spawn_worker("unknown", task_id)
            workers[task_id] = (proc, run)
            data = make_register(
                tmp_path=tmp_path, proc=proc, request_id=task_id, task_id=task_id, run_dir=run
            )
            atomic_submit(paths, data)
            assert _receipt(home, task_id)["status"] == "accepted"
        _stop(service)
        for proc, run in workers.values():
            spawn_worker.go(run)
            assert proc.wait(timeout=10) == 0
        service = _start(home, min_sessions=2)
        resumed_at = time.monotonic()
        for task_id in workers:
            assert _task_state(home, task_id) == "MONITORING"

        run = workers["late-evidence"][1]
        time.sleep(5)
        with (run / "run.log").open("a", encoding="utf-8") as fh:
            fh.write("Simulation completed\n")
        (run / "results").mkdir()
        (run / "results" / "result.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        wait_until(lambda: _task_state(home, "late-evidence") == "SUCCESS", timeout=8)
        success_delay = time.monotonic() - resumed_at
        assert _task_state(home, "missing-evidence") == "MONITORING"
        wait_until(lambda: _task_state(home, "missing-evidence") == "UNKNOWN", timeout=17)
        unknown_delay = time.monotonic() - resumed_at
        assert unknown_delay >= 14.5, unknown_delay

        _stop(service)
        service = _start(home, min_sessions=3)
        assert _query(
            home,
            "SELECT task_id,event_type,COUNT(*) FROM events "
            "WHERE event_type IN ('SUCCESS','FAILED','UNKNOWN','INTERRUPTED') "
            "GROUP BY task_id,event_type ORDER BY task_id",
        ) == [("late-evidence", "SUCCESS", 1), ("missing-evidence", "UNKNOWN", 1)]
        print(f"late_evidence_success={success_delay:.3f}s missing_evidence_unknown={unknown_delay:.3f}s")
    finally:
        _stop(service)


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
