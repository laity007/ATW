from __future__ import annotations

import logging
from pathlib import Path

from atw.utils.logging_setup import RedactSecretsFilter, setup_logging


def test_secret_query_parameters_are_redacted() -> None:
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "HTTP Request: POST %s",
        (
            "https://oapi.dingtalk.com/robot/send?access_token=SECRET-TOKEN&"
            "timestamp=1700000000000&sign=SIGN-VALUE",
        ),
        None,
    )
    RedactSecretsFilter().filter(record)
    message = record.getMessage()
    assert "SECRET-TOKEN" not in message
    assert "SIGN-VALUE" not in message
    assert "access_token=***" in message
    assert "sign=***" in message


def test_http_client_loggers_are_quiet_after_setup(tmp_path: Path) -> None:
    setup_logging(tmp_path / "atw.log")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
