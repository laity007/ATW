from __future__ import annotations

import json
import os
import time

from atw.storage.repository import Repository
from atw.utils.timeutil import utc_now
from conftest import drive_until_terminal, wait_until


def _terminal(repo: Repository, task_id: str):
    return repo.get_task(task_id)


def test_success_with_log_and_result(atw_home, repo, supervisor, spawn_worker, register_task):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "supervisor-success")
    _, receipt = register_task(proc, run)
    assert receipt.status == "accepted"
    # The worker is gated; release it and wait for the process to finish.
    (run / "go").write_text("1", encoding="utf-8")
    wait_until(lambda: proc.poll() is not None, timeout=20)
    time.sleep(0.1)
    task = drive_until_terminal(supervisor, repo, "task-1")
    assert task.state == "SUCCESS"
    assert task.finalized_at is not None
    events = [row["event_type"] for row in repo.recent_events("task-1")]
    assert "SUCCESS" in events
    # Only one success notification even after additional ticks.
    supervisor.tick()
    supervisor.tick()
    rows = [r for r in repo.recent_notifications(50) if r["event_type"] == "SUCCESS"]
    assert len(rows) == 1
    archive = paths.archive / "task-1.json"
    assert archive.exists()
    snapshot = json.loads(archive.read_text(encoding="utf-8"))
    assert snapshot["state"] == "SUCCESS"


def test_failure_log(atw_home, repo, supervisor, spawn_worker, register_task):
    cfg, paths = atw_home
    proc, run = spawn_worker("failure", "supervisor-failure")
    _, receipt = register_task(proc, run, failure_log="CUDA out of memory")
    assert receipt.status == "accepted"
    (run / "go").write_text("1", encoding="utf-8")
    wait_until(lambda: proc.poll() is not None, timeout=20)
    task = drive_until_terminal(supervisor, repo, "task-1")
    assert task.state == "FAILED"
    assert "失败规则" in (task.finalize_reason or "")


def test_unknown_when_process_disappears_without_evidence(
    atw_home, repo, supervisor, spawn_worker, register_task
):
    cfg, paths = atw_home
    proc, run = spawn_worker("unknown", "supervisor-unknown")
    _, receipt = register_task(proc, run)
    assert receipt.status == "accepted"
    (run / "go").write_text("1", encoding="utf-8")
    wait_until(lambda: proc.poll() is not None, timeout=20)
    task = drive_until_terminal(supervisor, repo, "task-1")
    assert task.state == "UNKNOWN"
    assert task.state != "SUCCESS"


def test_old_result_file_does_not_trigger_success(
    atw_home, repo, supervisor, spawn_worker, register_task
):
    cfg, paths = atw_home
    proc, run = spawn_worker("long", "old-result")
    (run / "results").mkdir(exist_ok=True)
    result = run / "results" / "result.csv"
    result.write_text("old", encoding="utf-8")
    old = time.time() - 3600
    os.utime(result, (old, old))
    _, receipt = register_task(proc, run)
    assert receipt.status == "accepted"
    (run / "go").write_text("1", encoding="utf-8")
    time.sleep(0.2)
    with open(run / "run.log", "a", encoding="utf-8") as handle:
        handle.write("Simulation completed\n")
    proc.terminate()
    proc.wait(timeout=10)
    task = drive_until_terminal(supervisor, repo, "task-1")
    assert task.state == "UNKNOWN"


def test_hot_attach_existing_current_run_log(
    atw_home, repo, supervisor, spawn_worker, register_task
):
    cfg, paths = atw_home
    proc, run = spawn_worker("long", "hot")
    (run / "run.log").write_text("task started\nSimulation completed\n", encoding="utf-8")
    (run / "results").mkdir(exist_ok=True)
    (run / "results" / "result.csv").write_text("done", encoding="utf-8")
    _, receipt = register_task(proc, run)
    assert receipt.status == "accepted"
    time.sleep(0.2)
    supervisor.tick()
    assert repo.get_task("task-1").state == "MONITORING"
    proc.terminate()
    proc.wait(timeout=10)
    task = drive_until_terminal(supervisor, repo, "task-1")
    assert task.state == "SUCCESS"


