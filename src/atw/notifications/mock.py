"""Mock notifier used when no DingTalk credentials are configured and in tests."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from atw.notifications.base import NotificationMessage, SendResult
from atw.utils.atomic import secure_chmod, secure_mkdir
from atw.utils.timeutil import dt_to_z, utc_now


class MockNotifier:
    channel = "mock"

    def __init__(self, sink: Path):
        self.sink = Path(sink)
        self._lock = threading.Lock()
        secure_mkdir(self.sink.parent)

    def send(self, message: NotificationMessage) -> SendResult:
        record = {
            "sent_at": dt_to_z(utc_now()),
            "title": message.title,
            "text": message.text,
        }
        try:
            with self._lock:
                with open(self.sink, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                secure_chmod(self.sink, 0o600)
            return SendResult(ok=True, detail="mock")
        except OSError as exc:
            return SendResult(ok=False, error=f"mock 写入失败: {type(exc).__name__}")

    def close(self) -> None:
        return None
