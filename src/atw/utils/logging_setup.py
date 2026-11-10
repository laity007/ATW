"""Logging configuration.  Secrets are never passed to log calls by design."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from atw.utils.atomic import secure_mkdir


def setup_logging(log_file: Path, *, verbose: bool = False) -> None:
    secure_mkdir(log_file.parent)
    root = logging.getLogger()
    if getattr(root, "_atw_configured", False):
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.setLevel(logging.INFO if verbose else logging.WARNING)
    root.addHandler(file_handler)
    root.addHandler(stream)
    root._atw_configured = True  # type: ignore[attr-defined]
