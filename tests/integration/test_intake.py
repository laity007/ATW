from __future__ import annotations

import json
import time

import psutil

from atw.errors import (
    AMBIGUOUS_PROCESS,
    PROCESS_IDENTITY_MISMATCH,
    PROCESS_NOT_FOUND,
    REQUEST_CONFLICT,
    REQUEST_EXPIRED,
    REVISION_CONFLICT,
)
from atw.intake.scanner import scan_inbox
from atw.utils.timeutil import utc_now
from conftest import atomic_submit, make_register


def _register(
    task_service, paths, proc, run, tmp_path, request_id="req-1", task_id="task-1", **kwargs
):
    data = make_register(
        tmp_path=tmp_path,
        proc=proc,
        request_id=request_id,
        task_id=task_id,
        run_dir=run,
        **kwargs,
    )
    path = atomic_submit(paths, data)
    return data, task_service.process_file(path)


def test_valid_registration_creates_task_and_receipt(
    atw_home, repo, task_service, spawn_worker, tmp_path
):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "valid")
    data, receipt = _register(task_service, paths, proc, run, tmp_path)
    assert receipt is not None
    assert receipt.status == "accepted"
    assert receipt.monitoring is True
    assert receipt.bound_process is not None
    assert receipt.bound_process.pid == proc.pid
    assert receipt.request_id == "req-1"
    assert (paths.accepted / "req-1.json").exists()
    receipt_file = paths.receipts / "req-1.json"
    assert receipt_file.exists()
    stored = json.loads(receipt_file.read_text(encoding="utf-8"))
    assert stored["status"] == "accepted"
    assert stored["bound_process"]["pid"] == proc.pid
    task = repo.get_task("task-1")
    assert task is not None and task.state == "MONITORING"
    assert repo.revisions("task-1")[0]["operation"] == "register"
    counts = repo.outbox_counts()
    assert counts.get("pending", 0) >= 1


def test_duplicate_request_is_idempotent(atw_home, repo, task_service, spawn_worker, tmp_path):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "dup")
    data, first = _register(task_service, paths, proc, run, tmp_path)
    second_path = atomic_submit(paths, data)
    second = task_service.process_file(second_path)
    assert first is not None and second is not None
    assert second.status == first.status == "accepted"
    assert second.bound_process == first.bound_process
    assert repo.counts()["tasks"] == 1
    assert repo.counts()["requests"] == 1


def test_same_request_id_different_content_conflicts(
    atw_home, repo, task_service, spawn_worker, tmp_path
):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "conflict")
    data, _ = _register(task_service, paths, proc, run, tmp_path)
    changed = json.loads(json.dumps(data))
    changed["task"]["name"] = "different content"
    path = atomic_submit(paths, changed)
    receipt = task_service.process_file(path)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == REQUEST_CONFLICT
    task = repo.get_task("task-1")
    assert task is not None and task.name == "unit test task"
    assert repo.counts()["tasks"] == 1


def test_invalid_json_rejected_without_task(atw_home, repo, task_service):
    cfg, paths = atw_home
    bad = paths.inbox / "bad-json.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    receipt = task_service.process_file(bad)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == "INVALID_JSON"
    assert repo.counts()["tasks"] == 0
    assert (paths.rejected / "bad-json.json").exists()
    assert (paths.receipts / "bad-json.json").exists()


def test_schema_error_rejected(atw_home, repo, task_service, spawn_worker, tmp_path):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "schema")
    data = make_register(
        tmp_path=tmp_path, proc=proc, request_id="req-schema", task_id="task-schema", run_dir=run
    )
    del data["target"]["create_time_epoch"]
    path = atomic_submit(paths, data)
    receipt = task_service.process_file(path)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == "SCHEMA_VALIDATION_FAILED"
    assert repo.counts()["tasks"] == 0


def test_tmp_file_is_ignored(atw_home, task_service):
    cfg, paths = atw_home
    tmp = paths.inbox / "notready.json.tmp"
    tmp.write_text("{}", encoding="utf-8")
    assert scan_inbox(paths) == []
    assert repo_tasks(task_service) == 0


def repo_tasks(task_service) -> int:
    return task_service.repo.counts()["tasks"]


def test_expired_request_rejected(atw_home, repo, task_service, spawn_worker, tmp_path):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "expired")
    data = make_register(
        tmp_path=tmp_path,
        proc=proc,
        request_id="req-expired",
        task_id="task-expired",
        run_dir=run,
        expires_delta=-5,
    )
    path = atomic_submit(paths, data)
    receipt = task_service.process_file(path)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == REQUEST_EXPIRED
    assert repo.get_task("task-expired") is None


