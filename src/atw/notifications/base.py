"""Pluggable notification interface (V1 ships DingTalk + mock)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class NotificationMessage:
    title: str
    text: str


@dataclass(slots=True)
class SendResult:
    ok: bool
    error: str | None = None
    detail: str | None = None


class Notifier(Protocol):
    channel: str

    def send(self, message: NotificationMessage) -> SendResult: ...

    def close(self) -> None: ...
