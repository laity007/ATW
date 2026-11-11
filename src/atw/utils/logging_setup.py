"""Logging configuration.  Secret query parameters are always redacted."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from atw.utils.atomic import secure_mkdir

_SECRET_QUERY = re.compile(r"(?i)(access_token|sign|secret|webhook)=([^&\s\"']+)")


class RedactSecretsFilter(logging.Filter):
    """Defense in depth: strip common secret query parameters from log text.

    HTTP client libraries may log request URLs at INFO/DEBUG.  ATW disables
    those client loggers below and additionally redacts query secrets so a
    webhook/access token cannot be written to ``atw.log`` by a stray logger.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        redacted = _SECRET_QUERY.sub(r"\1=***", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def _quiet_http_clients() -> None:
    # httpx logs "HTTP Request: POST <full URL>" at INFO; the URL contains the
    # DingTalk access_token and sign.  Never let it reach a handler.
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logging(log_file: Path, *, verbose: bool = False) -> None:
    secure_mkdir(log_file.parent)
    _quiet_http_clients()
    root = logging.getLogger()
    if getattr(root, "_atw_configured", False):
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redactor = RedactSecretsFilter()
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler.addFilter(redactor)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.setLevel(logging.INFO if verbose else logging.WARNING)
    stream.addFilter(redactor)
    root.addHandler(file_handler)
    root.addHandler(stream)
    root._atw_configured = True  # type: ignore[attr-defined]
