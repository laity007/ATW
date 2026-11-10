"""Cross-platform non-blocking single-instance file lock."""

from __future__ import annotations

import os
from pathlib import Path

from atw.utils.atomic import secure_mkdir


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None
        self._locked = False

    def acquire(self) -> bool:
        secure_mkdir(self.path.parent)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n".encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
            self._handle = handle
            self._locked = True
            return True
        except OSError:
            handle.close()
            return False

    def release(self) -> None:
        if not self._locked or self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._handle.close()
        finally:
            self._handle = None
            self._locked = False

    def __enter__(self) -> SingleInstanceLock:
        if not self.acquire():
            raise RuntimeError("另一个 ATW 服务实例已持有单实例锁")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
