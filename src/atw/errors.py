"""ATW error codes and the exception used for protocol rejections."""

from __future__ import annotations

from dataclasses import dataclass

# Rejection codes defined by AFHP/1 (spec §3.7) plus a small number of
# implementation-level codes that are documented in docs/protocol.md.
INVALID_JSON = "INVALID_JSON"
UNSUPPORTED_PROTOCOL = "UNSUPPORTED_PROTOCOL"
SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
REQUEST_EXPIRED = "REQUEST_EXPIRED"
PROCESS_NOT_FOUND = "PROCESS_NOT_FOUND"
PROCESS_IDENTITY_MISMATCH = "PROCESS_IDENTITY_MISMATCH"
AMBIGUOUS_PROCESS = "AMBIGUOUS_PROCESS"
ACCESS_DENIED = "ACCESS_DENIED"
TASK_ALREADY_EXISTS = "TASK_ALREADY_EXISTS"
TASK_NOT_FOUND = "TASK_NOT_FOUND"
TASK_FINALIZED = "TASK_FINALIZED"
IMMUTABLE_FIELD = "IMMUTABLE_FIELD"
REVISION_CONFLICT = "REVISION_CONFLICT"
REQUEST_CONFLICT = "REQUEST_CONFLICT"
UNSAFE_PATH = "UNSAFE_PATH"
# Implementation-level codes (explicitly documented, still protocol errors).
FILE_TOO_LARGE = "FILE_TOO_LARGE"
INVALID_REQUEST_ID = "INVALID_REQUEST_ID"
INTERNAL_ERROR = "INTERNAL_ERROR"
SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
RECEIPT_TIMEOUT = "RECEIPT_TIMEOUT"


@dataclass(slots=True)
class AtwError(Exception):
    """A controlled rejection that is safe to expose in a receipt."""

    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}
