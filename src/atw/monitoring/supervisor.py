"""Task supervision loop, terminal decisions, recovery and archiving."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any

from atw.config import AtwPaths, Config, resolve_allowed_roots
from atw.health.bootid import BootIdentity
from atw.models.task import NotificationSettings, RulesSpec
from atw.monitoring.evidence import EvidenceMonitor
from atw.monitoring.gpu import GpuMonitor, GpuSnapshot
from atw.monitoring.process import ProcessIdentity, ProcessMonitor, ProcessSnapshot
from atw.notifications.templates import (
    render_daily_health,
    render_event,
    render_service_recovered,
)
from atw.rules.engine import (
    FALSE,
    TRUE,
    UNKNOWN,
    EvalContext,
    RuleEngine,
    TriResult,
)
from atw.storage.records import TaskRecord
from atw.storage.repository import Repository
from atw.utils.atomic import atomic_write_json
from atw.utils.timeutil import duration_text, ensure_utc, utc_now

logger = logging.getLogger("atw.supervisor")

TERMINAL = {"SUCCESS", "FAILED", "UNKNOWN", "INTERRUPTED"}


class Supervisor:
    def __init__(
        self,
        cfg: Config,
        paths: AtwPaths,
        repo: Repository,
        engine: RuleEngine,
        process_monitor: ProcessMonitor,
        evidence_monitor: EvidenceMonitor,
        gpu_monitor: GpuMonitor,
        *,
        channel: str,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.cfg = cfg
        self.paths = paths
        self.repo = repo
        self.engine = engine
        self.processes = process_monitor
        self.evidence = evidence_monitor
        self.gpu = gpu_monitor
        self.channel = channel
        self.clock = clock
        self.node_label = cfg.node_label or cfg.instance_id
        self.roots = resolve_allowed_roots(cfg)

    # ------------------------------------------------------------------- ticks
    def tick(self) -> int:
        now = ensure_utc(self.clock())
        tasks = self.repo.monitoring_tasks()
        if not tasks:
            return 0
        pids: set[int] = set()
        for record in tasks:
            for row in self.repo.list_processes(record.task_id, alive_only=True):
                pids.add(int(row["pid"]))
        gpu_snapshot = self.gpu.sample(pids) if pids else None
        processed = 0
        for record in tasks:
            try:
                self._process_task(record, now, gpu_snapshot)
                processed += 1
            except Exception:
                logger.exception("监督任务失败 task_id=%s（下个周期重试）", record.task_id)
        return processed

    def _process_task(
        self, record: TaskRecord, now: datetime, gpu_snapshot: GpuSnapshot | None
    ) -> None:
        known_rows = self.repo.list_processes(record.task_id)
        root_identity = self._root_identity(record, known_rows)
        known = [
            ProcessIdentity(int(row["pid"]), float(row["create_time_epoch"]))
            for row in known_rows
            if row["alive"]
        ]
        if root_identity is not None and root_identity not in known:
            known.append(root_identity)
        snap = self.processes.snapshot(record.target, known, root_identity)
        evidence_snap, runtime = self.evidence.refresh(record, record.runtime)

        rules_hash = _rules_hash(record.rules)
        if runtime.get("rules_hash") != rules_hash:
            self.engine.reset_task(record.task_id)
            runtime["rules_hash"] = rules_hash
            runtime["warnings"] = {}
            runtime["max_runtime_alerted"] = False

        alive_pids = {m.identity.pid for m in snap.members}
        gpu_metrics = (
            gpu_snapshot.task_metrics(alive_pids)
            if gpu_snapshot is not None
            else {"available": False, "reason": "无可采样的监控进程"}
        )
        started_at = record.run_started_at or record.accepted_at
        elapsed = max(0.0, (now - started_at).total_seconds()) if started_at else 0.0
        ctx = EvalContext(
            now=now,
            now_epoch=now.timestamp(),
            started_at=started_at,
            elapsed_seconds=elapsed,
            process=snap,
            evidence=evidence_snap,
            gpu=gpu_snapshot,
            bound_pids=alive_pids,
        )
        success = self.engine.evaluate_success(record.task_id, record.rules, ctx)
        failure, finalize_mode = self.engine.evaluate_failure(record.task_id, record.rules, ctx)
        conflict = success.truth == TRUE and failure.truth == TRUE

        notification_payloads: list[dict[str, Any]] = []
        terminal_state = self._decide_terminal(
            success, failure, finalize_mode, snap, runtime, record, now
        )
        terminal_reason = ""
        if terminal_state is not None:
            terminal_reason = self._terminal_reason(terminal_state, success, failure, snap)
        if terminal_state is None:
            max_state, max_warning = self._max_runtime_action(record, runtime, elapsed)
            if max_state is not None:
                terminal_state = max_state
                terminal_reason = (
                    f"运行时间超过 max_runtime_seconds={record.rules.max_runtime_seconds}，"
                    "按任务配置进入终态（不终止计算进程）"
                )
            elif max_warning is not None:
                notification_payloads.append(max_warning)

        summary = self._summary(success, failure, terminal_state)
        task_gpu = gpu_metrics
        observation_details = {
            "process": snap.details(),
            "evidence": evidence_snap.as_dict(),
            "success": success.truth,
            "failure": failure.truth,
        }

        warning_updates = runtime.get("warnings") or {}
        runtime["warnings"] = warning_updates

        if terminal_state is None:
            runtime["exit_observed_at"] = (
                runtime.get("exit_observed_at") if snap.tree_exited else None
            )
            if snap.tree_exited and runtime.get("exit_observed_at") is None:
                runtime["exit_observed_at"] = now.timestamp()
            if not snap.tree_exited:
                self._collect_warnings(record, ctx, runtime, notification_payloads, now)
            self._collect_progress(
                record, runtime, notification_payloads, now, elapsed, snap, gpu_metrics
            )
        else:
            runtime["exit_observed_at"] = (
                now.timestamp() if snap.tree_exited else runtime.get("exit_observed_at")
            )

        with self.repo.db.transaction() as conn:
            self._persist_processes(conn, record, snap, known_rows, now)
            self.repo.add_observation(
                conn,
                task_id=record.task_id,
                ts=now,
                cpu_percent=snap.cpu_percent,
                memory_rss=snap.memory_rss,
                gpu=task_gpu,
                process_alive=snap.tree_alive,
                details=observation_details,
            )
            if terminal_state is not None:
                if conflict:
                    self.repo.record_event(
                        conn,
                        task_id=record.task_id,
                        event_type="RULE_CONFLICT",
                        instance=f"{int(now.timestamp())}",
                        payload={
                            "success": success.truth,
                            "failure": failure.truth,
                            "success_details": success.details,
                            "failure_details": failure.details,
                        },
                        now=now,
                    )
                payload = {
                    "state": terminal_state,
                    "reason": terminal_reason,
                    "duration_seconds": elapsed,
                    "evidence_summary": summary,
                    "cpu_percent": snap.cpu_percent,
                    "gpu": task_gpu,
                }
                self._finalize(
                    conn,
                    record,
                    terminal_state,
                    terminal_reason,
                    summary,
                    runtime,
                    payload,
                    now,
                )
            else:
                for item in notification_payloads:
                    self._record_notification(conn, record, item, now)
                runtime["last_sample"] = {
                    "at": now.isoformat(),
                    "cpu_percent": snap.cpu_percent,
                    "memory_rss": snap.memory_rss,
                    "gpu": task_gpu,
                    "process_alive": snap.tree_alive,
                }
                self.repo.update_runtime(conn, record.task_id, runtime, now)

        if terminal_state is not None:
            self._archive(record.task_id)

    # ------------------------------------------------------------- decisions
    def _decide_terminal(
        self,
        success: TriResult,
        failure: TriResult,
        finalize_mode: str,
        snap: ProcessSnapshot,
        runtime: dict[str, Any],
        record: TaskRecord,
        now: datetime,
    ) -> str | None:
        if success.truth == TRUE and failure.truth == TRUE:
            return "FAILED"  # failure wins
        if failure.truth == TRUE:
            if finalize_mode == "immediate" or snap.tree_exited:
                return "FAILED"
        if success.truth == TRUE and failure.truth == FALSE and snap.tree_exited:
            return "SUCCESS"
        if snap.tree_exited:
            exit_at = runtime.get("exit_observed_at")
            if exit_at is None:
                runtime["exit_observed_at"] = now.timestamp()
                exit_at = now.timestamp()
            if now.timestamp() - float(exit_at) >= self.cfg.finalize_grace_seconds:
                return "UNKNOWN"
        return None

    @staticmethod
    def _terminal_reason(
        state: str, success: TriResult, failure: TriResult, snap: ProcessSnapshot
    ) -> str:
        if state == "SUCCESS":
            return "成功规则全部满足，且进程树已退出"
        if state == "FAILED":
            if success.truth == TRUE and failure.truth == TRUE:
                return "规则冲突：成功与失败规则同时为 TRUE；按失败优先判定"
            return "失败规则满足" + ("" if snap.tree_exited else "（immediate 模式）")
        if state == "UNKNOWN":
            if success.truth == UNKNOWN or failure.truth == UNKNOWN:
                return "进程树已退出，但关键证据或指标为 UNKNOWN，不能判定成功"
            return "进程树已退出，但没有足够可信的成功证据（未判为 SUCCESS）"
        if state == "INTERRUPTED":
            return "主机重启导致原监控实例中断，不能重新激活"
        return state

    def _max_runtime_action(
        self, record: TaskRecord, runtime: dict[str, Any], elapsed: float
    ) -> tuple[str | None, dict[str, Any] | None]:
        limit = record.rules.max_runtime_seconds
        if limit is None or elapsed < limit or runtime.get("max_runtime_alerted"):
            return None, None
        runtime["max_runtime_alerted"] = True
        action = record.rules.max_runtime_action
        if action == "failed":
            return "FAILED", None
        if action == "unknown":
            return "UNKNOWN", None
        now_epoch = self.clock().timestamp()
        return None, {
            "event_type": "WARNING",
            "instance": f"max-runtime:{int(now_epoch * 1000)}",
            "priority": 3,
            "payload": {
                "warning_id": "max-runtime",
                "message": (
                    f"运行时间 {duration_text(elapsed)} 超过配置上限 "
                    f"{duration_text(limit)}，按配置继续监督（不终止计算进程）"
                ),
                "elapsed_seconds": elapsed,
                "condition_details": ["max_runtime_seconds exceeded"],
            },
        }

    @staticmethod
    def _summary(success: TriResult, failure: TriResult, state: str | None) -> str:
        details: list[str] = []
        if state == "SUCCESS":
            details = success.details
        elif state == "FAILED":
            details = failure.details or success.details
        else:
            details = success.details + failure.details
        unique: list[str] = []
        for item in details:
            if item not in unique:
                unique.append(item)
        text = "；".join(unique) if unique else "无"
        return text[:800]

    # -------------------------------------------------------------- warnings
    def _collect_warnings(
        self,
        record: TaskRecord,
        ctx: EvalContext,
        runtime: dict[str, Any],
        payloads: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        states: dict[str, Any] = runtime.setdefault("warnings", {})
        repeat_seconds = record.notifications.warning_repeat_minutes * 60
        for warning in record.rules.warnings:
            result = self.engine.evaluate_warning(record.task_id, warning, ctx)
            state = states.setdefault(warning.id, {"active": False, "last_sent_epoch": None})
            if result.truth == TRUE:
                last = state.get("last_sent_epoch")
                state["active"] = True
                if last is None or ctx.now_epoch - float(last) >= repeat_seconds:
                    state["last_sent_epoch"] = ctx.now_epoch
                    payloads.append(
                        {
                            "event_type": "WARNING",
                            "instance": f"{warning.id}:{int(ctx.now_epoch * 1000)}",
                            "priority": 3,
                            "payload": {
                                "warning_id": warning.id,
                                "message": warning.message
                                or "；".join(result.details)
                                or "警告条件满足",
                                "elapsed_seconds": ctx.elapsed_seconds,
                                "condition_details": result.details,
                            },
                        }
                    )
            else:
                state["active"] = False
                state["last_sent_epoch"] = None

    def _collect_progress(
        self,
        record: TaskRecord,
        runtime: dict[str, Any],
        payloads: list[dict[str, Any]],
        now: datetime,
        elapsed: float,
        snap: ProcessSnapshot,
        gpu_metrics: dict[str, Any],
    ) -> None:
        interval = record.notifications.progress_interval_minutes
        if interval <= 0:
            return
        progress = runtime.setdefault("progress", {})
        last = progress.get("last_epoch")
        if last is None:
            last = (record.accepted_at or now).timestamp()
            progress["last_epoch"] = last
        if now.timestamp() - float(last) < interval * 60:
            return
        progress["last_epoch"] = now.timestamp()
        payloads.append(
            {
                "event_type": "PROGRESS",
                "instance": f"progress:{int(now.timestamp())}",
                "priority": 5,
                "payload": {
                    "elapsed_seconds": elapsed,
                    "cpu_percent": snap.cpu_percent,
                    "gpu": gpu_metrics,
                },
            }
        )

    # ------------------------------------------------------------- persistence
    def _persist_processes(
        self,
        conn: sqlite3.Connection,
        record: TaskRecord,
        snap: ProcessSnapshot,
        known_rows: list[sqlite3.Row],
        now: datetime,
    ) -> None:
        alive_keys: set[tuple[int, float]] = set()
        for member in snap.members:
            alive_keys.add((member.identity.pid, member.identity.create_time_epoch))
            self.repo.record_process(
                conn,
                task_id=record.task_id,
                pid=member.identity.pid,
                create_time_epoch=member.identity.create_time_epoch,
                parent_pid=member.ppid,
                role=member.role,
                exe=(member.exe or "")[:4096] or None,
                cmdline=member.cmdline[:8192],
                username=member.username,
                now=now,
            )
        for row in known_rows:
            if not row["alive"]:
                continue
            key = (int(row["pid"]), float(row["create_time_epoch"]))
            if key not in alive_keys:
                self.repo.mark_process_dead(
                    conn, record.task_id, int(row["pid"]), float(row["create_time_epoch"])
                )

    def _record_notification(
        self,
        conn: sqlite3.Connection,
        record: TaskRecord,
        item: dict[str, Any],
        now: datetime,
    ) -> None:
        event_type = item["event_type"]
        payload = item["payload"]
        message = None
        try:
            message = render_event(event_type, record, payload, node_label=self.node_label)
        except Exception:
            logger.exception("通知模板渲染失败 task_id=%s event=%s", record.task_id, event_type)
        event_payload = {"title": message.title, "text": message.text} if message else payload
        self.repo.record_event(
            conn,
            task_id=record.task_id,
            event_type=event_type,
            instance=item["instance"],
            payload=event_payload,
            now=now,
        )
        enabled = True
        if event_type == "WARNING":
            enabled = record.notifications.on_warning
        if enabled and message is not None:
            self.repo.enqueue_notification(
                conn,
                task_id=record.task_id,
                event_type=event_type,
                instance=item["instance"],
                channel=self.channel,
                title=message.title,
                text=message.text,
                priority=int(item.get("priority", 5)),
                now=now,
            )

    def _finalize(
        self,
        conn: sqlite3.Connection,
        record: TaskRecord,
        state: str,
        reason: str,
        evidence_summary: str,
        runtime: dict[str, Any],
        payload: dict[str, Any],
        now: datetime,
    ) -> bool:
        ok = self.repo.finalize(
            conn,
            task_id=record.task_id,
            state=state,
            reason=reason,
            evidence=evidence_summary,
            runtime=runtime,
            now=now,
        )
        if not ok:
            return False
        instance = f"final:{int(now.timestamp() * 1000)}"
        message = None
        if self._notify_enabled(record.notifications, state):
            try:
                message = render_event(state, record, payload, node_label=self.node_label)
            except Exception:
                logger.exception("终态通知渲染失败 task_id=%s", record.task_id)
        event_payload = {"title": message.title, "text": message.text} if message else payload
        self.repo.record_event(
            conn,
            task_id=record.task_id,
            event_type=state,
            instance=instance,
            payload=event_payload,
            now=now,
        )
        if message is not None:
            self.repo.enqueue_notification(
                conn,
                task_id=record.task_id,
                event_type=state,
                instance=instance,
                channel=self.channel,
                title=message.title,
                text=message.text,
                priority=0,
                now=now,
            )
        logger.info(
            "finalized task=%s state=%s reason=%s duration=%s",
            record.task_id,
            state,
            reason,
            duration_text((now - (record.run_started_at or record.accepted_at)).total_seconds())
            if (record.run_started_at or record.accepted_at)
            else "unknown",
        )
        return True

    @staticmethod
    def _notify_enabled(settings: NotificationSettings, state: str) -> bool:
        return {
            "SUCCESS": settings.on_success,
            "FAILED": settings.on_failure,
            "UNKNOWN": settings.on_unknown,
            "INTERRUPTED": settings.on_interrupted,
        }.get(state, True)

    def _archive(self, task_id: str) -> None:
        task = self.repo.get_task(task_id)
        if task is None:
            return
        try:
            snapshot = task.snapshot()
            snapshot["archived_at"] = utc_now().isoformat()
            snapshot["events"] = [
                {
                    "event_type": row["event_type"],
                    "event_instance": row["event_instance"],
                    "ts": row["ts"],
                    "payload": json.loads(row["payload_json"]),
                }
                for row in self.repo.recent_events(task_id, limit=50)
            ]
            atomic_write_json(self.paths.archive / f"{task_id}.json", snapshot)
        except Exception:
            logger.exception("任务归档写入失败 task_id=%s", task_id)

    # --------------------------------------------------------------- recovery
    def recover(self, boot: BootIdentity) -> dict[str, Any]:
        last = self.repo.last_session()
        monitoring = self.repo.monitoring_tasks()
        result: dict[str, Any] = {
            "mode": "first_install",
            "host_reboot": False,
            "resumed": 0,
            "finalized": 0,
            "interrupted": 0,
        }
        if last is None:
            return result
        if last["boot_id"] != boot.boot_id:
            result["mode"] = "host_reboot"
            result["host_reboot"] = True
            for record in monitoring:
                runtime = dict(record.runtime)
                runtime["exit_observed_at"] = self.clock().timestamp()
                try:
                    with self.repo.db.transaction() as conn:
                        payload = {
                            "state": "INTERRUPTED",
                            "reason": "确认主机重启，旧监控实例无法恢复",
                            "duration_seconds": (
                                self.clock() - (record.run_started_at or record.accepted_at)
                            ).total_seconds()
                            if (record.run_started_at or record.accepted_at)
                            else None,
                            "evidence_summary": "boot_id 变化，主机重启",
                            "gpu": {"available": False, "reason": "恢复阶段未采样"},
                        }
                        self._finalize(
                            conn,
                            record,
                            "INTERRUPTED",
                            "确认主机重启，旧监控实例无法恢复",
                            "boot_id 变化，主机重启",
                            runtime,
                            payload,
                            self.clock(),
                        )
                except Exception:
                    logger.exception("主机重启中断处理失败 task_id=%s", record.task_id)
                    continue
                result["interrupted"] += 1
                self._archive(record.task_id)
            return result

        result["mode"] = "service_restart"
        previous_crashed = not bool(last["clean_shutdown"])
        recovered_ids: list[str] = []
        for record in monitoring:
            try:
                did_recover, finalized = self._recover_task(record)
                if did_recover:
                    recovered_ids.append(record.task_id)
                if finalized:
                    result["finalized"] += 1
                if did_recover and not finalized:
                    result["resumed"] += 1
            except Exception:
                logger.exception("服务恢复失败 task_id=%s", record.task_id)
        if previous_crashed and recovered_ids and not bool(last["recovered_notified"]):
            self._notify_service_recovered(
                previous_session_id=str(last["session_id"]),
                task_ids=recovered_ids,
                now=self.clock(),
            )
            self.repo.mark_session_recovered(str(last["session_id"]))
        return result

    def _recover_task(self, record: TaskRecord) -> tuple[bool, bool]:
        known_rows = self.repo.list_processes(record.task_id)
        root_identity = self._root_identity(record, known_rows)
        known = [
            ProcessIdentity(int(row["pid"]), float(row["create_time_epoch"]))
            for row in known_rows
            if row["alive"]
        ]
        if root_identity is not None and root_identity not in known:
            known.append(root_identity)
        snap = self.processes.snapshot(record.target, known, root_identity)
        ev_snap, runtime = self.evidence.refresh(record, record.runtime)
        runtime["rules_hash"] = _rules_hash(record.rules)
        now = ensure_utc(self.clock())
        started_at = record.run_started_at or record.accepted_at
        elapsed = max(0.0, (now - started_at).total_seconds()) if started_at else 0.0
        ctx = EvalContext(
            now=now,
            now_epoch=now.timestamp(),
            started_at=started_at,
            elapsed_seconds=elapsed,
            process=snap,
            evidence=ev_snap,
            gpu=None,
            bound_pids={m.identity.pid for m in snap.members},
        )
        success = self.engine.evaluate_success(record.task_id, record.rules, ctx)
        failure, finalize_mode = self.engine.evaluate_failure(record.task_id, record.rules, ctx)
        if snap.tree_alive:
            runtime["exit_observed_at"] = None
            with self.repo.db.transaction() as conn:
                self._persist_processes(conn, record, snap, known_rows, now)
                self.repo.update_runtime(conn, record.task_id, runtime, now)
                self.repo.record_event(
                    conn,
                    task_id=record.task_id,
                    event_type="RECOVERY_RESUMED",
                    instance=now.isoformat(),
                    payload={"pid": [m.identity.pid for m in snap.members]},
                    now=now,
                )
            self.processes.prime(record.target)
            return True, False

        # All known processes are gone: decide from the evidence now, no grace.
        state = "UNKNOWN"
        if success.truth == TRUE and failure.truth == TRUE:
            state = "FAILED"
        elif failure.truth == TRUE:
            state = "FAILED"
        elif success.truth == TRUE and failure.truth == FALSE:
            state = "SUCCESS"
        reason = {
            "SUCCESS": "服务恢复时进程已退出，成功规则与证据满足",
            "FAILED": "服务恢复时进程已退出，失败规则满足",
            "UNKNOWN": "服务恢复时进程已退出，但证据不足或为 UNKNOWN，不能判定成功",
        }[state]
        summary = self._summary(success, failure, state)
        payload = {
            "state": state,
            "reason": reason,
            "duration_seconds": elapsed,
            "evidence_summary": summary,
            "gpu": {"available": False, "reason": "恢复阶段未采样"},
        }
        with self.repo.db.transaction() as conn:
            self._persist_processes(conn, record, snap, known_rows, now)
            self.repo.add_observation(
                conn,
                task_id=record.task_id,
                ts=now,
                cpu_percent=None,
                memory_rss=None,
                gpu=None,
                process_alive=False,
                details={"recovery": True, "process": snap.details()},
            )
            self._finalize(conn, record, state, reason, summary, runtime, payload, now)
        self._archive(record.task_id)
        return True, True

    def _notify_service_recovered(
        self, *, previous_session_id: str, task_ids: list[str], now: datetime
    ) -> None:
        instance = previous_session_id
        message = render_service_recovered(
            node_label=self.node_label,
            task_count=len(task_ids),
            task_ids=task_ids,
            recovered_at=now.isoformat(),
        )
        with self.repo.db.transaction() as conn:
            inserted = self.repo.record_event(
                conn,
                task_id=None,
                event_type="SERVICE_RECOVERED",
                instance=instance,
                payload={"title": message.title, "text": message.text},
                now=now,
            )
            if inserted:
                self.repo.enqueue_notification(
                    conn,
                    task_id=None,
                    event_type="SERVICE_RECOVERED",
                    instance=instance,
                    channel=self.channel,
                    title=message.title,
                    text=message.text,
                    priority=0,
                    now=now,
                )

    # ------------------------------------------------------------ daily health
    def maybe_daily_health(self, session_started_at: datetime | None) -> bool:
        health = self.cfg.health_summary
        if not health.enabled:
            return False
        zone = self.cfg.health_zone()
        now_local = self.clock().astimezone(zone)
        hour, minute = (int(part) for part in health.time.split(":"))
        target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now_local < target:
            return False
        date_text = now_local.strftime("%Y-%m-%d")
        from datetime import timedelta

        stats = self.repo.finalized_counts_since(self.clock() - timedelta(hours=24))
        message = render_daily_health(
            node_label=self.node_label,
            date_text=date_text,
            monitoring=len(self.repo.monitoring_tasks()),
            stats=stats,
            outbox=self.repo.outbox_counts(),
            gpu=self.gpu.capability(),
            uptime_seconds=(
                (self.clock() - session_started_at).total_seconds() if session_started_at else None
            ),
        )
        with self.repo.db.transaction() as conn:
            inserted = self.repo.record_event(
                conn,
                task_id=None,
                event_type="DAILY_HEALTH",
                instance=date_text,
                payload={"title": message.title, "text": message.text},
                now=self.clock(),
            )
            if not inserted:
                return False
            self.repo.enqueue_notification(
                conn,
                task_id=None,
                event_type="DAILY_HEALTH",
                instance=date_text,
                channel=self.channel,
                title=message.title,
                text=message.text,
                priority=6,
                now=self.clock(),
            )
        return True

    # ---------------------------------------------------------------- helpers
    def _root_identity(self, record: TaskRecord, rows: list[sqlite3.Row]) -> ProcessIdentity | None:
        roots = [r for r in rows if r["role"] == "root"]
        if roots:
            row = roots[-1]
            return ProcessIdentity(int(row["pid"]), float(row["create_time_epoch"]))
        if record.target.kind == "pid" and record.target.pid and record.target.create_time_epoch:
            return ProcessIdentity(record.target.pid, float(record.target.create_time_epoch))
        return None


def _rules_hash(rules: RulesSpec) -> str:
    import hashlib

    return hashlib.sha256(
        rules.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
    ).hexdigest()
