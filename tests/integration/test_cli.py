from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atw.cli.app import app
from atw.models.task import BoundProcess, Receipt, ReceiptError
from atw.utils.timeutil import utc_now
from conftest import atomic_submit, make_register


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _invoke(runner: CliRunner, home: Path, *args: str):
    return runner.invoke(app, ["--home", str(home), *args])


def test_version(runner: CliRunner, atw_home) -> None:
    cfg, paths = atw_home
    result = _invoke(runner, paths.root, "--version")
    assert result.exit_code == 0
    assert "atw 1.0.0" in result.output


def test_validate_valid_and_invalid(runner: CliRunner, atw_home, tmp_path) -> None:
    cfg, paths = atw_home
    valid = tmp_path / "task.json"
    valid.write_text(
        (Path(__file__).resolve().parents[2] / "examples" / "task-pid.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    result = _invoke(runner, paths.root, "validate", str(valid), "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["valid"] is True
    assert data["operation"] == "register"

    invalid = tmp_path / "bad.json"
    invalid.write_text("{bad", encoding="utf-8")
    result = _invoke(runner, paths.root, "validate", str(invalid), "--json")
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["valid"] is False
    assert data["error"]["code"] == "INVALID_JSON"


def test_status_list_show(runner: CliRunner, atw_home, repo, task_service, spawn_worker, tmp_path):
    cfg, paths = atw_home
    proc, run = spawn_worker("long", "cli")
    data = make_register(
        tmp_path=tmp_path,
        proc=proc,
        request_id="req-cli",
        task_id="task-cli",
        run_dir=run,
        name="CLI task",
    )
    atomic_submit(paths, data)
    task_service.process_file(paths.inbox / "req-cli.json")

    result = _invoke(runner, paths.root, "status", "--json")
    assert result.exit_code == 0, result.output
    status = json.loads(result.stdout)
    assert status["protocol"] == "atw.status.v1"
    assert status["counts"]["monitoring"] == 1

    result = _invoke(runner, paths.root, "list", "--json")
    assert result.exit_code == 0
    listing = json.loads(result.stdout)
    assert listing["count"] == 1
    assert listing["tasks"][0]["task_id"] == "task-cli"

    result = _invoke(runner, paths.root, "show", "task-cli", "--json")
    assert result.exit_code == 0
    show = json.loads(result.stdout)
    assert show["task"]["task_id"] == "task-cli"
    assert show["revisions"][0]["revision"] == 1
    assert show["processes"][0]["pid"] == proc.pid


def test_receipt_accepted_rejected_timeout(runner: CliRunner, atw_home) -> None:
    cfg, paths = atw_home
    accepted = Receipt(
        request_id="req-ok",
        task_id="task-ok",
        revision=1,
        status="accepted",
        monitoring=True,
        bound_process=BoundProcess(pid=123, create_time_epoch=1.5),
        accepted_at=utc_now(),
        message="ok",
    )
    from atw.intake.receipts import ReceiptStore

    ReceiptStore(paths.receipts).write(accepted)
    result = _invoke(runner, paths.root, "receipt", "req-ok", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "accepted"

    rejected = Receipt(
        request_id="req-bad",
        task_id="task-bad",
        revision=1,
        status="rejected",
        monitoring=False,
        message="bad",
        error=ReceiptError(code="PROCESS_NOT_FOUND", message="missing"),
    )
    ReceiptStore(paths.receipts).write(rejected)
    result = _invoke(runner, paths.root, "receipt", "req-bad", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "rejected"

    result = _invoke(runner, paths.root, "receipt", "req-none", "--wait", "0", "--json")
    assert result.exit_code == 2
    data = json.loads(result.stdout)
    assert data["status"] == "timeout"
    assert data["error"]["code"] == "RECEIPT_TIMEOUT"


def test_notify_test_mock(runner: CliRunner, atw_home) -> None:
    cfg, paths = atw_home
    result = _invoke(runner, paths.root, "notify", "test", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["channel"] == "mock"
    assert paths.mock_notifications_file.exists()


def test_notifications_snapshot(runner: CliRunner, atw_home, repo) -> None:
    cfg, paths = atw_home
    from datetime import datetime

    with repo.db.transaction() as conn:
        repo.enqueue_notification(
            conn,
            task_id="t",
            event_type="SUCCESS",
            instance="final:1",
            channel="mock",
            title="[ATW] 成功",
            text="text",
            priority=0,
            now=datetime.now(UTC),
        )
    result = _invoke(runner, paths.root, "notifications", "--json")
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["counts"]["pending"] == 1
    assert data["recent"][0]["event_type"] == "SUCCESS"


def test_doctor_json_is_secret_free(runner: CliRunner, atw_home, monkeypatch) -> None:
    cfg, paths = atw_home
    monkeypatch.setenv(
        "ATW_DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=VERYSECRET"
    )
    monkeypatch.setenv("ATW_DINGTALK_SECRET", "SEC-VERYSECRET")
    result = _invoke(runner, paths.root, "doctor", "--json")
    assert result.exit_code in (0, 1), result.output
    assert "VERYSECRET" not in result.stdout
    data = json.loads(result.stdout)
    assert data["protocol"] == "atw.doctor.v1"
    assert data["overall"] in {"PASS", "WARN", "FAIL"}
