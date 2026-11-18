"""Publish prepared requests through the existing inbox and wait for their result."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path, PureWindowsPath

import psutil

from atw import PROTOCOL_PROGRESS
from atw.config import AtwPaths, Config
from atw.errors import (
    FILE_TOO_LARGE,
    INVALID_REQUEST_ID,
    RECEIPT_TIMEOUT,
    REQUEST_CONFLICT,
    UNSUPPORTED_PROTOCOL,
    AtwError,
)
from atw.models.task import Receipt, parse_request_json, request_content_hash
from atw.service.lock import SingleInstanceLock
from atw.storage.repository import Repository
from atw.utils.atomic import atomic_write_bytes


def read_request(file: Path, limit: int) -> bytes:
    with file.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise AtwError(FILE_TOO_LARGE, f"请求文件超过 {limit} 字节")
    return raw


def active_protocols(paths: AtwPaths, repo: Repository) -> list[str]:
    session = repo.last_session()
    raw = repo.get_state("protocol_capabilities")
    if not session or session["ended_at"] or not raw or not paths.lock_file.is_file():
        return []
    caps = json.loads(raw)
    if caps["session_id"] != session["session_id"]:
        return []
    lock = SingleInstanceLock(paths.lock_file)
    if lock.acquire():
        lock.release()
        return []
    try:
        process = psutil.Process(session["pid"])
        same_process = process.is_running() and process.create_time() == caps["create_time"]
    except (psutil.Error, KeyError):
        return []
    return caps["protocols"] if same_process else []


def submit_request(
    raw: bytes, cfg: Config, paths: AtwPaths, repo: Repository, wait: float
) -> Receipt:
    if len(raw) > cfg.max_json_bytes:
        raise AtwError(FILE_TOO_LARGE, "请求超过服务配置的字节限制")
    model, data = parse_request_json(raw)
    request_id = model.request_id
    target = paths.inbox / f"{request_id}.json"
    reserved = getattr(os.path, "isreserved", None)
    if os.name == "nt" and (
        reserved(target.name) if reserved else PureWindowsPath(target.name).is_reserved()
    ):
        raise AtwError(INVALID_REQUEST_ID, "此请求 ID 不能用作 Windows 文件名")
    digest = request_content_hash(data)

    def committed() -> Receipt | None:
        row = repo.get_request_for_file(request_id)
        if row is None:
            return None
        if row["request_id"] != request_id or row["content_hash"] != digest:
            raise AtwError(REQUEST_CONFLICT, "请求 ID 已用于不同内容")
        return Receipt.model_validate_json(row["receipt_json"])

    receipt = committed()
    if receipt is not None:
        return receipt
    if model.protocol == PROTOCOL_PROGRESS and PROTOCOL_PROGRESS not in active_protocols(paths, repo):
        raise AtwError(UNSUPPORTED_PROTOCOL, "运行中的 ATW 未确认支持进度协议，请检查或升级服务")
    try:
        atomic_write_bytes(target, raw, replace=False)
    except FileExistsError:
        try:
            queued, queued_data = parse_request_json(read_request(target, cfg.max_json_bytes))
        except FileNotFoundError:
            # A consumed file is removed only after the canonical request commits.
            receipt = committed()
            if receipt is not None:
                return receipt
            raise
        except AtwError as exc:
            raise AtwError(REQUEST_CONFLICT, "inbox 中已有同名且无效的请求") from exc
        if queued.request_id != request_id or request_content_hash(queued_data) != digest:
            raise AtwError(REQUEST_CONFLICT, "inbox 中同名请求的 ID 或内容不同") from None

    deadline = time.monotonic() + wait
    while True:
        receipt = committed()
        if receipt is not None:
            return receipt
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AtwError(RECEIPT_TIMEOUT, f"{wait:g} 秒内未收到回执，继续查询 {request_id}")
        time.sleep(min(0.2, remaining))
