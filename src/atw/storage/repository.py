"""Domain repository over the SQLite schema."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from atw.storage.db import Database
from atw.storage.records import TaskRecord
from atw.utils.timeutil import dt_to_z, utc_now

TERMINAL_STATES = ("SUCCESS", "FAILED", "UNKNOWN", "INTERRUPTED")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ requests
    def get_request(self, request_id: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM requests WHERE request_id=?", (request_id,))

    def insert_request(
        self,
        conn: sqlite3.Connection,
        *,
        request_id: str,
        task_id: str,
        operation: str,
        revision: int,
        content_hash: str,
        status: str,
        content_json: str,
        receipt_json: str,
        received_at: datetime,
        processed_at: datetime,
    ) -> None:
        conn.execute(
            "INSERT INTO requests(request_id, task_id, operation, revision, content_hash,"
            " status, content_json, receipt_json, received_at, processed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                task_id,
                operation,
                revision,
                content_hash,
                status,
                content_json,
                receipt_json,
                dt_to_z(received_at),
                dt_to_z(processed_at),
            ),
        )

    # --------------------------------------------------------------------- tasks
    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self.db.query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        return TaskRecord.from_row(row) if row else None

    def get_task_row(self, task_id: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))

    def insert_task(self, conn: sqlite3.Connection, record: TaskRecord) -> None:
        conn.execute(
            "INSERT INTO tasks(task_id, name, project, description, run_started_at, state,"
            " current_revision, target_json, evidence_json, rules_json, notifications_json,"
            " runtime_json, created_at, updated_at, accepted_at, finalized_at,"
            " finalize_reason, finalize_evidence, session_id, boot_id, archived)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                record.task_id,
                record.name,
                record.project,
                record.description,
                dt_to_z(record.run_started_at),
                record.state,
                record.revision,
                record.target.model_dump_json(by_alias=True, exclude_none=True),
                record.evidence.model_dump_json(by_alias=True, exclude_none=True)
                if record.evidence
                else None,
                record.rules.model_dump_json(by_alias=True, exclude_none=True),
                record.notifications.model_dump_json(by_alias=True, exclude_none=True),
                _json(record.runtime),
                dt_to_z(record.created_at),
                dt_to_z(record.updated_at),
                dt_to_z(record.accepted_at),
                dt_to_z(record.finalized_at),
                record.finalize_reason,
                record.finalize_evidence,
                record.session_id,
                record.boot_id,
            ),
        )

    def update_task_config(
        self,
        conn: sqlite3.Connection,
        record: TaskRecord,
        *,
        request_id: str,
        changes_json: str,
        now: datetime,
    ) -> bool:
        cur = conn.execute(
            "UPDATE tasks SET name=?, project=?, description=?, run_started_at=?,"
            " current_revision=?, evidence_json=?, rules_json=?, notifications_json=?,"
            " runtime_json=?, updated_at=? WHERE task_id=? AND state='MONITORING'",
            (
                record.name,
                record.project,
                record.description,
                dt_to_z(record.run_started_at),
                record.revision,
                record.evidence.model_dump_json(by_alias=True, exclude_none=True)
                if record.evidence
                else None,
                record.rules.model_dump_json(by_alias=True, exclude_none=True),
                record.notifications.model_dump_json(by_alias=True, exclude_none=True),
                _json(record.runtime),
                dt_to_z(now),
                record.task_id,
            ),
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            "INSERT INTO task_revisions(task_id, revision, request_id, operation,"
            " snapshot_json, changes_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                record.task_id,
                record.revision,
                request_id,
                "update",
                _json(record.snapshot()),
                changes_json,
                dt_to_z(now),
            ),
        )
        return True

    def insert_revision(
        self,
        conn: sqlite3.Connection,
        record: TaskRecord,
        *,
        request_id: str,
        operation: str,
        changes_json: str | None,
        now: datetime,
    ) -> None:
        conn.execute(
            "INSERT INTO task_revisions(task_id, revision, request_id, operation,"
            " snapshot_json, changes_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                record.task_id,
                record.revision,
                request_id,
                operation,
                _json(record.snapshot()),
                changes_json,
                dt_to_z(now),
            ),
        )

    def revisions(self, task_id: str) -> list[sqlite3.Row]:
        return self.db.query_all(
            "SELECT revision, request_id, operation, changes_json, created_at"
            " FROM task_revisions WHERE task_id=? ORDER BY revision",
            (task_id,),
        )

    def update_runtime(
        self, conn: sqlite3.Connection, task_id: str, runtime: dict[str, Any], now: datetime
    ) -> None:
        conn.execute(
            "UPDATE tasks SET runtime_json=?, updated_at=? WHERE task_id=?",
            (_json(runtime), dt_to_z(now), task_id),
        )

    def finalize(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        state: str,
        reason: str,
        evidence: str | None,
        runtime: dict[str, Any],
        now: datetime,
    ) -> bool:
        if state not in TERMINAL_STATES:
            raise ValueError(f"not a terminal state: {state}")
        cur = conn.execute(
            "UPDATE tasks SET state=?, finalized_at=?, finalize_reason=?,"
            " finalize_evidence=?, runtime_json=?, updated_at=?, archived=1"
            " WHERE task_id=? AND state='MONITORING'",
            (
                state,
                dt_to_z(now),
                reason,
                evidence,
                _json(runtime),
                dt_to_z(now),
                task_id,
            ),
        )
        return cur.rowcount == 1

    def mark_archived(self, task_id: str) -> None:
        self.db.execute("UPDATE tasks SET archived=1 WHERE task_id=?", (task_id,))

    def list_tasks(self, state: str | None = None, limit: int = 200) -> list[TaskRecord]:
        if state:
            rows = self.db.query_all(
                "SELECT * FROM tasks WHERE state=? ORDER BY created_at DESC LIMIT ?",
                (state, limit),
            )
        else:
            rows = self.db.query_all(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [TaskRecord.from_row(row) for row in rows]

    def monitoring_tasks(self) -> list[TaskRecord]:
        rows = self.db.query_all("SELECT * FROM tasks WHERE state='MONITORING' ORDER BY created_at")
        return [TaskRecord.from_row(row) for row in rows]

    def history(self, limit: int = 50) -> list[TaskRecord]:
        rows = self.db.query_all(
            "SELECT * FROM tasks WHERE state IN (?,?,?,?) AND finalized_at IS NOT NULL"
            " ORDER BY finalized_at DESC LIMIT ?",
            (*TERMINAL_STATES, limit),
        )
        return [TaskRecord.from_row(row) for row in rows]

    def state_counts(self) -> dict[str, int]:
        rows = self.db.query_all("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")
        return {row["state"]: int(row["n"]) for row in rows}

    def finalized_counts_since(self, since: datetime) -> dict[str, int]:
        rows = self.db.query_all(
            "SELECT state, COUNT(*) AS n FROM tasks WHERE finalized_at >= ? GROUP BY state",
            (dt_to_z(since),),
        )
        return {row["state"]: int(row["n"]) for row in rows}

    # ----------------------------------------------------------------- processes
    def record_process(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        pid: int,
        create_time_epoch: float,
        parent_pid: int | None,
        role: str,
        exe: str | None,
        cmdline: str | None,
        username: str | None,
        now: datetime,
        alive: bool = True,
    ) -> None:
        conn.execute(
            "INSERT INTO processes(task_id, pid, create_time_epoch, parent_pid, role, exe,"
            " cmdline, username, discovered_at, last_seen_at, alive)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(task_id, pid, create_time_epoch) DO UPDATE SET"
            " parent_pid=excluded.parent_pid, exe=excluded.exe, cmdline=excluded.cmdline,"
            " username=excluded.username, last_seen_at=excluded.last_seen_at,"
            " alive=excluded.alive",
            (
                task_id,
                pid,
                create_time_epoch,
                parent_pid,
                role,
                exe,
                cmdline,
                username,
                dt_to_z(now),
                dt_to_z(now),
                1 if alive else 0,
            ),
        )

    def list_processes(self, task_id: str, *, alive_only: bool = False) -> list[sqlite3.Row]:
        if alive_only:
            return self.db.query_all(
                "SELECT * FROM processes WHERE task_id=? AND alive=1 ORDER BY id", (task_id,)
            )
        return self.db.query_all("SELECT * FROM processes WHERE task_id=? ORDER BY id", (task_id,))

    def mark_process_dead(
        self, conn: sqlite3.Connection, task_id: str, pid: int, create_time_epoch: float
    ) -> None:
        conn.execute(
            "UPDATE processes SET alive=0 WHERE task_id=? AND pid=? AND create_time_epoch=?",
            (task_id, pid, create_time_epoch),
        )

    # -------------------------------------------------------------- observations
    def add_observation(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        ts: datetime,
        cpu_percent: float | None,
        memory_rss: int | None,
        gpu: Any,
        process_alive: bool | None,
        details: Any,
    ) -> None:
        conn.execute(
            "INSERT INTO observations(task_id, ts, cpu_percent, memory_rss, gpu_json,"
            " process_alive, details_json) VALUES (?,?,?,?,?,?,?)",
            (
                task_id,
                dt_to_z(ts),
                cpu_percent,
                memory_rss,
                _json(gpu) if gpu is not None else None,
                1 if process_alive else 0 if process_alive is not None else None,
                _json(details) if details is not None else None,
            ),
        )

    def observations(self, task_id: str, limit: int = 100) -> list[sqlite3.Row]:
        return self.db.query_all(
            "SELECT * FROM observations WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        )

    # ----------------------------------------------------------- events/outbox
    def record_event(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str | None,
        event_type: str,
        instance: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> bool:
        dedupe = f"{task_id or 'service'}:{event_type}:{instance}"
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(task_id, event_type, event_instance, ts,"
            " payload_json, dedupe_key) VALUES (?,?,?,?,?,?)",
            (task_id, event_type, instance, dt_to_z(now), _json(payload), dedupe),
        )
        return cur.rowcount == 1

    def enqueue_notification(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str | None,
        event_type: str,
        instance: str,
        channel: str,
        title: str,
        text: str,
        priority: int,
        now: datetime,
    ) -> bool:
        dedupe = f"{task_id or 'service'}:{event_type}:{instance}"
        cur = conn.execute(
            "INSERT OR IGNORE INTO notification_outbox(task_id, event_type, event_instance,"
            " dedupe_key, channel, payload_json, priority, status, attempts, next_attempt_at,"
            " created_at) VALUES (?,?,?,?,?,?,?,'pending',0,?,?)",
            (
                task_id,
                event_type,
                instance,
                dedupe,
                channel,
                _json({"title": title, "text": text}),
                priority,
                dt_to_z(now),
                dt_to_z(now),
            ),
        )
        return cur.rowcount == 1

    def pending_outbox(self, now: datetime, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.query_all(
            "SELECT * FROM notification_outbox WHERE status='pending' AND next_attempt_at<=?"
            " ORDER BY priority ASC, id ASC LIMIT ?",
            (dt_to_z(now), limit),
        )

    def mark_outbox_delivered(self, row_id: int, now: datetime) -> None:
        self.db.execute(
            "UPDATE notification_outbox SET status='delivered', delivered_at=?, last_error=NULL"
            " WHERE id=?",
            (dt_to_z(now), row_id),
        )

    def defer_outbox(
        self,
        row_id: int,
        *,
        next_attempt_at: datetime,
        error: str | None,
        max_attempts: int = 0,
    ) -> str:
        row = self.db.query_one("SELECT attempts FROM notification_outbox WHERE id=?", (row_id,))
        attempts = int(row["attempts"]) + 1 if row else 1
        status = "failed" if max_attempts and attempts >= max_attempts else "pending"
        self.db.execute(
            "UPDATE notification_outbox SET attempts=?, status=?, next_attempt_at=?,"
            " last_error=? WHERE id=?",
            (attempts, status, dt_to_z(next_attempt_at), (error or "")[:1000], row_id),
        )
        return status

    def outbox_deliveries_since(self, channel: str, since: datetime) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM notification_outbox WHERE channel=? AND"
            " status='delivered' AND delivered_at>=?",
            (channel, dt_to_z(since)),
        )
        return int(row["n"]) if row else 0

    def outbox_counts(self) -> dict[str, int]:
        rows = self.db.query_all(
            "SELECT status, COUNT(*) AS n FROM notification_outbox GROUP BY status"
        )
        return {row["status"]: int(row["n"]) for row in rows}

    def recent_notifications(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.db.query_all(
            "SELECT id, task_id, event_type, channel, status, attempts, created_at,"
            " delivered_at, last_error, payload_json FROM notification_outbox"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def recent_events(self, task_id: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if task_id:
            return self.db.query_all(
                "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            )
        return self.db.query_all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ sessions
    def last_session(self) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM service_sessions ORDER BY started_at DESC LIMIT 1")

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM service_sessions WHERE session_id=?", (session_id,))

    def create_session(
        self,
        *,
        session_id: str,
        instance_id: str,
        boot_id: str,
        boot_confidence: str,
        pid: int,
        started_at: datetime,
        service_version: str,
    ) -> None:
        self.db.execute(
            "INSERT INTO service_sessions(session_id, instance_id, boot_id, boot_confidence,"
            " pid, started_at, ended_at, clean_shutdown, recovered_notified, service_version)"
            " VALUES (?,?,?,?,?,?,NULL,0,0,?)",
            (
                session_id,
                instance_id,
                boot_id,
                boot_confidence,
                pid,
                dt_to_z(started_at),
                service_version,
            ),
        )

    def close_session(self, session_id: str, now: datetime, *, clean: bool = True) -> None:
        self.db.execute(
            "UPDATE service_sessions SET ended_at=?, clean_shutdown=? WHERE session_id=?",
            (dt_to_z(now), 1 if clean else 0, session_id),
        )

    def mark_session_recovered(self, session_id: str) -> None:
        self.db.execute(
            "UPDATE service_sessions SET recovered_notified=1 WHERE session_id=?",
            (session_id,),
        )

    # --------------------------------------------------------------- service kv
    def get_state(self, key: str) -> str | None:
        row = self.db.query_one("SELECT value FROM service_state WHERE key=?", (key,))
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str, now: datetime) -> None:
        self.db.execute(
            "INSERT INTO service_state(key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, dt_to_z(now)),
        )

    # ---------------------------------------------------------------- retention
    def purge_retention(self, days: int) -> dict[str, int]:
        cutoff = dt_to_z(utc_now().replace(microsecond=0))
        # Compute cutoff in SQLite-friendly form via Python for clarity.
        from datetime import timedelta

        cutoff = dt_to_z(utc_now() - timedelta(days=days))
        result: dict[str, int] = {}
        for name, sql in (
            ("observations", "DELETE FROM observations WHERE ts < ?"),
            ("events", "DELETE FROM events WHERE ts < ?"),
            (
                "outbox",
                "DELETE FROM notification_outbox WHERE status='delivered' AND delivered_at < ?",
            ),
        ):
            cur = self.db.execute(sql, (cutoff,))
            result[name] = cur.rowcount
        return result

    def counts(self) -> dict[str, Any]:
        tasks = self.db.query_one("SELECT COUNT(*) AS n FROM tasks")
        monitoring = self.db.query_one("SELECT COUNT(*) AS n FROM tasks WHERE state='MONITORING'")
        requests = self.db.query_one("SELECT COUNT(*) AS n FROM requests")
        return {
            "tasks": int(tasks["n"]) if tasks else 0,
            "monitoring": int(monitoring["n"]) if monitoring else 0,
            "requests": int(requests["n"]) if requests else 0,
            "outbox": self.outbox_counts(),
            "states": self.state_counts(),
        }

    def iter_tasks_by_state(self, state: str) -> Iterable[TaskRecord]:
        for row in self.db.query_all(
            "SELECT * FROM tasks WHERE state=? ORDER BY created_at", (state,)
        ):
            yield TaskRecord.from_row(row)
