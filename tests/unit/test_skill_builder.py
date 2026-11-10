from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from atw.models.task import RegisterRequest, parse_request_json

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "atw-task-handoff" / "scripts" / "build_task_json.py"
PYTHON = getattr(sys, "_base_executable", sys.executable)


def _run(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [PYTHON, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _parse(result: subprocess.CompletedProcess) -> RegisterRequest:
    assert result.returncode == 0, result.stderr
    model, _ = parse_request_json(result.stdout.encode("utf-8"))
    assert isinstance(model, RegisterRequest)
    return model


def test_both_user_paths_are_used(tmp_path: Path) -> None:
    run = tmp_path / "run"
    result = _run(
        "--task-id",
        "t-both",
        "--name",
        "both paths",
        "--pid",
        "123",
        "--create-time",
        "1.5",
        "--run-dir",
        str(run),
        "--log",
        str(run / "custom.log"),
        "--result",
        str(run / "custom.csv"),
        "--ensure-dirs",
    )
    model = _parse(result)
    assert model.evidence is not None
    assert model.evidence.log is not None
    assert model.evidence.log.path == str(run / "custom.log")
    assert model.evidence.result_files == [str(run / "custom.csv")]
    assert run.exists()


def test_only_log_generates_result_default(tmp_path: Path) -> None:
    log = tmp_path / "only.log"
    result = _run(
        "--task-id",
        "t-log",
        "--name",
        "only log",
        "--pid",
        "124",
        "--create-time",
        "2.5",
        "--project-dir",
        str(tmp_path),
        "--log",
        str(log),
        "--ensure-dirs",
    )
    model = _parse(result)
    assert model.evidence is not None
    assert model.evidence.log is not None
    assert model.evidence.log.path == str(log)
    expected = str(tmp_path / "runs" / "t-log" / "results" / "result.csv")
    assert model.evidence.result_files == [expected]
    assert (tmp_path / "runs" / "t-log" / "results").is_dir()


def test_both_defaults_generate_independent_run_dir(tmp_path: Path) -> None:
    result = _run(
        "--task-id",
        "t-default",
        "--name",
        "defaults",
        "--pid",
        "125",
        "--create-time",
        "3.5",
        "--project-dir",
        str(tmp_path),
        "--ensure-dirs",
    )
    model = _parse(result)
    assert model.evidence is not None
    assert model.evidence.run_directory == str(tmp_path / "runs" / "t-default")
    assert model.evidence.log is not None
    assert model.evidence.log.path == str(tmp_path / "runs" / "t-default" / "run.log")
    assert model.evidence.result_files == [
        str(tmp_path / "runs" / "t-default" / "results" / "result.csv")
    ]
    assert (tmp_path / "runs" / "t-default" / "results").is_dir()


def test_match_target_and_output_file(tmp_path: Path) -> None:
    output = tmp_path / "out" / "task.json"
    result = _run(
        "--task-id",
        "t-match",
        "--name",
        "match",
        "--kind",
        "match",
        "--process-name",
        "python",
        "--cmdline-contains",
        "train.py",
        "--cmdline-contains=--run-id",
        "--project-dir",
        str(tmp_path),
        "--output",
        str(output),
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["target"]["kind"] == "match"
    assert data["target"]["cmdline_contains"] == ["train.py", "--run-id"]
    model, _ = parse_request_json(output.read_bytes())
    assert isinstance(model, RegisterRequest)


def test_relative_path_rejected(tmp_path: Path) -> None:
    result = _run(
        "--task-id",
        "t-bad",
        "--name",
        "bad",
        "--pid",
        "1",
        "--create-time",
        "1.0",
        "--log",
        "relative.log",
    )
    assert result.returncode != 0
    assert "绝对路径" in result.stderr
