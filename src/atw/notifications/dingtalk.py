"""DingTalk custom robot webhook notifier with HMAC-SHA256 signing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from collections.abc import Callable

import httpx

from atw.notifications.base import NotificationMessage, SendResult


def sign_webhook(webhook: str, secret: str, timestamp_ms: int) -> str:
    """Append the DingTalk ``timestamp``/``sign`` query parameters."""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
    separator = "&" if "?" in webhook else "?"
    return f"{webhook}{separator}timestamp={timestamp_ms}&sign={sign}"


def _redact(text: str, secrets: list[str]) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out[:1000]


class DingTalkNotifier:
    channel = "dingtalk"

    def __init__(
        self,
        webhook: str,
        secret: str,
        *,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.webhook = webhook
        self.secret = secret
        self.timeout = timeout_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        self._owns_client = client is None
        self._clock = clock

    def send(self, message: NotificationMessage) -> SendResult:
        timestamp_ms = int(self._clock() * 1000)
        url = sign_webhook(self.webhook, self.secret, timestamp_ms)
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": message.title, "text": message.text},
        }
        secrets = [self.secret, self.webhook]
        token = ""
        try:
            query = urllib.parse.urlparse(self.webhook).query
            params = urllib.parse.parse_qs(query)
            token = (params.get("access_token") or [""])[0]
            if token:
                secrets.append(token)
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            response = self._client.post(url, json=payload)
        except httpx.TimeoutException:
            return SendResult(ok=False, error="DingTalk 请求超时")
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=_redact(f"网络错误: {type(exc).__name__}", secrets))
        except Exception as exc:  # pragma: no cover - defensive
            return SendResult(ok=False, error=_redact(f"发送异常: {type(exc).__name__}", secrets))
        if response.status_code < 200 or response.status_code >= 300:
            return SendResult(
                ok=False,
                error=_redact(f"HTTP {response.status_code}", secrets),
                detail=_redact(response.text, secrets),
            )
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            return SendResult(ok=False, error="DingTalk 返回非 JSON 响应")
        errcode = body.get("errcode")
        if errcode != 0:
            errmsg = str(body.get("errmsg") or "")
            return SendResult(
                ok=False,
                error=_redact(f"errcode={errcode}: {errmsg}", secrets),
                detail=_redact(response.text, secrets),
            )
        return SendResult(ok=True, detail="delivered")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
