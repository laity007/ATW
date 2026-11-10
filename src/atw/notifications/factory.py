"""Choose a notifier from configuration + environment credentials."""

from __future__ import annotations

from pathlib import Path

from atw.config import Config, dingtalk_credentials
from atw.errors import SERVICE_UNAVAILABLE, AtwError
from atw.notifications.base import Notifier
from atw.notifications.dingtalk import DingTalkNotifier
from atw.notifications.mock import MockNotifier


def build_notifier(cfg: Config, mock_file: Path) -> Notifier:
    webhook, secret = dingtalk_credentials(cfg)
    provider = cfg.notification.provider
    if provider == "mock":
        return MockNotifier(mock_file)
    if provider == "dingtalk":
        if not webhook or not secret:
            raise AtwError(
                SERVICE_UNAVAILABLE,
                "notification.provider=dingtalk 但未设置 ATW_DINGTALK_WEBHOOK / "
                "ATW_DINGTALK_SECRET",
            )
        return DingTalkNotifier(webhook, secret, timeout_seconds=cfg.notification.timeout_seconds)
    # auto
    if webhook and secret:
        return DingTalkNotifier(webhook, secret, timeout_seconds=cfg.notification.timeout_seconds)
    return MockNotifier(mock_file)


def provider_name(notifier: Notifier) -> str:
    return getattr(notifier, "channel", "unknown")
