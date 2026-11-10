from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from atw.errors import INVALID_JSON, SCHEMA_VALIDATION_FAILED, UNSUPPORTED_PROTOCOL, AtwError
from atw.models.task import (
    Receipt,
    RegisterRequest,
    UpdateRequest,
    parse_request_json,
)
from atw.schema_export import check_schemas, schemas

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"


def _read(name: str) -> bytes:
    return (EXAMPLES / name).read_bytes()


def test_committed_schemas_match_models() -> None:
    assert check_schemas() == []


def test_schemas_are_valid_draft_2020_12() -> None:
    for schema in schemas().values():
        Draft202012Validator.check_schema(schema)


def test_task_examples_validate_against_committed_schema() -> None:
    schema = json.loads((ROOT / "schemas" / "task-v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for name in ("task-pid.json", "task-match.json", "task-update.json"):
        data = json.loads(_read(name))
        errors = list(validator.iter_errors(data))
        assert not errors, f"{name}: {[e.message for e in errors]}"
        model, _ = parse_request_json(_read(name))
        assert isinstance(model, (RegisterRequest, UpdateRequest))


def test_receipt_examples_validate() -> None:
    schema = json.loads((ROOT / "schemas" / "receipt-v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for name in ("receipt-accepted.json", "receipt-rejected.json"):
        data = json.loads(_read(name))
        assert not list(validator.iter_errors(data))
        Receipt.model_validate(data)


def test_pid_example_uses_full_identity() -> None:
    model, _ = parse_request_json(_read("task-pid.json"))
    assert isinstance(model, RegisterRequest)
    assert model.target.kind == "pid"
    assert model.target.create_time_epoch == pytest.approx(1794254400.125)
    assert model.rules.success is not None


def test_invalid_json_rejected() -> None:
    with pytest.raises(AtwError) as exc:
        parse_request_json(b"{not json")
    assert exc.value.code == INVALID_JSON


def test_unsupported_protocol() -> None:
    data = json.loads(_read("task-pid.json"))
    data["protocol"] = "atw.task.v2"
    with pytest.raises(AtwError) as exc:
        parse_request_json(json.dumps(data).encode())
    assert exc.value.code == UNSUPPORTED_PROTOCOL


def test_pid_requires_create_time() -> None:
    data = json.loads(_read("task-pid.json"))
    del data["target"]["create_time_epoch"]
    with pytest.raises(AtwError) as exc:
        parse_request_json(json.dumps(data).encode())
    assert exc.value.code == SCHEMA_VALIDATION_FAILED
    assert "create_time_epoch" in exc.value.message


def test_extra_fields_rejected() -> None:
    data = json.loads(_read("task-pid.json"))
    data["unexpected"] = True
    with pytest.raises(AtwError) as exc:
        parse_request_json(json.dumps(data).encode())
    assert exc.value.code == SCHEMA_VALIDATION_FAILED


def test_rule_irrelevant_fields_rejected() -> None:
    data = json.loads(_read("task-pid.json"))
    data["rules"]["success"]["all"][0]["percent"] = 10
    with pytest.raises(AtwError) as exc:
        parse_request_json(json.dumps(data).encode())
    assert exc.value.code == SCHEMA_VALIDATION_FAILED


def test_update_changes_must_be_non_empty() -> None:
    data = json.loads(_read("task-update.json"))
    data["changes"] = {}
    with pytest.raises(AtwError) as exc:
        parse_request_json(json.dumps(data).encode())
    assert exc.value.code == SCHEMA_VALIDATION_FAILED


def test_update_cannot_change_target() -> None:
    data = json.loads(_read("task-update.json"))
    data["changes"]["target"] = {"kind": "pid", "pid": 1, "create_time_epoch": 1.0}
    with pytest.raises(AtwError) as exc:
        parse_request_json(json.dumps(data).encode())
    assert exc.value.code == SCHEMA_VALIDATION_FAILED


def test_rejected_receipt_requires_error() -> None:
    data = json.loads(_read("receipt-rejected.json"))
    del data["error"]
    with pytest.raises(ValidationError):
        Receipt.model_validate(data)


def test_rule_depth_limit() -> None:
    data = json.loads(_read("task-pid.json"))
    node = {"kind": "log_contains", "value": "x"}
    for _ in range(9):
        node = {"not": node}
    data["rules"]["success"] = node
    with pytest.raises(AtwError) as exc:
        parse_request_json(json.dumps(data).encode())
    assert exc.value.code == SCHEMA_VALIDATION_FAILED