def test_filename_request_id_mismatch(atw_home, repo, task_service, spawn_worker, tmp_path):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "name")
    data = make_register(
        tmp_path=tmp_path, proc=proc, request_id="req-content", task_id="task-name", run_dir=run
    )
    wrong = paths.inbox / "req-filename.json"
    wrong.write_text(json.dumps(data), encoding="utf-8")
    receipt = task_service.process_file(wrong)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == "INVALID_REQUEST_ID"
    assert repo.get_task("task-name") is None


def test_process_not_found(atw_home, task_service, spawn_worker, tmp_path):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "missing")
    data = make_register(
        tmp_path=tmp_path,
        proc=proc,
        request_id="req-missing",
        task_id="task-missing",
        run_dir=run,
    )
    data["target"]["pid"] = 99_999_999
    path = atomic_submit(paths, data)
    receipt = task_service.process_file(path)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == PROCESS_NOT_FOUND


def test_process_identity_mismatch(atw_home, task_service, spawn_worker, tmp_path):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "identity")
    actual = psutil.Process(proc.pid).create_time()
    data = make_register(
        tmp_path=tmp_path,
        proc=proc,
        request_id="req-mismatch",
        task_id="task-mismatch",
        run_dir=run,
        create_time=actual + 100,
    )
    path = atomic_submit(paths, data)
    receipt = task_service.process_file(path)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == PROCESS_IDENTITY_MISMATCH


def test_ambiguous_match(atw_home, task_service, tmp_path):
    import subprocess
    import sys

    marker = "atw-intake-ambiguous-xyz"
    code = f"import time; time.sleep(20)  # {marker}"
    procs = [
        subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL) for _ in range(2)
    ]
    try:
        time.sleep(0.2)
        name = psutil.Process(procs[0].pid).name()
        run = tmp_path / "runs" / "ambiguous"
        run.mkdir(parents=True)
        data = make_register(
            tmp_path=tmp_path,
            proc=procs[0],
            request_id="req-ambiguous",
            task_id="task-ambiguous",
            run_dir=run,
        )
        data["target"] = {
            "kind": "match",
            "name": name,
            "cmdline_contains": [marker],
            "match_mode": "unique",
            "tree": {"enabled": True},
        }
        cfg, paths = atw_home
        path = atomic_submit(paths, data)
        receipt = task_service.process_file(path)
        assert receipt is not None and receipt.status == "rejected"
        assert receipt.error is not None and receipt.error.code == AMBIGUOUS_PROCESS
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _update_request(task_id: str, revision: int, expected: int, *, expired: bool = False) -> dict:
    from datetime import timedelta

    return {
        "protocol": "atw.task.v1",
        "operation": "update",
        "request_id": f"req-update-{revision}",
        "task_id": task_id,
        "revision": revision,
        "expected_revision": expected,
        "expires_at": (utc_now() + timedelta(seconds=(-5 if expired else 600)))
        .isoformat()
        .replace("+00:00", "Z"),
        "changes": {
            "notifications": {
                "on_accepted": True,
                "on_success": True,
                "on_failure": True,
                "on_unknown": True,
                "on_interrupted": True,
                "on_warning": True,
                "progress_interval_minutes": 60,
                "warning_repeat_minutes": 30,
            }
        },
    }


def test_update_revision_and_history(atw_home, repo, task_service, spawn_worker, tmp_path):
    cfg, paths = atw_home
    proc, run = spawn_worker("long", "update")
    _register(task_service, paths, proc, run, tmp_path)
    update = _update_request("task-1", 2, 1)
    path = atomic_submit(paths, update)
    receipt = task_service.process_file(path)
    assert receipt is not None and receipt.status == "accepted"
    task = repo.get_task("task-1")
    assert task is not None
    assert task.revision == 2
    assert task.notifications.progress_interval_minutes == 60
    revisions = repo.revisions("task-1")
    assert [row["revision"] for row in revisions] == [1, 2]
    assert revisions[0]["operation"] == "register"


def test_revision_conflict_preserves_old_config(
    atw_home, repo, task_service, spawn_worker, tmp_path
):
    cfg, paths = atw_home
    proc, run = spawn_worker("long", "revision")
    _register(task_service, paths, proc, run, tmp_path)
    update = _update_request("task-1", 3, 1)
    path = atomic_submit(paths, update)
    receipt = task_service.process_file(path)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == REVISION_CONFLICT
    task = repo.get_task("task-1")
    assert task is not None and task.revision == 1


def test_expired_update_does_not_affect_accepted_task(
    atw_home, repo, task_service, spawn_worker, tmp_path
):
    cfg, paths = atw_home
    proc, run = spawn_worker("long", "expired-update")
    _register(task_service, paths, proc, run, tmp_path)
    update = _update_request("task-1", 2, 1, expired=True)
    path = atomic_submit(paths, update)
    receipt = task_service.process_file(path)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == REQUEST_EXPIRED
    task = repo.get_task("task-1")
    assert task is not None and task.revision == 1
