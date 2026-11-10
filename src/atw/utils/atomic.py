"""Atomic file helpers and permission hardening."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from atw.utils.timeutil import dt_to_z


def secure_mkdir(path: Path) -> None:
    """Create a directory (mode 0700 on POSIX) if it does not exist."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def secure_chmod(path: Path, mode: int = 0o600) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write bytes through a same-directory temp file + fsync + rename."""
    secure_mkdir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        secure_chmod(tmp, mode)
        os.replace(tmp, path)
        # Best-effort directory fsync so the rename is durable on POSIX.
        if os.name != "nt":
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _default(value: Any) -> Any:
    from datetime import datetime
    from enum import Enum

    if isinstance(value, datetime):
        return dt_to_z(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def json_dumps(data: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(data, ensure_ascii=False, indent=2, default=_default) + "\n"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=_default)


def atomic_write_json(path: Path, data: Any, *, pretty: bool = True) -> None:
    atomic_write_bytes(path, json_dumps(data, pretty=pretty).encode("utf-8"))


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)
