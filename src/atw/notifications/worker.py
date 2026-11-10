"""Durable outbox worker: rate limit, priority, exponential backoff."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from atw.config import NotificationConfig
from atw.notifications.base import NotificationMessage, Notifier
from atw.storage.repository import Repository
from atw.utils.timeutil import utc_now

logger = logging.getLogger("atw.notifications")


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

    def _rate_allowed(self, now: datetime) -> bool:
        window_start = now - timedelta(minutes=1)
        used = self.repo.outbox_deliveries_since(self.notifier.channel, window_start)
        return used < self.config.rate_limit_per_minute

    def run_once(self, limit: int = 10) -> int:
        now = self.clock()
        if not self._rate_allowed(now):
            return 0
        sent = 0
        for row in self.repo.pending_outbox(now, limit=limit):
            if not self._rate_allowed(now):
                break
            try:
                payload = json.loads(row["payload_json"])
                message = NotificationMessage(
                    title=str(payload.get("title") or "[ATW] 通知"),
                    text=str(payload.get("text") or ""),
                )
            except (ValueError, TypeError):
                self.repo.defer_outbox(
                    int(row["id"]),
                    next_attempt_at=now + timedelta(hours=1),
                    error="outbox payload 无法解析",
                    max_attempts=self.config.max_attempts,
                )
                continue
            result = self.notifier.send(message)
            if result.ok:
                self.repo.mark_outbox_delivered(int(row["id"]), self.clock())
                sent += 1
                continue
            attempts = int(row["attempts"])
            delay = min(
                self.config.retry_max_seconds,
                self.config.retry_base_seconds * (2 ** min(attempts, 16)),
            )
            status = self.repo.defer_outbox(
                int(row["id"]),
                next_attempt_at=now + timedelta(seconds=delay),
                error=result.error or "发送失败",
                max_attempts=self.config.max_attempts,
            )
            logger.warning(
                "notification send failed id=%s event=%s status=%s error=%s",
                row["id"],
                row["event_type"],
                status,
                (result.error or "")[:200],
            )
        return sent
