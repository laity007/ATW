"""Atomic receipt files and the CLI wait helper."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from atw.models.task import Receipt
from atw.utils.atomic import atomic_write_json, secure_mkdir


class ReceiptStore:
    def __init__(self, receipts_dir: Path):
        self.dir = Path(receipts_dir)
        secure_mkdir(self.dir)

    def path(self, request_id: str) -> Path:
        safe = "".join(ch for ch in request_id if ch.isalnum() or ch in "._:-")[:160]
        if not safe:
            safe = "invalid"
        return self.dir / f"{safe}.json"

    def write(self, receipt: Receipt) -> Path:
        path = self.path(receipt.request_id)
        atomic_write_json(path, receipt.to_json_dict(), pretty=True)
        return path

    def read(self, request_id: str) -> dict[str, Any] | None:
        path = self.path(request_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def wait(self, request_id: str, timeout: float, poll: float = 0.25) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            data = self.read(request_id)
            if data is not None:
                return data
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
