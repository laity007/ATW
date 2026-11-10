"""Three-state rule engine.

TRUE/FALSE/UNKNOWN with AND/OR/NOT and ``for_seconds``.  Unavailable metrics are
UNKNOWN, never zero.  Sustained-condition timers are in-memory and reset when
the rule semantics change or the service restarts (conservative: an alert may be
delayed, never fabricated).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from atw.models.task import Condition, FailureRule, RulesSpec, WarningRule
from atw.monitoring.evidence import EvidenceSnapshot
from atw.monitoring.gpu import GpuSnapshot
from atw.monitoring.process import ProcessSnapshot

TRUE = "TRUE"
FALSE = "FALSE"
UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class EvalContext:
    now: datetime
    now_epoch: float
    started_at: datetime | None
    elapsed_seconds: float
    process: ProcessSnapshot
    evidence: EvidenceSnapshot
    gpu: GpuSnapshot | None
    bound_pids: set[int] = field(default_factory=set)


@dataclass(slots=True)
class TriResult:
    truth: str
    details: list[str] = field(default_factory=list)

    @property
    def is_true(self) -> bool:
        return self.truth == TRUE

    @property
    def is_unknown(self) -> bool:
        return self.truth == UNKNOWN


def condition_has_positive_evidence(cond: Condition | None) -> bool:
    """True if the condition proves *positive* completion evidence.

    A negated file condition is not positive evidence.  Process-only success
    rules are deliberately insufficient for SUCCESS (spec §4.3).
    """
    if cond is None:
        return False
    if cond.kind in {"log_contains", "file_exists_fresh", "file_size_above"}:
        return True
    for child in (cond.all or []) + (cond.any or []):
        if condition_has_positive_evidence(child):
            return True
    return False


class RuleEngine:
    def __init__(self) -> None:
        self._timers: dict[tuple[str, str], float] = {}

    def reset_task(self, task_id: str) -> None:
        for key in [k for k in self._timers if k[0] == task_id]:
            self._timers.pop(key, None)

    # ------------------------------------------------------------------ public
    def evaluate_success(self, task_id: str, rules: RulesSpec, ctx: EvalContext) -> TriResult:
        if rules.success is None:
            return TriResult(FALSE, ["未配置成功规则"])
        result = self._evaluate(task_id, "success", rules.success, ctx)
        if result.truth == TRUE and not condition_has_positive_evidence(rules.success):
            return TriResult(
                UNKNOWN,
                result.details + ["成功规则缺少日志/结果等正向证据；按规范不得判为 SUCCESS"],
            )
        return result

    def evaluate_failure(
        self, task_id: str, rules: RulesSpec, ctx: EvalContext
    ) -> tuple[TriResult, str]:
        if rules.failure is None:
            return TriResult(FALSE, ["未配置失败规则"]), "on_process_exit"
        failure: FailureRule = rules.failure
        result = self._evaluate(task_id, "failure", failure, ctx)
        return result, failure.finalize

    def evaluate_warning(self, task_id: str, warning: WarningRule, ctx: EvalContext) -> TriResult:
        return self._evaluate(task_id, f"warning.{warning.id}", warning, ctx)

    # ----------------------------------------------------------------- internal
    def _evaluate(self, task_id: str, key: str, cond: Condition, ctx: EvalContext) -> TriResult:
        details: list[str] = []
        raw = self._raw(task_id, key, cond, ctx, details)
        sustained = self._apply_sustain(task_id, key, cond.sustain_seconds, raw, ctx, details)
        return TriResult(sustained, details)

    def _apply_sustain(
        self,
        task_id: str,
        key: str,
        seconds: int,
        raw: str,
        ctx: EvalContext,
        details: list[str],
    ) -> str:
        if seconds <= 0:
            return raw
        tkey = (task_id, key)
        if raw != TRUE:
            self._timers.pop(tkey, None)
            if raw == UNKNOWN:
                details.append("持续条件因 UNKNOWN 重置")
            return raw
        first = self._timers.get(tkey)
        if first is None:
            first = ctx.now_epoch
            self._timers[tkey] = first
        waited = ctx.now_epoch - first
        if waited + 1e-6 >= seconds:
            return TRUE
        details.append(f"条件已满足 {waited:.0f}s，尚未达到持续 {seconds}s")
        return FALSE

    def _raw(
        self,
        task_id: str,
        key: str,
        cond: Condition,
        ctx: EvalContext,
        details: list[str],
    ) -> str:
        if cond.kind is not None:
            return self._leaf(cond, ctx, details)
        if cond.all is not None:
            results = [
                self._evaluate(task_id, f"{key}.all.{index}", child, ctx)
                for index, child in enumerate(cond.all)
            ]
            for result in results:
                details.extend(result.details)
            if any(r.truth == FALSE for r in results):
                details.append("all: 存在 FALSE")
                return FALSE
            if any(r.truth == UNKNOWN for r in results):
                details.append("all: 存在 UNKNOWN")
                return UNKNOWN
            details.append("all: 全部 TRUE")
            return TRUE
        if cond.any is not None:
            results = [
                self._evaluate(task_id, f"{key}.any.{index}", child, ctx)
                for index, child in enumerate(cond.any)
            ]
            for result in results:
                details.extend(result.details)
            if any(r.truth == TRUE for r in results):
                details.append("any: 至少一个 TRUE")
                return TRUE
            if any(r.truth == UNKNOWN for r in results):
                details.append("any: 无 TRUE 且存在 UNKNOWN")
                return UNKNOWN
            details.append("any: 全部 FALSE")
            return FALSE
        if cond.not_ is not None:
            inner = self._evaluate(task_id, f"{key}.not", cond.not_, ctx)
            details.extend(inner.details)
            if inner.truth == TRUE:
                details.append("not: 内层 TRUE -> FALSE")
                return FALSE
            if inner.truth == FALSE:
                details.append("not: 内层 FALSE -> TRUE")
                return TRUE
            details.append("not: 内层 UNKNOWN")
            return UNKNOWN
        details.append("无效条件")
        return UNKNOWN

    def _leaf(self, cond: Condition, ctx: EvalContext, details: list[str]) -> str:
        kind = cond.kind
        proc = ctx.process
        ev = ctx.evidence
        if kind == "process_exited":
            if not proc.tree_complete and proc.unknown_children:
                details.append("进程状态不完整（权限不足），无法确认根进程退出")
                return UNKNOWN
            details.append("根进程已退出" if not proc.root_alive else "根进程仍在运行")
            return TRUE if not proc.root_alive else FALSE
        if kind == "process_tree_exited":
            if proc.tree_exited:
                if proc.tree_complete:
                    details.append("进程树已退出")
                    return TRUE
                details.append("已知进程均已退出，但部分子进程信息不可读")
                return UNKNOWN
            details.append("进程树仍在运行")
            return FALSE
        if kind == "log_contains":
            truth = ev.log_contains(cond.value or "")
            if truth:
                details.append(f"日志命中 {cond.value!r}")
            elif truth is None:
                details.append(f"日志不可用/不可信，无法判断 {cond.value!r}")
            else:
                details.append(f"日志未命中 {cond.value!r}")
            return TRUE if truth is True else UNKNOWN if truth is None else FALSE
        if kind == "log_not_updated":
            if not ev.log.available or ev.log.mtime is None:
                details.append("日志不可用，无法判断是否停止更新")
                return UNKNOWN
            age = max(0.0, ctx.now_epoch - ev.log.mtime)
            ok = age >= (cond.seconds or 0)
            details.append(f"日志已 {age:.0f}s 未更新（阈值 {cond.seconds}s）")
            return TRUE if ok else FALSE
        if kind == "file_exists_fresh":
            f = ev.file(cond.path or "")
            if f.exists:
                details.append(
                    "本次结果文件存在且新鲜" if f.fresh else "结果文件存在但早于本次运行"
                )
                return TRUE if f.fresh else FALSE
            if f.error and "不存在" not in f.error:
                details.append(f"结果文件不可读: {f.error}")
                return UNKNOWN
            details.append("结果文件不存在")
            return FALSE
        if kind == "file_size_above":
            f = ev.file(cond.path or "")
            if f.exists and f.size is not None:
                if not f.fresh:
                    details.append("文件早于本次运行，不能作为本次结果")
                    return FALSE
                ok = f.size >= (cond.bytes or 0)
                details.append(f"文件大小 {f.size}B（阈值 {cond.bytes}B）")
                return TRUE if ok else FALSE
            if f.error and "不存在" not in f.error:
                details.append(f"文件不可读: {f.error}")
                return UNKNOWN
            details.append("文件不存在")
            return FALSE
        if kind == "cpu_below":
            if not proc.cpu_available or proc.cpu_percent is None:
                details.append("CPU 指标不可用（预热或进程不可读），UNKNOWN")
                return UNKNOWN
            ok = proc.cpu_percent < (cond.percent or 0)
            details.append(f"CPU {proc.cpu_percent:.2f}%（阈值 <{cond.percent}%）")
            return TRUE if ok else FALSE
        if kind == "gpu_util_below":
            return self._gpu_util(cond, ctx, details)
        if kind == "gpu_memory_above":
            return self._gpu_memory(cond, ctx, details)
        if kind == "elapsed_above":
            ok = ctx.elapsed_seconds >= (cond.seconds or 0)
            details.append(f"已运行 {ctx.elapsed_seconds:.0f}s（阈值 {cond.seconds}s）")
            return TRUE if ok else FALSE
        details.append(f"不支持的规则类型: {kind}")
        return UNKNOWN

    def _gpu_processes(self, cond: Condition, ctx: EvalContext):
        if ctx.gpu is None or not ctx.gpu.available:
            return None
        procs = [p for p in ctx.gpu.processes if p.pid in ctx.bound_pids]
        if cond.gpu_uuid:
            procs = [p for p in procs if p.gpu_uuid == cond.gpu_uuid]
        return procs

    def _gpu_util(self, cond: Condition, ctx: EvalContext, details: list[str]) -> str:
        if ctx.gpu is None or not ctx.gpu.available:
            details.append("GPU/NVML 不可用，不将利用率视为 0")
            return UNKNOWN
        if not ctx.gpu.per_process_utilization_available:
            details.append("当前 NVML 无进程级利用率指标，UNKNOWN")
            return UNKNOWN
        procs = self._gpu_processes(cond, ctx)
        if not procs:
            details.append("任务进程未出现在 GPU 进程表中，无法判断利用率")
            return UNKNOWN
        values = [p.utilization_percent for p in procs if p.utilization_percent is not None]
        if not values:
            details.append("进程级 GPU 利用率不可用，UNKNOWN")
            return UNKNOWN
        value = max(values)
        ok = value < (cond.percent or 0)
        details.append(f"GPU 进程利用率 {value:.1f}%（阈值 <{cond.percent}%）")
        return TRUE if ok else FALSE

    def _gpu_memory(self, cond: Condition, ctx: EvalContext, details: list[str]) -> str:
        if ctx.gpu is None or not ctx.gpu.available:
            details.append("GPU/NVML 不可用，显存指标为 UNKNOWN")
            return UNKNOWN
        procs = self._gpu_processes(cond, ctx)
        if procs is None:
            return UNKNOWN
        if not procs:
            if not ctx.gpu.process_metrics_available:
                details.append("GPU 进程查询不可用（权限/驱动限制），显存为 UNKNOWN，不视为 0")
                return UNKNOWN
            # GPU is available, process query works, and the task has no GPU
            # process: 0 MiB is a real measurement.
            value = 0.0
        else:
            values = [p.memory_mb for p in procs]
            if any(value is None for value in values):
                details.append("进程级 GPU 显存不可用或部分缺失，UNKNOWN（不视为 0）")
                return UNKNOWN
            value = sum(value for value in values if value is not None)
        ok = value > (cond.mb or 0)
        details.append(f"任务 GPU 显存 {value:.1f}MiB（阈值 >{cond.mb}MiB）")
        return TRUE if ok else FALSE


def evaluate_success_and_failure(
    engine: RuleEngine, task_id: str, rules: RulesSpec, ctx: EvalContext
) -> tuple[TriResult, TriResult, str, bool]:
    """Convenience wrapper returning success, failure, finalize mode, conflict."""
    success = engine.evaluate_success(task_id, rules, ctx)
    failure, finalize = engine.evaluate_failure(task_id, rules, ctx)
    conflict = success.truth == TRUE and failure.truth == TRUE
    return success, failure, finalize, conflict


def gpu_metric_available(gpu: GpuSnapshot | None) -> dict[str, Any]:
    if gpu is None:
        return {"available": False, "reason": "未采样"}
    return {"available": gpu.available, "reason": gpu.reason}