def test_warning_duration_and_single_alert(atw_home, repo, supervisor, spawn_worker, register_task):
    cfg, paths = atw_home
    proc, run = spawn_worker("long", "warning")
    extra = {
        "warnings": [
            {"id": "cpu-idle", "kind": "cpu_below", "percent": 50.0, "duration_seconds": 1}
        ]
    }
    _, receipt = register_task(
        proc,
        run,
        extra_rules=extra,
        notifications={
            "on_accepted": True,
            "on_success": True,
            "on_failure": True,
            "on_unknown": True,
            "on_interrupted": True,
            "on_warning": True,
            "progress_interval_minutes": 0,
            "warning_repeat_minutes": 1,
        },
    )
    assert receipt.status == "accepted"
    (run / "go").write_text("1", encoding="utf-8")
    deadline = time.time() + 8
    warnings = []
    while time.time() < deadline:
        supervisor.tick()
        task = repo.get_task("task-1")
        if task.terminal:
            break
        warnings = [r for r in repo.recent_events("task-1") if r["event_type"] == "WARNING"]
        if warnings:
            break
        time.sleep(1.0)
    assert warnings, "warning was not emitted"
    assert len(warnings) == 1
    task = repo.get_task("task-1")
    assert task.state == "MONITORING"
    # No immediate repeat within the 1-minute dedupe window.
    supervisor.tick()
    warnings2 = [r for r in repo.recent_events("task-1") if r["event_type"] == "WARNING"]
    assert len(warnings2) == 1
    # Simulate the repeat window elapsing; exactly one new warning is emitted.
    runtime = dict(task.runtime)
    runtime["warnings"]["cpu-idle"]["last_sent_epoch"] = time.time() - 120
    with repo.db.transaction() as conn:
        repo.update_runtime(conn, "task-1", runtime, utc_now())
    time.sleep(1.1)
    supervisor.tick()
    warnings3 = [r for r in repo.recent_events("task-1") if r["event_type"] == "WARNING"]
    assert len(warnings3) == 2
    proc.terminate()
    proc.wait(timeout=10)


def test_rule_conflict_fails_closed(atw_home, repo, supervisor, spawn_worker, register_task):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "conflict")
    _, receipt = register_task(proc, run, failure_log="Simulation completed")
    assert receipt.status == "accepted"
    (run / "go").write_text("1", encoding="utf-8")
    wait_until(lambda: proc.poll() is not None, timeout=20)
    task = drive_until_terminal(supervisor, repo, "task-1")
    assert task.state == "FAILED"
    events = [r["event_type"] for r in repo.recent_events("task-1")]
    assert "RULE_CONFLICT" in events


def test_progress_event(atw_home, repo, supervisor, spawn_worker, register_task):
    cfg, paths = atw_home
    proc, run = spawn_worker("long", "progress")
    _, receipt = register_task(
        proc,
        run,
        notifications={
            "on_accepted": True,
            "on_success": True,
            "on_failure": True,
            "on_unknown": True,
            "on_interrupted": True,
            "on_warning": True,
            "progress_interval_minutes": 1,
            "warning_repeat_minutes": 30,
        },
    )
    assert receipt.status == "accepted"
    task = repo.get_task("task-1")
    runtime = dict(task.runtime)
    runtime["progress"] = {"last_epoch": time.time() - 120}
    with repo.db.transaction() as conn:
        repo.update_runtime(conn, "task-1", runtime, utc_now())
    supervisor.tick()
    events = [r for r in repo.recent_events("task-1") if r["event_type"] == "PROGRESS"]
    assert events
    proc.terminate()
    proc.wait(timeout=10)
