from __future__ import annotations

import time

from atw.health.bootid import BootIdentity
from atw.utils.timeutil import utc_now
from conftest import atomic_submit, make_register, wait_until


def _session(
    repo, *, session_id: str = "old-session", boot_id: str = "boot-1", clean: bool = False
):
    now = utc_now()
    repo.create_session(
        session_id=session_id,
        instance_id="test-node",
        boot_id=boot_id,
        boot_confidence="high",
        pid=123,
        started_at=now,
        service_version="test",
    )
    if clean:
        repo.close_session(session_id, now, clean=True)


def test_service_crash_recovers_monitoring_without_duplicate_accept(
    atw_home, repo, supervisor, spawn_worker, register_task
):
    proc, run = spawn_worker("long", "recover")
    _, receipt = register_task(proc, run)
    assert receipt.status == "accepted"
    (run / "go").write_text("1", encoding="utf-8")
    time.sleep(0.2)
    _session(repo, clean=False)
    result = supervisor.recover(BootIdentity("boot-1", "high", "test"))
    assert result["mode"] == "service_restart"
    assert result["resumed"] == 1
    task = repo.get_task("task-1")
    assert task.state == "MONITORING"
    events = [row["event_type"] for row in repo.recent_events(None, 200)]
    assert events.count("MONITOR_ACCEPTED") == 1
    assert events.count("SERVICE_RECOVERED") == 1
    assert "RECOVERY_RESUMED" in events
    # A second restart must not duplicate the recovery notification.
    supervisor.recover(BootIdentity("boot-1", "high", "test"))
    events2 = [row["event_type"] for row in repo.recent_events(None, 200)]
    assert events2.count("SERVICE_RECOVERED") == 1
    proc.terminate()
    proc.wait(timeout=10)


def test_clean_restart_recovers_quietly(atw_home, repo, supervisor, spawn_worker, register_task):
    proc, run = spawn_worker("long", "clean-restart")
    register_task(proc, run)
    (run / "go").write_text("1", encoding="utf-8")
    time.sleep(0.2)
    _session(repo, boot_id="boot-1", clean=True)
    result = supervisor.recover(BootIdentity("boot-1", "high", "test"))
    assert result["resumed"] == 1
    events = [row["event_type"] for row in repo.recent_events(None, 100)]
    assert "SERVICE_RECOVERED" not in events
    assert repo.get_task("task-1").state == "MONITORING"
    proc.terminate()
    proc.wait(timeout=10)


def test_host_reboot_marks_interrupted_and_never_reactivates(
    atw_home, repo, supervisor, spawn_worker, register_task, tmp_path
):
    proc, run = spawn_worker("long", "reboot")
    register_task(proc, run)
    _session(repo, boot_id="boot-1", clean=False)
    result = supervisor.recover(BootIdentity("boot-2", "high", "test"))
    assert result["host_reboot"] is True
    assert result["interrupted"] == 1
    task = repo.get_task("task-1")
    assert task.state == "INTERRUPTED"
    assert task.finalized_at is not None
    assert (atw_home[1].archive / "task-1.json").exists()
    events = [row["event_type"] for row in repo.recent_events("task-1", 100)]
    assert "INTERRUPTED" in events
    # Recovered service sees no monitoring task to reactivate.
    result2 = supervisor.recover(BootIdentity("boot-2", "high", "test"))
    assert result2["interrupted"] == 0
    assert repo.get_task("task-1").state == "INTERRUPTED"
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=10)


def test_process_finished_while_service_down_uses_evidence(
    atw_home, repo, supervisor, spawn_worker, register_task
):
    proc, run = spawn_worker("success", "finished-down")
    register_task(proc, run)
    (run / "go").write_text("1", encoding="utf-8")
    wait_until(lambda: proc.poll() is not None, timeout=20)
    time.sleep(0.1)
    _session(repo, boot_id="boot-1", clean=False)
    result = supervisor.recover(BootIdentity("boot-1", "high", "test"))
    assert result["finalized"] == 1
    task = repo.get_task("task-1")
    assert task.state == "SUCCESS"


def test_first_install_has_no_recovery_notification(atw_home, repo, supervisor):
    result = supervisor.recover(BootIdentity("boot-1", "high", "test"))
    assert result["mode"] == "first_install"
    assert repo.recent_events(None, 10) == []


def test_terminal_task_is_not_reactivated(
    atw_home, repo, supervisor, spawn_worker, register_task, task_service, tmp_path
):
    proc, run = spawn_worker("success", "terminal")
    _, receipt = register_task(proc, run)
    create_time = receipt.bound_process.create_time_epoch
    (run / "go").write_text("1", encoding="utf-8")
    wait_until(lambda: proc.poll() is not None, timeout=20)
    from conftest import drive_until_terminal

    task = drive_until_terminal(supervisor, repo, "task-1")
    assert task.state == "SUCCESS"
    _session(repo, boot_id="boot-1", clean=False)
    supervisor.recover(BootIdentity("boot-1", "high", "test"))
    assert repo.get_task("task-1").state == "SUCCESS"
    # A new register for the same terminal task_id must be rejected.
    data = make_register(
        tmp_path=tmp_path,
        proc=proc,
        request_id="req-reopen",
        task_id="task-1",
        run_dir=run,
        create_time=create_time,
    )
    path = atomic_submit(atw_home[1], data)
    reopen = task_service.process_file(path)
    assert reopen.status == "rejected"
    assert reopen.error.code == "TASK_FINALIZED"
