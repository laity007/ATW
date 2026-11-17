"""DingTalk custom robot webhook notifier with HMAC-SHA256 signing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import time
import urllib.parse
from collections.abc import Callable

import httpx
from httpx._utils import URLPattern, get_environment_proxies

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
        # Match the same rules and precedence HTTPX uses, including NO_PROXY.
        self._proxy_rules = sorted(
            (URLPattern(pattern), proxy)
            for pattern, proxy in (get_environment_proxies() if client is None else {}).items()
        )

    def _route(self, url: httpx.URL) -> str:
        if not self._owns_client:
            return "custom"
        for pattern, proxy in self._proxy_rules:
            if not pattern.matches(url):
                continue
            if proxy is None:
                return "direct (NO_PROXY)"
            address = httpx.URL(proxy)
            host = address.host
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host.lower() == "localhost"
            location = "loopback" if local else "remote"
            scheme = address.scheme if address.scheme in {"http", "https", "socks5", "socks5h"} else "other"
            return f"proxy ({scheme}, {location})"
        return "direct"

    @property
    def route(self) -> str:
        try:
            return self._route(httpx.URL(self.webhook))
        except httpx.InvalidURL:
            return "invalid_url"

    def send(self, message: NotificationMessage) -> SendResult:
        timestamp_ms = int(self._clock() * 1000)
        url = sign_webhook(self.webhook, self.secret, timestamp_ms)
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": message.title, "text": message.text},
        }
        route = self.route
        try:
            response = self._client.post(url, json=payload)
        except httpx.RequestError as exc:
            if request := getattr(exc, "request", None):
                route = self._route(request.url)
            if isinstance(exc, httpx.TimeoutException):
                return SendResult(False, "DingTalk 请求超时", error_code="timeout", route=route)
            if isinstance(exc, httpx.ProxyError) or (
                isinstance(exc, httpx.ConnectError) and route.startswith("proxy ")
            ):
                return SendResult(False, "经代理建立连接失败", error_code="proxy_connect_error", route=route)
            return SendResult(False, "DingTalk 网络请求失败", error_code="network_error", route=route)
        except Exception:
            return SendResult(False, "DingTalk 发送异常", error_code="send_error", route=route)
        route = self._route(response.request.url)
        if response.status_code < 200 or response.status_code >= 300:
            return SendResult(False, f"HTTP {response.status_code}", error_code="http_error", route=route)
        try:
            body = response.json()
        except ValueError:
            body = None
        errcode = body.get("errcode") if isinstance(body, dict) else None
        if type(errcode) is not int or not -(2**31) <= errcode < 2**31:
            return SendResult(False, "DingTalk 响应缺少有效业务码", error_code="invalid_response", route=route)
        if errcode != 0:
            return SendResult(False, f"DingTalk 拒绝投递 (errcode={errcode})",
                              error_code="dingtalk_rejected", route=route)
        return SendResult(ok=True, detail="delivered", route=route)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
