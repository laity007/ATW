from __future__ import annotations

import json
from pathlib import Path

from atw.errors import SCHEMA_VALIDATION_FAILED, UNSAFE_PATH
from atw.models.task import parse_request_json
from conftest import atomic_submit, make_register


def test_shell_command_field_is_rejected(atw_home, repo, task_service, spawn_worker, tmp_path):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "inject")
    data = make_register(
        tmp_path=tmp_path,
        proc=proc,
        request_id="req-inject",
        task_id="task-inject",
        run_dir=run,
    )
    data["command"] = "rm -rf /tmp/atw-should-not-run"
    data["rules"]["success"]["all"].append({"kind": "shell", "value": "rm -rf /"})
    path = atomic_submit(paths, data)
    receipt = task_service.process_file(path)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == SCHEMA_VALIDATION_FAILED
    assert repo.get_task("task-inject") is None
    assert not Path("/tmp/atw-should-not-run").exists()


def test_evidence_outside_allowed_roots_rejected(
    atw_home, repo, task_service, spawn_worker, tmp_path
):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "outside")
    data = make_register(
        tmp_path=tmp_path,
        proc=proc,
        request_id="req-outside",
        task_id="task-outside",
        run_dir=run,
        evidence_paths=("/etc/passwd", "/etc/shadow"),
    )
    path = atomic_submit(paths, data)
    receipt = task_service.process_file(path)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == UNSAFE_PATH
    assert repo.get_task("task-outside") is None


def test_path_traversal_is_rejected(atw_home, repo, task_service, spawn_worker, tmp_path):
    cfg, paths = atw_home
    proc, run = spawn_worker("success", "traversal")
    data = make_register(
        tmp_path=tmp_path,
        proc=proc,
        request_id="req-traversal",
        task_id="task-traversal",
        run_dir=run,
        evidence_paths=(
            str(run / ".." / ".." / ".." / "etc" / "passwd"),
            str(run / "result.csv"),
        ),
    )
    path = atomic_submit(paths, data)
    receipt = task_service.process_file(path)
    assert receipt is not None and receipt.status == "rejected"
    assert receipt.error is not None and receipt.error.code == UNSAFE_PATH


def test_rule_file_path_is_checked(tmp_path):
    data = json.loads(
        (Path(__file__).resolve().parents[2] / "examples" / "task-pid.json").read_text(
            encoding="utf-8"
        )
    )
    data["rules"]["success"]["all"].append({"kind": "file_exists_fresh", "path": "/etc/passwd"})
    # Schema-valid; the path policy is enforced at intake with configured roots.
    model, _ = parse_request_json(json.dumps(data).encode())
    assert model.operation == "register"
