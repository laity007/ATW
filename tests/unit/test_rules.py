from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atw.models.task import (
    Condition,
    FailureRule,
    RulesSpec,
    WarningRule,
)
from atw.monitoring.evidence import EvidenceSnapshot, FileEvidence, LogEvidence
from atw.monitoring.gpu import GpuProcess, GpuSnapshot
from atw.monitoring.process import ProcessSnapshot
from atw.rules.engine import FALSE, TRUE, UNKNOWN, EvalContext, RuleEngine


def proc(
    *,
    root_alive: bool = False,
    tree_alive: bool = False,
    complete: bool = True,
    cpu: float | None = 1.0,
    cpu_available: bool = True,
) -> ProcessSnapshot:
    return ProcessSnapshot(
        root_alive=root_alive,
        tree_alive=tree_alive,
        tree_exited=not tree_alive,
        tree_complete=complete,
        cpu_percent=cpu,
        cpu_available=cpu_available,
        memory_rss=123,
        members=[],
    )


def evidence(
    *,
    matched: set[str] | None = None,
    log_available: bool = True,
    log_fresh: bool = True,
    log_age: float = 0.0,
    files: dict | None = None,
    log_error: str | None = None,
) -> EvidenceSnapshot:
    now = datetime.now(UTC)
    return EvidenceSnapshot(
        log=LogEvidence(
            path="/tmp/run.log",
            available=log_available,
            fresh=log_fresh,
            size=100,
            mtime=(now - timedelta(seconds=log_age)).timestamp(),
            matched=matched or set(),
            error=log_error,
        ),
        files=files or {},
    )


def ctx(
    *,
    process: ProcessSnapshot | None = None,
    ev: EvidenceSnapshot | None = None,
    gpu: GpuSnapshot | None = None,
    elapsed: float = 10.0,
    now: datetime | None = None,
    bound_pids: set[int] | None = None,
) -> EvalContext:
    now = now or datetime.now(UTC)
    return EvalContext(
        now=now,
        now_epoch=now.timestamp(),
        started_at=now - timedelta(seconds=elapsed),
        elapsed_seconds=elapsed,
        process=process or proc(),
        evidence=ev or evidence(),
        gpu=gpu,
        bound_pids=bound_pids or {10},
    )


def test_all_conditions_true_is_true() -> None:
    rules = RulesSpec.model_validate(
        {
            "success": {
                "all": [
                    {"kind": "process_tree_exited"},
                    {"kind": "log_contains", "value": "done"},
                    {"kind": "file_exists_fresh", "path": "/tmp/r.csv"},
                ]
            }
        }
    )
    ev = evidence(
        matched={"done"},
        files={"/tmp/r.csv": FileEvidence("/tmp/r.csv", True, True, 20, 1.0)},
    )
    result = RuleEngine().evaluate_success("t", rules, ctx(ev=ev))
    assert result.truth == TRUE
    assert any("日志命中" in d for d in result.details)


def test_any_condition() -> None:
    rules = RulesSpec.model_validate(
        {
            "failure": {
                "any": [
                    {"kind": "log_contains", "value": "OOM"},
                    {"kind": "log_contains", "value": "Traceback"},
                ]
            }
        }
    )
    ev = evidence(matched={"Traceback"})
    failure, mode = RuleEngine().evaluate_failure("t", rules, ctx(ev=ev))
    assert failure.truth == TRUE
    assert mode == "on_process_exit"


def test_not_condition() -> None:
    rules = RulesSpec.model_validate(
        {
            "success": {
                "all": [
                    {"kind": "process_tree_exited"},
                    {"kind": "log_contains", "value": "done"},
                    {"not": {"kind": "log_contains", "value": "ERROR"}},
                ]
            }
        }
    )
    result = RuleEngine().evaluate_success("t", rules, ctx(ev=evidence(matched={"done"})))
    assert result.truth == TRUE


def test_unknown_propagates_through_all() -> None:
    rules = RulesSpec.model_validate(
        {
            "success": {
                "all": [
                    {"kind": "process_tree_exited"},
                    {"kind": "log_contains", "value": "done"},
                ]
            }
        }
    )
    ev = evidence(log_available=False, log_fresh=False, log_error="missing")
    result = RuleEngine().evaluate_success("t", rules, ctx(ev=ev))
    assert result.truth == UNKNOWN


def test_process_only_success_is_unknown() -> None:
    rules = RulesSpec.model_validate({"success": {"all": [{"kind": "process_tree_exited"}]}})
    result = RuleEngine().evaluate_success("t", rules, ctx())
    assert result.truth == UNKNOWN
    assert any("正向证据" in d for d in result.details)


def test_cpu_unavailable_is_unknown_not_zero() -> None:
    rules = RulesSpec.model_validate(
        {"warnings": [{"id": "idle", "kind": "cpu_below", "percent": 2.0}]}
    )
    warning = rules.warnings[0]
    result = RuleEngine().evaluate_warning(
        "t", warning, ctx(process=proc(cpu=None, cpu_available=False))
    )
    assert result.truth == UNKNOWN


def test_gpu_unavailable_is_unknown() -> None:
    cond = Condition.model_validate({"kind": "gpu_util_below", "percent": 10})
    rules = RulesSpec(
        success=Condition.model_validate({"all": [{"kind": "log_contains", "value": "x"}, cond]})
    )
    result = RuleEngine().evaluate_success(
        "t",
        rules,
        ctx(ev=evidence(matched={"x"}), gpu=GpuSnapshot(available=False, reason="no nvml")),
    )
    assert result.truth == UNKNOWN


