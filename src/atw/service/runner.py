"""Foreground service runner used by systemd / Windows Task Scheduler."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
import uuid

from atw import __version__
from atw.config import AtwPaths, Config, resolve_allowed_roots
from atw.health.bootid import current_boot_identity
from atw.intake.scanner import InboxWatcher, WakeEvent, scan_inbox
from atw.models.task import Receipt
from atw.monitoring.evidence import EvidenceMonitor
from atw.monitoring.gpu import GpuMonitor
from atw.monitoring.process import ProcessMonitor
from atw.monitoring.supervisor import Supervisor
from atw.notifications.factory import build_notifier
from atw.notifications.worker import OutboxWorker
from atw.rules.engine import RuleEngine
from atw.service.lock import SingleInstanceLock
from atw.storage.db import Database
from atw.storage.repository import Repository
from atw.tasks.service import TaskService
from atw.utils.logging_setup import setup_logging
from atw.utils.timeutil import utc_now

logger = logging.getLogger("atw.service")


class ServiceRunner:
    def __init__(self, cfg: Config, paths: AtwPaths):
        self.cfg = cfg
        self.paths = paths

    def run(self, max_seconds: float | None = None) -> int:
        setup_logging(self.paths.log_file)
        logger.info(
            "ATW %s starting instance=%s home=%s",
            __version__,
            self.cfg.instance_id,
            self.paths.root,
        )
        lock = SingleInstanceLock(self.paths.lock_file)
        if not lock.acquire():
            logger.error("另一个 ATW 服务实例已在运行（锁 %s）", self.paths.lock_file)
            return 3
        db = Database(self.paths.db_file)
        notifier = None
        wake = WakeEvent()
        watcher = InboxWatcher(self.paths.inbox, wake.set)
        session_id = uuid.uuid4().hex
        session_started = utc_now()
        try:
            db.connect()
            repo = Repository(db)
            boot = current_boot_identity()
            notifier = build_notifier(self.cfg, self.paths.mock_notifications_file)
            process_monitor = ProcessMonitor(
                create_time_tolerance=self.cfg.process_create_time_tolerance_seconds
            )
            evidence_monitor = EvidenceMonitor(
                roots=resolve_allowed_roots(self.cfg),
                max_scan_bytes=self.cfg.max_log_scan_bytes,
                chunk_bytes=self.cfg.log_read_chunk_bytes,
            )
            gpu_monitor = GpuMonitor()
            engine = RuleEngine()
            supervisor = Supervisor(
                self.cfg,
                self.paths,
                repo,
                engine,
                process_monitor,
                evidence_monitor,
                gpu_monitor,
                channel=notifier.channel,
            )
            recovery = supervisor.recover(boot)
            logger.info(
                "boot=%s confidence=%s recovery=%s",
                boot.boot_id,
                boot.confidence,
                recovery,
            )
            repo.create_session(
                session_id=session_id,
                instance_id=self.cfg.instance_id,
                boot_id=boot.boot_id,
                boot_confidence=boot.confidence,
                pid=os.getpid(),
                started_at=session_started,
                service_version=__version__,
            )
            task_service = TaskService(
                self.cfg,
                self.paths,
                repo,
                process_monitor,
                session_id=session_id,
                boot_id=boot.boot_id,
                channel=notifier.channel,
            )
            worker = OutboxWorker(repo, notifier, self.cfg.notification)
            watcher.start()

            stop = threading.Event()

            def _stop(signum=None, frame=None):  # type: ignore[no-untyped-def]
                logger.info("收到停止信号 %s，准备优雅退出", signum)
                stop.set()

            for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                sig = getattr(signal, sig_name, None)
                if sig is not None:
                    try:
                        signal.signal(sig, _stop)
                    except (ValueError, OSError):
                        pass

            # Immediate first pass: pending inbox and previously queued outbox.
            self._drain_inbox(task_service)
            worker.run_once()
            repo.purge_retention(self.cfg.retention_days)

            last_scan = 0.0
            last_sample = 0.0
            last_outbox = 0.0
            last_health = 0.0
            last_retention = 0.0
            started_mono = time.monotonic()
            while not stop.is_set():
                now_mono = time.monotonic()
                try:
                    if now_mono - last_scan >= self.cfg.scan_interval_seconds:
                        self._drain_inbox(task_service)
                        last_scan = now_mono
                    if now_mono - last_sample >= self.cfg.sample_interval_seconds:
                        supervisor.tick()
                        last_sample = now_mono
                    if now_mono - last_outbox >= self.cfg.outbox_interval_seconds:
                        worker.run_once()
                        last_outbox = now_mono
                    if now_mono - last_health >= 30.0:
                        supervisor.maybe_daily_health(session_started)
                        last_health = now_mono
                    if now_mono - last_retention >= 3600.0:
                        removed = repo.purge_retention(self.cfg.retention_days)
                        logger.info("retention cleanup: %s", removed)
                        last_retention = now_mono
                except Exception:
                    logger.exception("服务主循环异常（继续运行）")
                if max_seconds is not None and time.monotonic() - started_mono >= max_seconds:
                    logger.info("达到 max_seconds=%s，优雅退出（测试用途）", max_seconds)
                    break
                if wake.wait(0.2):
                    self._drain_inbox(task_service)
                    last_scan = time.monotonic()

            logger.info("ATW 服务退出中 session=%s", session_id)
            return 0
        finally:
            try:
                watcher.stop()
            except Exception:
                pass
            try:
                Repository(db).close_session(session_id, utc_now(), clean=True)
            except Exception:
                logger.exception("关闭服务会话失败")
            try:
                if notifier is not None:
                    notifier.close()
            except Exception:
                pass
            try:
                db.close()
            except Exception:
                pass
            lock.release()

    def _drain_inbox(self, task_service: TaskService) -> None:
        for path in scan_inbox(self.paths):
            try:
                receipt: Receipt | None = task_service.process_file(path)
                if receipt is not None:
                    logger.info(
                        "intake %s -> %s (%s)",
                        path.name,
                        receipt.status,
                        receipt.error.code if receipt.error else "ok",
                    )
            except Exception:
                logger.exception("处理 inbox 文件失败 %s（保留待重试）", path)
