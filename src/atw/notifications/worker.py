"""Durable outbox worker: rate limit, priority, exponential backoff."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from atw.config import NotificationConfig
from atw.notifications.base import NotificationMessage, Notifier, SendResult
from atw.storage.repository import Repository
from atw.utils.timeutil import utc_now

logger = logging.getLogger("atw.notifications")
SHUTDOWN_WAIT_SECONDS = 5.0


class OutboxWorker:
    def __init__(
        self,
        repo: Repository,
        notifier: Notifier,
        config: NotificationConfig,
        *,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.repo = repo
        self.notifier = notifier
        self.config = config
        self.clock = clock
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._abandon_result = False
        self._thread: threading.Thread | None = None

    def start(self, interval: float) -> None:
        thread = threading.Thread(
            target=self._run, args=(interval,), name="atw-outbox", daemon=True
        )
        thread.start()
        self._thread = thread

    def close(self) -> None:
        self._stop.set()
        if self._thread is None:
            self.notifier.close()
            return
        self._thread.join(timeout=SHUTDOWN_WAIT_SECONDS)
        if self._thread.is_alive():
            # Fence off SQLite before the service closes it and releases its lock.
            # The worker owns the notifier until the in-flight send returns.
            with self._state_lock:
                self._abandon_result = True
            logger.warning("通知 worker 停止等待超时，未确认的通知保留在队列中")

    def _run(self, interval: float) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:
                    logger.exception("处理通知队列失败（下个周期重试）")
                self._stop.wait(interval)
        finally:
            self.notifier.close()

    def _rate_allowed(self, now: datetime) -> bool:
        window_start = now - timedelta(minutes=1)
        used = self.repo.outbox_deliveries_since(self.notifier.channel, window_start)
        return used < self.config.rate_limit_per_minute

    def run_once(self, limit: int = 10) -> int:
        now = self.clock()
        with self._state_lock:
            if self._stop.is_set() or not self._rate_allowed(now):
                return 0
            pending = self.repo.pending_outbox(now, limit=limit)
        sent = 0
        for row in pending:
            row_id = int(row["id"])
            with self._state_lock:
                if self._stop.is_set() or not self._rate_allowed(self.clock()):
                    break
                try:
                    payload = json.loads(row["payload_json"])
                    message = NotificationMessage(
                        title=str(payload.get("title") or "[ATW] 通知"),
                        text=str(payload.get("text") or ""),
                    )
                except (ValueError, TypeError, AttributeError):
                    failed_at = self.clock()
                    self.repo.defer_outbox(
                        row_id,
                        next_attempt_at=failed_at + timedelta(hours=1),
                        failed_at=failed_at,
                        error="outbox payload 无法解析",
                        error_code="invalid_payload",
                        max_attempts=self.config.max_attempts,
                    )
                    continue
            result = self.notifier.send(message)
            with self._state_lock:
                if self._abandon_result:
                    break
                if result.ok:
                    self.repo.mark_outbox_delivered(row_id, self.clock())
                    sent += 1
                    continue
                self._defer(row_id, row["event_type"], int(row["attempts"]), result)
        return sent

    def _defer(self, row_id: int, event_type: str, attempts: int, result: SendResult) -> None:
        delay = min(
            self.config.retry_max_seconds,
            self.config.retry_base_seconds * (2 ** min(attempts, 16)),
        )
        failed_at = self.clock()
        status = self.repo.defer_outbox(
            row_id,
            next_attempt_at=failed_at + timedelta(seconds=delay),
            failed_at=failed_at,
            error=result.error or "发送失败",
            error_code=result.error_code or "send_error",
            route=result.route,
            max_attempts=self.config.max_attempts,
        )
        logger.warning(
            "notification send failed id=%s event=%s status=%s error=%s",
            row_id,
            event_type,
            status,
            (result.error or "")[:200],
        )
