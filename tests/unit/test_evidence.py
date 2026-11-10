from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from atw.models.task import (
    Evidence,
    LogSpec,
    NotificationSettings,
    RulesSpec,
    Target,
)
from atw.monitoring.evidence import EvidenceMonitor
from atw.storage.records import TaskRecord


def make_record(
    run_dir: Path, *, started_ago: float = 10.0, rules: dict | None = None
) -> TaskRecord:
    target = Target(kind="pid", pid=1234, create_time_epoch=1.0)
    return TaskRecord(
        task_id="t1",
        name="test",
        project=None,
        description=None,
        run_started_at=datetime.now(UTC) - timedelta(seconds=started_ago),
        state="MONITORING",
        revision=1,
        target=target,
        evidence=Evidence(
            run_directory=str(run_dir),
            log=LogSpec(path=str(run_dir / "run.log")),
            result_files=[str(run_dir / "results" / "result.csv")],
        ),
        rules=RulesSpec.model_validate(
            rules
            or {
                "success": {
                    "all": [
                        {"kind": "log_contains", "value": "Simulation completed"},
                        {
                            "kind": "file_exists_fresh",
                            "path": str(run_dir / "results" / "result.csv"),
                        },
                    ]
                }
            }
        ),
        notifications=NotificationSettings(),
        runtime={},
        accepted_at=datetime.now(UTC) - timedelta(seconds=started_ago),
    )


def test_hot_attach_reads_existing_current_run_log(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.log").write_text("start\nSimulation completed\n", encoding="utf-8")
    monitor = EvidenceMonitor(roots=[tmp_path])
    record = make_record(run)
    snap, runtime = monitor.refresh(record, record.runtime)
    assert snap.log.available and snap.log.fresh
    assert "Simulation completed" in snap.log.matched
    assert snap.log_contains("Simulation completed") is True
    assert runtime["evidence"]["log"]["matched"] == ["Simulation completed"]


def test_old_log_content_is_not_used(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    log = run / "run.log"
    log.write_text("Simulation completed\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(log, (old, old))
    record = make_record(run)
    monitor = EvidenceMonitor(roots=[tmp_path])
    snap, _ = monitor.refresh(record, record.runtime)
    assert not snap.log.fresh
    assert snap.log.matched == set()
    assert snap.log_contains("Simulation completed") is None


def test_log_append_and_multibyte(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    log = run / "run.log"
    log.write_text("启动\n", encoding="utf-8")
    monitor = EvidenceMonitor(roots=[tmp_path])
    record = make_record(run)
    snap1, runtime = monitor.refresh(record, record.runtime)
    assert not snap1.log.matched
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("Simulation completed ✓\n")
    snap2, _ = monitor.refresh(record, runtime)
    assert "Simulation completed" in snap2.log.matched


def test_log_truncation_keeps_matches(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    log = run / "run.log"
    log.write_text("Simulation completed\n", encoding="utf-8")
    monitor = EvidenceMonitor(roots=[tmp_path])
    record = make_record(run)
    _, runtime = monitor.refresh(record, record.runtime)
    log.write_text("new content\n", encoding="utf-8")
    snap, _ = monitor.refresh(record, runtime)
    assert "Simulation completed" in snap.log.matched
    assert snap.log.truncated is True or snap.log.rotated is True


def test_log_rotation_keeps_matches(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    log = run / "run.log"
    log.write_text("Simulation completed\n", encoding="utf-8")
    monitor = EvidenceMonitor(roots=[tmp_path])
    record = make_record(run)
    _, runtime = monitor.refresh(record, record.runtime)
    rotated = run / "run.log.1"
    os.replace(log, rotated)
    log.write_text("rotated fresh content\n", encoding="utf-8")
    snap, _ = monitor.refresh(record, runtime)
    assert "Simulation completed" in snap.log.matched


def test_result_file_freshness(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "results").mkdir(parents=True)
    result = run / "results" / "result.csv"
    result.write_text("ok", encoding="utf-8")
    monitor = EvidenceMonitor(roots=[tmp_path])
    record = make_record(run)
    snap, _ = monitor.refresh(record, record.runtime)
    assert snap.file(str(result)).exists
    assert snap.file(str(result)).fresh
    old = time.time() - 3600
    os.utime(result, (old, old))
    snap2, _ = monitor.refresh(record, record.runtime)
    assert not snap2.file(str(result)).fresh
    assert snap2.file(str(result)).exists


def test_log_age_and_not_updated(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    log = run / "run.log"
    log.write_text("x\n", encoding="utf-8")
    age = 120
    old = time.time() - age
    os.utime(log, (old, old))
    monitor = EvidenceMonitor(roots=[tmp_path])
    record = make_record(run, started_ago=600)
    snap, _ = monitor.refresh(record, record.runtime)
    assert snap.log.fresh
    assert snap.log.mtime is not None
    assert time.time() - snap.log.mtime >= age - 5
