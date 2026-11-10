from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from atw.config import NotificationConfig
from atw.notifications.base import NotificationMessage, SendResult
from atw.notifications.dingtalk import DingTalkNotifier, sign_webhook
from atw.notifications.mock import MockNotifier
from atw.notifications.worker import OutboxWorker


def _expected_sign(secret: str, timestamp_ms: int) -> str:
    digest = hmac.new(
        secret.encode(), f"{timestamp_ms}\n{secret}".encode(), hashlib.sha256
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest).decode())


def test_sign_webhook_matches_spec() -> None:
    webhook = "https://oapi.dingtalk.com/robot/send?access_token=abc"
    secret = "SEC-test-secret"
    timestamp = 1_700_000_000_000
    url = sign_webhook(webhook, secret, timestamp)
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert params["timestamp"] == [str(timestamp)]
    assert params["access_token"] == ["abc"]
    assert params["sign"] == [urllib.parse.unquote_plus(_expected_sign(secret, timestamp))]


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_dingtalk_success_requires_errcode_zero() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    notifier = DingTalkNotifier(
        "https://oapi.dingtalk.com/robot/send?access_token=TOKEN",
        "SECRET",
        client=_client(handler),
        clock=lambda: 1_700_000_000.0,
    )
    result = notifier.send(NotificationMessage("[ATW] 测试", "### [ATW] 测试"))
    assert result.ok
    assert calls
    body = json.loads(calls[0].content)
    assert body["msgtype"] == "markdown"
    assert body["markdown"]["title"] == "[ATW] 测试"
    assert "timestamp=1700000000000" in str(calls[0].url)
    assert "sign=" in str(calls[0].url)
    notifier.close()


def test_dingtalk_http_200_but_errcode_nonzero_is_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 310000, "errmsg": "keyword not in content"})

    notifier = DingTalkNotifier(
        "https://oapi.dingtalk.com/robot/send?access_token=SECRET-TOKEN",
        "SECRET",
        client=_client(handler),
    )
    result = notifier.send(NotificationMessage("[ATW] x", "x"))
    assert not result.ok
    assert "310000" in (result.error or "")
    assert "SECRET-TOKEN" not in (result.error or "")
    notifier.close()


def test_dingtalk_network_error_does_not_leak_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    notifier = DingTalkNotifier(
        "https://oapi.dingtalk.com/robot/send?access_token=SECRET-TOKEN",
        "SECRET",
        client=_client(handler),
    )
    result = notifier.send(NotificationMessage("[ATW] x", "x"))
    assert not result.ok
    assert "SECRET" not in (result.error or "")
    assert "access_token" not in (result.error or "")
    notifier.close()


def test_mock_notifier_writes_jsonl(tmp_path: Path) -> None:
    sink = tmp_path / "mock.jsonl"
    notifier = MockNotifier(sink)
    assert notifier.send(NotificationMessage("[ATW] one", "text")).ok
    data = json.loads(sink.read_text(encoding="utf-8").strip())
    assert data["title"] == "[ATW] one"


class FakeNotifier:
    channel = "fake"

    def __init__(self, failures: int = 0):
        self.failures = failures
        self.sent: list[NotificationMessage] = []

    def send(self, message: NotificationMessage) -> SendResult:
        if self.failures > 0:
            self.failures -= 1
            return SendResult(False, "network down")
        self.sent.append(message)
        return SendResult(True)

    def close(self) -> None:
        pass


def _enqueue(repo, count: int, now: datetime, prefix: str = "p") -> None:
    with repo.db.transaction() as conn:
        for index in range(count):
            repo.enqueue_notification(
                conn,
                task_id=None,
                event_type="PROGRESS",
                instance=f"{prefix}:{index}",
                channel="fake",
                title=f"[ATW] {prefix}{index}",
                text="text",
                priority=5,
                now=now,
            )


def test_rate_limit_15_per_minute(atw_home, repo) -> None:
    cfg, paths = atw_home
    now = datetime(2026, 1, 1, tzinfo=UTC)
    clock = {"now": now}
    _enqueue(repo, 20, now)

    def now_fn():
        return clock["now"]

    notifier = FakeNotifier()
    worker = OutboxWorker(
        repo,
        notifier,
        NotificationConfig(rate_limit_per_minute=15, retry_base_seconds=1, retry_max_seconds=10),
        clock=now_fn,
    )
    assert worker.run_once(limit=50) == 15
    assert len(notifier.sent) == 15
    assert repo.outbox_counts().get("pending") == 5
    clock["now"] = now + timedelta(seconds=61)
    assert worker.run_once(limit=50) == 5
    assert len(notifier.sent) == 20
    assert repo.outbox_counts().get("delivered") == 20


def test_retry_backoff_and_eventual_delivery(atw_home, repo) -> None:
    cfg, paths = atw_home
    now = datetime(2026, 1, 1, tzinfo=UTC)
    clock = {"now": now}
    _enqueue(repo, 1, now)

    notifier = FakeNotifier(failures=2)
    worker = OutboxWorker(
        repo,
        notifier,
        NotificationConfig(rate_limit_per_minute=15, retry_base_seconds=1, retry_max_seconds=10),
        clock=lambda: clock["now"],
    )
    assert worker.run_once() == 0
    row = repo.recent_notifications(1)[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    clock["now"] = clock["now"] + timedelta(seconds=2)
    assert worker.run_once() == 0
    row = repo.recent_notifications(1)[0]
    assert row["attempts"] == 2
    clock["now"] = clock["now"] + timedelta(seconds=5)
    assert worker.run_once() == 1
    row = repo.recent_notifications(1)[0]
    assert row["status"] == "delivered"


def test_outbox_deduplicates_same_event(atw_home, repo) -> None:
    cfg, paths = atw_home
    now = datetime.now(UTC)
    with repo.db.transaction() as conn:
        first = repo.enqueue_notification(
            conn,
            task_id="t",
            event_type="SUCCESS",
            instance="final:1",
            channel="fake",
            title="a",
            text="b",
            priority=0,
            now=now,
        )
        second = repo.enqueue_notification(
            conn,
            task_id="t",
            event_type="SUCCESS",
            instance="final:1",
            channel="fake",
            title="a",
            text="b",
            priority=0,
            now=now,
        )
    assert first is True
    assert second is False
    assert repo.outbox_counts().get("pending") == 1
