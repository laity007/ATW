"""Notifier abstraction, DingTalk adapter, templates and outbox worker."""

from atw.notifications.base import NotificationMessage, Notifier, SendResult
from atw.notifications.factory import build_notifier
from atw.notifications.worker import OutboxWorker

__all__ = [
    "NotificationMessage",
    "Notifier",
    "SendResult",
    "build_notifier",
    "OutboxWorker",
]