def test_gpu_memory_available() -> None:
    cond = Condition.model_validate({"kind": "gpu_memory_above", "mb": 50})
    rules = RulesSpec(
        warnings=[
            WarningRule.model_validate(
                {"id": "mem", **cond.model_dump(by_alias=True, exclude_none=True)}
            )
        ]
    )
    gpu = GpuSnapshot(
        available=True,
        devices=[],
        processes=[GpuProcess(pid=10, gpu_uuid="GPU-1", memory_mb=100.0)],
        per_process_utilization_available=False,
    )
    result = RuleEngine().evaluate_warning("t", rules.warnings[0], ctx(gpu=gpu))
    assert result.truth == TRUE


def test_gpu_process_query_unavailable_is_unknown_not_zero() -> None:
    warning = WarningRule(id="mem", kind="gpu_memory_above", mb=50)
    gpu = GpuSnapshot(
        available=True,
        devices=[],
        processes=[],
        per_process_utilization_available=False,
        process_metrics_available=False,
    )
    result = RuleEngine().evaluate_warning("t", warning, ctx(gpu=gpu))
    assert result.truth == UNKNOWN


def test_gpu_process_utilization_never_faked() -> None:
    cond = Condition.model_validate({"kind": "gpu_util_below", "percent": 10})
    warning = WarningRule(id="util", kind=cond.kind, percent=10)
    gpu = GpuSnapshot(
        available=True,
        devices=[],
        processes=[GpuProcess(pid=10, gpu_uuid="GPU-1", memory_mb=100.0)],
        per_process_utilization_available=False,
    )
    result = RuleEngine().evaluate_warning("t", warning, ctx(gpu=gpu))
    assert result.truth == UNKNOWN


def test_sustained_condition_requires_duration() -> None:
    engine = RuleEngine()
    warning = WarningRule(id="idle", kind="cpu_below", percent=50.0, duration_seconds=5)
    t0 = datetime.now(UTC)
    first = engine.evaluate_warning("t", warning, ctx(process=proc(cpu=1.0), now=t0))
    assert first.truth == FALSE
    still = engine.evaluate_warning(
        "t", warning, ctx(process=proc(cpu=1.0), now=t0 + timedelta(seconds=3))
    )
    assert still.truth == FALSE
    ready = engine.evaluate_warning(
        "t", warning, ctx(process=proc(cpu=1.0), now=t0 + timedelta(seconds=6))
    )
    assert ready.truth == TRUE


def test_sustained_condition_resets_on_false() -> None:
    engine = RuleEngine()
    warning = WarningRule(id="idle", kind="cpu_below", percent=5.0, duration_seconds=5)
    t0 = datetime.now(UTC)
    engine.evaluate_warning("t", warning, ctx(process=proc(cpu=1.0), now=t0))
    engine.evaluate_warning(
        "t", warning, ctx(process=proc(cpu=90.0), now=t0 + timedelta(seconds=3))
    )
    after = engine.evaluate_warning(
        "t", warning, ctx(process=proc(cpu=1.0), now=t0 + timedelta(seconds=4))
    )
    assert after.truth == FALSE


def test_elapsed_above() -> None:
    cond = Condition.model_validate({"kind": "elapsed_above", "seconds": 5})
    rules = RulesSpec(
        success=Condition.model_validate({"all": [{"kind": "log_contains", "value": "x"}, cond]})
    )
    result = RuleEngine().evaluate_success("t", rules, ctx(ev=evidence(matched={"x"}), elapsed=10))
    assert result.truth == TRUE


def test_log_not_updated() -> None:
    cond = Condition.model_validate({"kind": "log_not_updated", "seconds": 50})
    warning = WarningRule(id="stale", **cond.model_dump(by_alias=True, exclude_none=True))
    result = RuleEngine().evaluate_warning("t", warning, ctx(ev=evidence(log_age=100)))
    assert result.truth == TRUE


def test_file_size_requires_freshness() -> None:
    path = "/tmp/old.bin"
    old = FileEvidence(path, True, False, 10_000, 1.0, "文件早于本次运行")
    rules = RulesSpec(
        success=Condition.model_validate(
            {"all": [{"kind": "file_size_above", "path": path, "bytes": 10}]}
        )
    )
    result = RuleEngine().evaluate_success("t", rules, ctx(ev=evidence(files={path: old})))
    assert result.truth == FALSE  # old file is not this run's completion evidence
    cond = Condition.model_validate({"kind": "file_size_above", "path": path, "bytes": 10})
    warning = WarningRule(id="size", **cond.model_dump(by_alias=True, exclude_none=True))
    assert (
        RuleEngine().evaluate_warning("t", warning, ctx(ev=evidence(files={path: old}))).truth
        == FALSE
    )


def test_conflict_detected() -> None:
    rules = RulesSpec(
        success=Condition.model_validate(
            {"all": [{"kind": "process_tree_exited"}, {"kind": "log_contains", "value": "done"}]}
        ),
        failure=FailureRule.model_validate(
            {"any": [{"kind": "log_contains", "value": "done"}], "finalize": "immediate"}
        ),
    )
    engine = RuleEngine()
    success = engine.evaluate_success("t", rules, ctx(ev=evidence(matched={"done"})))
    failure, _ = engine.evaluate_failure("t", rules, ctx(ev=evidence(matched={"done"})))
    assert success.truth == TRUE
    assert failure.truth == TRUE
