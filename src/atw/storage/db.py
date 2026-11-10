"""SQLite connection management, WAL settings and versioned migrations."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from atw.utils.atomic import secure_chmod, secure_mkdir
from atw.utils.timeutil import dt_to_z, utc_now

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS requests (
            request_id     TEXT PRIMARY KEY,
            task_id        TEXT NOT NULL,
            operation      TEXT NOT NULL,
            revision       INTEGER NOT NULL,
            content_hash   TEXT NOT NULL,
            status         TEXT NOT NULL CHECK (status IN ('accepted','rejected')),
            content_json   TEXT NOT NULL,
            receipt_json   TEXT NOT NULL,
            received_at    TEXT NOT NULL,
            processed_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            task_id            TEXT PRIMARY KEY,
            name               TEXT NOT NULL,
            project            TEXT,
            description        TEXT,
            run_started_at     TEXT,
            state              TEXT NOT NULL CHECK
                               (state IN ('MONITORING','SUCCESS','FAILED','UNKNOWN','INTERRUPTED')),
            current_revision   INTEGER NOT NULL,
            target_json        TEXT NOT NULL,
            evidence_json      TEXT,
            rules_json         TEXT NOT NULL,
            notifications_json TEXT NOT NULL,
            runtime_json       TEXT NOT NULL DEFAULT '{}',
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL,
            accepted_at        TEXT NOT NULL,
            finalized_at       TEXT,
            finalize_reason    TEXT,
            finalize_evidence  TEXT,
            session_id         TEXT,
            boot_id            TEXT,
            archived           INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
        CREATE INDEX IF NOT EXISTS idx_tasks_finalized ON tasks(finalized_at);

        CREATE TABLE IF NOT EXISTS task_revisions (
            task_id      TEXT NOT NULL,
            revision     INTEGER NOT NULL,
            request_id   TEXT NOT NULL,
            operation    TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            changes_json TEXT,
            created_at   TEXT NOT NULL,
            PRIMARY KEY (task_id, revision),
            FOREIGN KEY (task_id) REFERENCES tasks(task_id)
        );

        CREATE TABLE IF NOT EXISTS processes (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id          TEXT NOT NULL,
            pid              INTEGER NOT NULL,
            create_time_epoch REAL NOT NULL,
            parent_pid       INTEGER,
            role             TEXT NOT NULL,
            exe              TEXT,
            cmdline          TEXT,
            username         TEXT,
            discovered_at    TEXT NOT NULL,
            last_seen_at     TEXT,
            alive            INTEGER NOT NULL DEFAULT 1,
            UNIQUE (task_id, pid, create_time_epoch),
            FOREIGN KEY (task_id) REFERENCES tasks(task_id)
        );
        CREATE INDEX IF NOT EXISTS idx_processes_task ON processes(task_id, alive);

        CREATE TABLE IF NOT EXISTS observations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id       TEXT NOT NULL,
            ts            TEXT NOT NULL,
            cpu_percent   REAL,
            memory_rss    INTEGER,
            gpu_json      TEXT,
            process_alive INTEGER,
            details_json  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_observations_task_ts ON observations(task_id, ts);

        CREATE TABLE IF NOT EXISTS events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id       TEXT,
            event_type    TEXT NOT NULL,
            event_instance TEXT NOT NULL,
            ts            TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            dedupe_key    TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_events_task_ts ON events(task_id, ts);

        CREATE TABLE IF NOT EXISTS notification_outbox (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id        TEXT,
            event_type     TEXT NOT NULL,
            event_instance TEXT NOT NULL,
            dedupe_key     TEXT NOT NULL UNIQUE,
            channel        TEXT NOT NULL,
            payload_json   TEXT NOT NULL,
            priority       INTEGER NOT NULL DEFAULT 5,
            status         TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending','delivered','failed')),
            attempts       INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            delivered_at   TEXT,
            last_error     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_pending
            ON notification_outbox(status, next_attempt_at, priority);

        CREATE TABLE IF NOT EXISTS service_sessions (
            session_id     TEXT PRIMARY KEY,
            instance_id    TEXT NOT NULL,
            boot_id        TEXT NOT NULL,
            boot_confidence TEXT NOT NULL,
            pid            INTEGER,
            started_at     TEXT NOT NULL,
            ended_at       TEXT,
            clean_shutdown INTEGER NOT NULL DEFAULT 0,
            recovered_notified INTEGER NOT NULL DEFAULT 0,
            service_version TEXT
        );

        CREATE TABLE IF NOT EXISTS service_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
]


class Database:
    """A single-process service connection plus cross-process WAL readers.

    A service-wide file lock (see ``atw.service.lock``) guarantees a single
    writer.  CLI processes open the same database read-only in practice.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            secure_mkdir(self.path.parent)
            conn = sqlite3.connect(
                str(self.path),
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
            secure_chmod(self.path, 0o600)
            self._migrate(conn)
            return conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connect()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            # Migration scripts use IF NOT EXISTS and are safe to replay if the
            # process dies between DDL and recording the version.
            try:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, dt_to_z(utc_now())),
                )
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self.connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def query_one(
        self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def query_all(
        self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
    ) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                self._conn.close()
                self._conn = None

    def integrity_check(self) -> str:
        row = self.query_one("PRAGMA integrity_check")
        return str(row[0]) if row else "unknown"
