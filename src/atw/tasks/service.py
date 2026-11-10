"""Task intake service: idempotent register/update with validated receipts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from atw.config import AtwPaths, Config, resolve_allowed_roots
from atw.errors import (
    FILE_TOO_LARGE,
    IMMUTABLE_FIELD,
    INTERNAL_ERROR,
    REQUEST_CONFLICT,
    REQUEST_EXPIRED,
    REVISION_CONFLICT,
    SCHEMA_VALIDATION_FAILED,
    TASK_ALREADY_EXISTS,
    TASK_FINALIZED,
    TASK_NOT_FOUND,
    AtwError,
)
from atw.intake.receipts import ReceiptStore
from atw.models.task import (
    BoundProcess,
    Receipt,
    ReceiptError,
    RegisterRequest,
    UpdateRequest,
    parse_request_json,
    request_content_hash,
)
from atw.monitoring.evidence import evidence_paths
from atw.monitoring.process import ProcessMonitor
from atw.notifications.templates import render_event
from atw.storage.records import TaskRecord
from atw.storage.repository import Repository
from atw.utils.pathsafe import validate_read_path
from atw.utils.timeutil import ensure_utc, utc_now

logger = logging.getLogger("atw.tasks")

ID_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")


def _rules_hash(rules: Any) -> str:
    data = rules.model_dump_json(by_alias=True, exclude_none=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _safe_id(value: Any, fallback: str) -> str:
    text = str(value) if isinstance(value, str) and value else fallback
    cleaned = "".join(ch for ch in text if ch in ID_CHARS)
    return (cleaned or "invalid")[:160]


def _raw_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TaskService:
    def __init__(
        self,
        cfg: Config,
        paths: AtwPaths,
        repo: Repository,
        process_monitor: ProcessMonitor,
        *,
        session_id: str,
        boot_id: str,
        channel: str,
        clock=utc_now,
    ):
        self.cfg = cfg
        self.paths = paths
        self.repo = repo
        self.processes = process_monitor
        self.receipts = ReceiptStore(paths.receipts)
        self.session_id = session_id
        self.boot_id = boot_id
        self.channel = channel
        self.clock = clock
        self.roots = resolve_allowed_roots(cfg)
        self.node_label = (
            cfg.node_label
            or f"{cfg.instance_id} / {os.uname().nodename if hasattr(os, 'uname') else os.environ.get('COMPUTERNAME', 'host')}"
        )

    # ------------------------------------------------------------------ intake
    def process_file(self, path: Path) -> Receipt | None:
        """Process one inbox file.  I/O errors leave it for the next scan."""
        filename_id = path.stem
        try:
            stat = path.stat()
        except OSError as exc:
            logger.error("inbox 文件 stat 失败 %s: %s", path, exc)
            return None
        max_bytes = self.cfg.max_json_bytes
        try:
            with open(path, "rb") as handle:
                raw = handle.read(max_bytes + 1)
        except OSError as exc:
            logger.error("inbox 文件读取失败 %s: %s", path, exc)
            return None
        if len(raw) > max_bytes or stat.st_size > max_bytes:
            receipt = self._reject_raw(
                raw=raw[:max_bytes],
                request_id=filename_id,
                task_id=filename_id,
                revision=1,
                code=FILE_TOO_LARGE,
                message=f"请求文件超过 {max_bytes} 字节限制",
                content_hash=hashlib.sha256(raw).hexdigest(),
                path=path,
            )
            self._publish(path, receipt)
            return receipt
        receipt = self.handle_raw(raw, filename_id=filename_id)
        self._publish(path, receipt)
        return receipt

    def handle_raw(self, raw: bytes, *, filename_id: str) -> Receipt:
        now = ensure_utc(self.clock())
        try:
            model, data = parse_request_json(raw)
        except AtwError as exc:
            data = self._try_json(raw)
            request_id = _safe_id((data or {}).get("request_id"), filename_id)
            task_id = _safe_id((data or {}).get("task_id"), request_id)
            revision = self._safe_revision((data or {}).get("revision"))
            content_hash = (
                request_content_hash(data) if data is not None else hashlib.sha256(raw).hexdigest()
            )
            return self._reject_model_meta(
                request_id=request_id,
                task_id=task_id,
                revision=revision,
                code=exc.code,
                message=exc.message,
                content_json=_raw_json(data)
                if data is not None
                else _raw_json({"raw_bytes": len(raw)}),
                content_hash=content_hash,
                now=now,
            )

        assert isinstance(model, (RegisterRequest, UpdateRequest))
        if filename_id != model.request_id:
            return self._reject_model(
                model,
                code="INVALID_REQUEST_ID",
                message=f"文件名 {filename_id}.json 与 request_id {model.request_id} 不一致",
                content_hash=request_content_hash(data),
                now=now,
            )

        content_hash = request_content_hash(data)
        existing = self.repo.get_request(model.request_id)
        if existing is not None:
            if existing["content_hash"] == content_hash:
                logger.info("幂等重放 request_id=%s", model.request_id)
                return Receipt.model_validate_json(existing["receipt_json"])
            return self._reject_model(
                model,
                code=REQUEST_CONFLICT,
                message="同一 request_id 已用于不同内容的请求；原请求结果保持不变",
                content_hash=content_hash,
                now=now,
                persist=False,
            )

        if ensure_utc(model.expires_at) <= now:
            return self._reject_model(
                model,
                code=REQUEST_EXPIRED,
                message=f"请求已于 {model.expires_at.isoformat()} 过期，尚未受理",
                content_hash=content_hash,
                now=now,
            )

        if isinstance(model, RegisterRequest):
            return self._register(model, data, content_hash, now)
        return self._update(model, data, content_hash, now)

    # ---------------------------------------------------------------- register
    def _register(
        self,
        model: RegisterRequest,
        data: dict[str, Any],
        content_hash: str,
        now: datetime,
    ) -> Receipt:
        existing = self.repo.get_task(model.task_id)
        if existing is not None:
            code = TASK_FINALIZED if existing.terminal else TASK_ALREADY_EXISTS
            return self._reject_model(
                model,
                code=code,
                message="该 task_id 已存在；重跑必须使用新的 task_id，更新请用 operation=update",
                content_hash=content_hash,
                now=now,
            )
        if model.task.run_started_at and ensure_utc(model.task.run_started_at).timestamp() > (
            now.timestamp() + 300
        ):
            return self._reject_model(
                model,
                code=SCHEMA_VALIDATION_FAILED,
                message="task.run_started_at 晚于当前时间超过 5 分钟",
                content_hash=content_hash,
                now=now,
            )
        try:
            self._validate_evidence(model.evidence, model.rules)
        except AtwError as exc:
            return self._reject_model(
                model, code=exc.code, message=exc.message, content_hash=content_hash, now=now
            )
        try:
            info = (
                self.processes.validate_pid_target(model.target)
                if model.target.kind == "pid"
                else self.processes.match_target(model.target)
            )
        except AtwError as exc:
            return self._reject_model(
                model, code=exc.code, message=exc.message, content_hash=content_hash, now=now
            )

        runtime = {
            "rules_hash": _rules_hash(model.rules),
            "warnings": {},
            "progress": {"last_epoch": now.timestamp()},
            "max_runtime_alerted": False,
            "exit_observed_at": None,
            "evidence": {},
            "start_skew_seconds": (
                info.identity.create_time_epoch - ensure_utc(model.task.run_started_at).timestamp()
                if model.task.run_started_at
                else None
            ),
        }
        record = TaskRecord(
            task_id=model.task_id,
            name=model.task.name,
            project=model.task.project,
            description=model.task.description,
            run_started_at=ensure_utc(model.task.run_started_at)
            if model.task.run_started_at
            else None,
            state="MONITORING",
            revision=1,
            target=model.target,
            evidence=model.evidence,
            rules=model.rules,
            notifications=model.notifications,
            runtime=runtime,
            created_at=now,
            updated_at=now,
            accepted_at=now,
            session_id=self.session_id,
            boot_id=self.boot_id,
        )
        receipt = Receipt(
            request_id=model.request_id,
            task_id=model.task_id,
            revision=1,
            status="accepted",
            monitoring=True,
            bound_process=BoundProcess(
                pid=info.identity.pid,
                create_time_epoch=info.identity.create_time_epoch,
            ),
            accepted_at=now,
            message="已接管该计算任务的监督",
        )
        try:
            with self.repo.db.transaction() as conn:
                self.repo.insert_task(conn, record)
                self.repo.insert_revision(
                    conn,
                    record,
                    request_id=model.request_id,
                    operation="register",
                    changes_json=None,
                    now=now,
                )
                self.repo.record_process(
                    conn,
                    task_id=record.task_id,
                    pid=info.identity.pid,
                    create_time_epoch=info.identity.create_time_epoch,
                    parent_pid=info.ppid,
                    role="root",
                    exe=(info.exe or "")[:4096] or None,
                    cmdline=info.cmdline[:8192],
                    username=info.username,
                    now=now,
                )
                self._accepted_notification(conn, record, model, now)
                self.repo.insert_request(
                    conn,
                    request_id=model.request_id,
                    task_id=model.task_id,
                    operation="register",
                    revision=1,
                    content_hash=content_hash,
                    status="accepted",
                    content_json=_raw_json(data),
                    receipt_json=receipt.model_dump_json(by_alias=True, exclude_none=True),
                    received_at=now,
                    processed_at=now,
                )
        except sqlite3.IntegrityError:
            return self._reject_model(
                model,
                code=TASK_ALREADY_EXISTS,
                message="并发注册冲突，任务已存在",
                content_hash=content_hash,
                now=now,
            )
        except Exception as exc:
            logger.exception("注册事务失败 task_id=%s", model.task_id)
            return self._reject_model(
                model,
                code=INTERNAL_ERROR,
                message=f"内部错误，任务未登记: {type(exc).__name__}",
                content_hash=content_hash,
                now=now,
            )

        self.processes.prime(model.target)
        logger.info(
            "accepted request=%s task=%s pid=%s create=%.6f",
            model.request_id,
            model.task_id,
            info.identity.pid,
            info.identity.create_time_epoch,
        )
        return receipt

    # ------------------------------------------------------------------ update
    def _update(
        self,
        model: UpdateRequest,
        data: dict[str, Any],
        content_hash: str,
        now: datetime,
    ) -> Receipt:
        task = self.repo.get_task(model.task_id)
        if task is None:
            return self._reject_model(
                model,
                code=TASK_NOT_FOUND,
                message="task_id 不存在；更新只能作用于已受理任务",
                content_hash=content_hash,
                now=now,
            )
        if task.terminal:
            return self._reject_model(
                model,
                code=TASK_FINALIZED,
                message=f"任务已处于终态 {task.state}，不可重新打开",
                content_hash=content_hash,
                now=now,
            )
        if model.revision != task.revision + 1 or model.expected_revision != task.revision:
            return self._reject_model(
                model,
                code=REVISION_CONFLICT,
                message=f"revision 冲突：当前 revision={task.revision}，"
                f"expected_revision={model.expected_revision}，请求 revision={model.revision}",
                content_hash=content_hash,
                now=now,
            )

        changes = model.changes
        new_name, new_project, new_description = task.name, task.project, task.description
        run_started_at = task.run_started_at
        if changes.task is not None:
            requested_start = changes.task.run_started_at
            if requested_start is not None and task.run_started_at is not None:
                if (
                    abs(
                        ensure_utc(requested_start).timestamp()
                        - ensure_utc(task.run_started_at).timestamp()
                    )
                    > 1.0
                ):
                    return self._reject_model(
                        model,
                        code=IMMUTABLE_FIELD,
                        message="task.run_started_at 不允许更新（影响证据归属）",
                        content_hash=content_hash,
                        now=now,
                    )
            elif requested_start is not None and task.run_started_at is None:
                run_started_at = ensure_utc(requested_start)
            new_name = changes.task.name
            new_project = changes.task.project
            new_description = changes.task.description

        new_evidence = changes.evidence if changes.evidence is not None else task.evidence
        new_rules = changes.rules if changes.rules is not None else task.rules
        new_notifications = (
            changes.notifications if changes.notifications is not None else task.notifications
        )
        try:
            self._validate_evidence(new_evidence, new_rules)
        except AtwError as exc:
            return self._reject_model(
                model, code=exc.code, message=exc.message, content_hash=content_hash, now=now
            )

        runtime = dict(task.runtime)
        rules_changed = changes.rules is not None
        if rules_changed:
            runtime["rules_hash"] = _rules_hash(new_rules)
            runtime["warnings"] = {}
            runtime["max_runtime_alerted"] = False
        if changes.evidence is not None:
            runtime["evidence"] = {}
        updated = replace(
            task,
            name=new_name,
            project=new_project,
            description=new_description,
            run_started_at=run_started_at,
            revision=model.revision,
            evidence=new_evidence,
            rules=new_rules,
            notifications=new_notifications,
            runtime=runtime,
            updated_at=now,
        )
        root_rows = [r for r in self.repo.list_processes(task.task_id) if r["role"] == "root"]
        if root_rows:
            bound = BoundProcess(
                pid=int(root_rows[-1]["pid"]),
                create_time_epoch=float(root_rows[-1]["create_time_epoch"]),
            )
        elif task.target.kind == "pid" and task.target.pid and task.target.create_time_epoch:
            bound = BoundProcess(
                pid=task.target.pid, create_time_epoch=task.target.create_time_epoch
            )
        else:
            return self._reject_model(
                model,
                code=INTERNAL_ERROR,
                message="找不到已绑定的根进程身份",
                content_hash=content_hash,
                now=now,
            )
        receipt = Receipt(
            request_id=model.request_id,
            task_id=model.task_id,
            revision=model.revision,
            status="accepted",
            monitoring=True,
            bound_process=bound,
            accepted_at=now,
            message=f"已更新监控配置到 revision {model.revision}",
        )
        try:
            with self.repo.db.transaction() as conn:
                ok = self.repo.update_task_config(
                    conn,
                    updated,
                    request_id=model.request_id,
                    changes_json=_raw_json(
                        changes.model_dump(mode="json", by_alias=True, exclude_none=True)
                    ),
                    now=now,
                )
                if not ok:
                    raise AtwError(TASK_FINALIZED, "任务在更新提交前已进入终态，旧配置保持不变")
                self.repo.record_event(
                    conn,
                    task_id=task.task_id,
                    event_type="CONFIG_UPDATED",
                    instance=f"rev{model.revision}",
                    payload={"revision": model.revision, "request_id": model.request_id},
                    now=now,
                )
                self.repo.insert_request(
                    conn,
                    request_id=model.request_id,
                    task_id=model.task_id,
                    operation="update",
                    revision=model.revision,
                    content_hash=content_hash,
                    status="accepted",
                    content_json=_raw_json(data),
                    receipt_json=receipt.model_dump_json(by_alias=True, exclude_none=True),
                    received_at=now,
                    processed_at=now,
                )
        except AtwError as exc:
            return self._reject_model(
                model, code=exc.code, message=exc.message, content_hash=content_hash, now=now
            )
        except Exception as exc:
            logger.exception("更新事务失败 task_id=%s", model.task_id)
            return self._reject_model(
                model,
                code=INTERNAL_ERROR,
                message=f"内部错误，更新未生效: {type(exc).__name__}",
                content_hash=content_hash,
                now=now,
            )
        logger.info(
            "updated task=%s revision=%s request=%s",
            task.task_id,
            model.revision,
            model.request_id,
        )
        return receipt

    # ---------------------------------------------------------------- helpers
    def _validate_evidence(self, evidence: Any, rules: Any) -> None:
        if evidence is not None:
            validate_read_path(
                evidence.run_directory,
                self.roots,
                field="evidence.run_directory",
                require_exists=True,
                expect_directory=True,
            )
            if evidence.log is not None:
                validate_read_path(evidence.log.path, self.roots, field="evidence.log.path")
            for index, path in enumerate(evidence.result_files):
                validate_read_path(path, self.roots, field=f"evidence.result_files[{index}]")
        result_files = evidence.result_files if evidence is not None else []
        for path in evidence_paths(rules, result_files):
            validate_read_path(path, self.roots, field="rule.path")

    def _accepted_notification(
        self,
        conn: sqlite3.Connection,
        record: TaskRecord,
        model: RegisterRequest,
        now: datetime,
    ) -> None:
        instance = f"rev{model.revision}"
        payload: dict[str, Any] = {
            "bound_process": {
                "pid": record.runtime.get("bound_pid"),
            }
        }
        # Root identity is available through the process row (bound below).
        root = [r for r in self.repo.list_processes(record.task_id) if r["role"] == "root"]
        if root:
            payload["bound_process"] = {
                "pid": int(root[-1]["pid"]),
                "create_time_epoch": float(root[-1]["create_time_epoch"]),
            }
        message = None
        if model.notifications.on_accepted:
            try:
                message = render_event(
                    "MONITOR_ACCEPTED", record, payload, node_label=self.node_label
                )
            except Exception:
                logger.exception("接入通知渲染失败 task_id=%s", record.task_id)
        event_payload = {"title": message.title, "text": message.text} if message else payload
        self.repo.record_event(
            conn,
            task_id=record.task_id,
            event_type="MONITOR_ACCEPTED",
            instance=instance,
            payload=event_payload,
            now=now,
        )
        if message is not None:
            self.repo.enqueue_notification(
                conn,
                task_id=record.task_id,
                event_type="MONITOR_ACCEPTED",
                instance=instance,
                channel=self.channel,
                title=message.title,
                text=message.text,
                priority=0,
                now=now,
            )

    def _reject_model(
        self,
        model: RegisterRequest | UpdateRequest,
        *,
        code: str,
        message: str,
        content_hash: str,
        now: datetime,
        persist: bool = True,
    ) -> Receipt:
        content_json = _raw_json(model.model_dump(mode="json", by_alias=True, exclude_none=True))
        return self._reject_model_meta(
            request_id=model.request_id,
            task_id=model.task_id,
            revision=model.revision,
            code=code,
            message=message,
            content_json=content_json,
            content_hash=content_hash,
            now=now,
            persist=persist,
        )

    def _reject_model_meta(
        self,
        *,
        request_id: str,
        task_id: str,
        revision: int,
        code: str,
        message: str,
        content_json: str,
        content_hash: str,
        now: datetime,
        persist: bool = True,
    ) -> Receipt:
        receipt = Receipt(
            request_id=request_id,
            task_id=task_id,
            revision=revision,
            status="rejected",
            monitoring=False,
            message=message,
            error=ReceiptError(code=code, message=message),
        )
        if persist:
            try:
                existing = self.repo.get_request(request_id)
                if existing is None:
                    with self.repo.db.transaction() as conn:
                        self.repo.insert_request(
                            conn,
                            request_id=request_id,
                            task_id=task_id,
                            operation="unknown",
                            revision=revision,
                            content_hash=content_hash,
                            status="rejected",
                            content_json=content_json,
                            receipt_json=receipt.model_dump_json(by_alias=True, exclude_none=True),
                            received_at=now,
                            processed_at=now,
                        )
            except sqlite3.IntegrityError:
                logger.warning("拒绝回执已存在 request_id=%s", request_id)
            except Exception:
                logger.exception("写入拒绝请求失败 request_id=%s", request_id)
        logger.info("rejected request=%s code=%s message=%s", request_id, code, message)
        return receipt

    def _reject_raw(
        self,
        *,
        raw: bytes,
        request_id: str,
        task_id: str,
        revision: int,
        code: str,
        message: str,
        content_hash: str,
        path: Path | None = None,
    ) -> Receipt:
        return self._reject_model_meta(
            request_id=_safe_id(request_id, "invalid"),
            task_id=_safe_id(task_id, _safe_id(request_id, "invalid")),
            revision=revision,
            code=code,
            message=message,
            content_json=_raw_json({"raw_sha256": hashlib.sha256(raw).hexdigest()}),
            content_hash=content_hash,
            now=ensure_utc(self.clock()),
        )

    def _publish(self, source: Path, receipt: Receipt) -> None:
        try:
            self.receipts.write(receipt)
        except OSError:
            logger.exception("写入回执失败 request_id=%s（将在下次扫描重放）", receipt.request_id)
            return
        target_dir = self.paths.accepted if receipt.status == "accepted" else self.paths.rejected
        target = target_dir / source.name
        try:
            os.replace(source, target)
        except OSError:
            # If the source vanished (concurrent replay), or Windows holds it,
            # leave it; the next scan replays the request idempotently.
            logger.exception("移动请求文件失败 %s -> %s", source, target)

    @staticmethod
    def _try_json(raw: bytes) -> dict[str, Any] | None:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _safe_revision(value: Any) -> int:
        try:
            revision = int(value)
        except (TypeError, ValueError):
            return 1
        return revision if revision >= 1 else 1
