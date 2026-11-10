"""Inbox discovery (watchdog wake-up + periodic fallback scan)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from atw.config import AtwPaths

logger = logging.getLogger("atw.intake")


def scan_inbox(paths: AtwPaths) -> list[Path]:
    """Return complete ``*.json`` requests.  ``*.tmp`` is never considered."""
    try:
        entries = list(paths.inbox.iterdir())
    except OSError as exc:
        logger.error("无法扫描 inbox %s: %s", paths.inbox, exc)
        return []
    out: list[Path] = []
    for path in entries:
        try:
            if path.name.startswith(".") or path.suffix != ".json":
                continue
            if path.is_symlink() or not path.is_file():
                continue
            out.append(path)
        except OSError:
            continue
    out.sort(key=lambda p: p.name)
    return out


class InboxWatcher:
    """Optional low-latency wake-up; periodic scanning remains authoritative."""

    def __init__(self, inbox: Path, on_change: callable):
        self.inbox = Path(inbox)
        self.on_change = on_change
        self._observer = None

    def start(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            callback = self.on_change

            class _Handler(FileSystemEventHandler):
                def on_created(self, event):  # type: ignore[override]
                    if not event.is_directory and str(event.src_path).endswith(".json"):
                        callback()

                def on_moved(self, event):  # type: ignore[override]
                    if not event.is_directory and str(event.dest_path).endswith(".json"):
                        callback()

                def on_modified(self, event):  # type: ignore[override]
                    if not event.is_directory and str(event.src_path).endswith(".json"):
                        callback()

            observer = Observer()
            observer.daemon = True
            observer.schedule(_Handler(), str(self.inbox), recursive=False)
            observer.start()
            self._observer = observer
            logger.info("inbox watchdog started: %s", self.inbox)
        except Exception as exc:  # pragma: no cover - platform/service integration
            logger.warning("watchdog 启动失败，仅使用周期扫描: %s", exc)
            self._observer = None

    def stop(self) -> None:
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception:
            pass
        self._observer = None


class WakeEvent:
    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def wait(self, timeout: float) -> bool:
        triggered = self._event.wait(timeout)
        if triggered:
            self._event.clear()
        return triggered
