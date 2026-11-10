"""Strict AFHP/1 models.

The JSON Schema under ``schemas/`` is generated from these models.  Examples in
``examples/`` are validated by the test suite, so a field rename cannot drift
between documentation, schema and runtime.
"""

from __future__ import annotations

import codecs
import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    ValidationError,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from atw import PROTOCOL_RECEIPT, PROTOCOL_TASK
from atw.errors import (
    INVALID_JSON,
    SCHEMA_VALIDATION_FAILED,
    UNSUPPORTED_PROTOCOL,
    AtwError,
)
from atw.utils.timeutil import dt_to_z

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
MAX_CONDITION_NODES = 64

UTCDateTime = Annotated[
    datetime,
    PlainSerializer(dt_to_z, return_type=str),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]

ConditionKind = Literal[
    "process_exited",
    "process_tree_exited",
    "log_contains",
    "log_not_updated",
    "file_exists_fresh",
    "file_size_above",
    "cpu_below",
    "gpu_util_below",
    "gpu_memory_above",
    "elapsed_above",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TreeSpec(StrictModel):
    enabled: bool = True


class Target(StrictModel):
    kind: Literal["pid", "match"]
    pid: int | None = Field(default=None, gt=0)
    create_time_epoch: float | None = Field(default=None, gt=0)
    tree: TreeSpec = Field(default_factory=TreeSpec)
    name: str | None = Field(default=None, min_length=1, max_length=512)
    cmdline_contains: list[str] = Field(default_factory=list, max_length=32)
    match_mode: Literal["unique"] = "unique"

    @field_validator("cmdline_contains")
    @classmethod
    def _needles(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item or "\x00" in item:
                raise ValueError("cmdline_contains 的每一项必须是非空字符串")
        return value

    @model_validator(mode="after")
    def _kind_fields(self) -> Target:
        if self.kind == "pid":
            if self.pid is None or self.create_time_epoch is None:
                raise ValueError("kind=pid 时必须提供 pid 与 create_time_epoch")
            if self.name is not None or self.cmdline_contains:
                raise ValueError("kind=pid 时不得提供 name/cmdline_contains")
        else:
            if not self.name or not self.cmdline_contains:
                raise ValueError("kind=match 时必须提供 name 与 cmdline_contains")
            if self.pid is not None or self.create_time_epoch is not None:
                raise ValueError("kind=match 时不得提供 pid/create_time_epoch")
        return self


class LogSpec(StrictModel):
    path: str = Field(min_length=1, max_length=4096)
    scope: Literal["current_run"] = "current_run"
    encoding: str = "utf-8"

    @field_validator("encoding")
    @classmethod
    def _encoding(cls, value: str) -> str:
        try:
            codecs.lookup(value)
        except LookupError as exc:
            raise ValueError(f"未知日志编码: {value}") from exc
        return value


class Evidence(StrictModel):
    run_directory: str = Field(min_length=1, max_length=4096)
    log: LogSpec | None = None
    result_files: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("result_files")
    @classmethod
    def _files(cls, value: list[str]) -> list[str]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("result_files 必须是非空路径字符串")
        return value


class TaskMeta(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    project: str | None = Field(default=None, max_length=256)
    run_started_at: UTCDateTime | None = None
    description: str | None = Field(default=None, max_length=2000)


class NotificationSettings(StrictModel):
    on_accepted: bool = True
    on_success: bool = True
    on_failure: bool = True
    on_unknown: bool = True
    on_interrupted: bool = True
    on_warning: bool = True
    progress_interval_minutes: int = Field(default=30, ge=0, le=10080)
    warning_repeat_minutes: int = Field(default=30, ge=1, le=10080)


class Condition(StrictModel):
    """Recursive three-state condition node (leaf, all, any, not)."""

    kind: ConditionKind | None = None
    all: list[Condition] | None = Field(default=None, min_length=1, max_length=32)
    any: list[Condition] | None = Field(default=None, min_length=1, max_length=32)
    not_: Condition | None = Field(default=None, alias="not")
    value: str | None = Field(default=None, max_length=4096)
    path: str | None = Field(default=None, max_length=4096)
    percent: float | None = Field(default=None, ge=0.0, le=100.0)
    mb: float | None = Field(default=None, gt=0.0)
    bytes: int | None = Field(default=None, ge=1)
    seconds: int | None = Field(default=None, ge=1, le=2_592_000)
    gpu_uuid: str | None = Field(default=None, max_length=128)
    for_seconds: int | None = Field(default=None, ge=0, le=2_592_000)
    duration_seconds: int | None = Field(default=None, ge=0, le=2_592_000)

    @property
    def sustain_seconds(self) -> int:
        if self.duration_seconds is not None:
            return self.duration_seconds
        return self.for_seconds or 0

    @model_validator(mode="after")
    def _shape(self) -> Condition:
        branches = [
            self.kind is not None,
            self.all is not None,
            self.any is not None,
            self.not_ is not None,
        ]
        if sum(branches) != 1:
            raise ValueError("条件节点必须且只能包含 kind/all/any/not 之一")
        # Fields added by subclasses (FailureRule/WarningRule) are allowed.
        subclass_fields = {"finalize", "id", "message"}
        if self.kind is None:
            allowed_sets = {
                "all",
                "any",
                "not",
                "for_seconds",
                "duration_seconds",
            } | subclass_fields
            provided = self._provided_fields()
            extra = provided - allowed_sets
            if extra:
                raise ValueError(f"组合条件包含不适用字段: {sorted(extra)}")
            return self

        kind = self.kind
        allowed: dict[str, set[str]] = {
            "process_exited": set(),
            "process_tree_exited": set(),
            "log_contains": {"value"},
            "log_not_updated": {"seconds"},
            "file_exists_fresh": {"path"},
            "file_size_above": {"path", "bytes"},
            "cpu_below": {"percent"},
            "gpu_util_below": {"percent", "gpu_uuid"},
            "gpu_memory_above": {"mb", "gpu_uuid"},
            "elapsed_above": {"seconds"},
        }
        base = {"kind", "for_seconds", "duration_seconds"} | subclass_fields
        provided = self._provided_fields()
        extra = provided - base - allowed[kind]
        if extra:
            raise ValueError(f"规则 {kind} 包含不适用字段: {sorted(extra)}")

        required: dict[str, tuple[str, ...]] = {
            "log_contains": ("value",),
            "log_not_updated": ("seconds",),
            "file_exists_fresh": ("path",),
            "file_size_above": ("path", "bytes"),
            "cpu_below": ("percent",),
            "gpu_util_below": ("percent",),
            "gpu_memory_above": ("mb",),
            "elapsed_above": ("seconds",),
        }
        for field_name in required.get(kind, ()):
            if getattr(self, field_name) is None:
                raise ValueError(f"规则 {kind} 缺少必填字段 {field_name}")
        return self

    def _provided_fields(self) -> set[str]:
        out: set[str] = set()
        for name in self.model_fields_set:
            out.add("not" if name == "not_" else name)
        # Aliased input ("not") may appear under not_ in fields_set already.
        return out


class FailureRule(Condition):
    finalize: Literal["immediate", "on_process_exit"] = "on_process_exit"


class WarningRule(Condition):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    message: str | None = Field(default=None, max_length=512)


def condition_depth(cond: Condition) -> int:
    children: list[Condition] = []
    if cond.all:
        children.extend(cond.all)
    if cond.any:
        children.extend(cond.any)
    if cond.not_ is not None:
        children.append(cond.not_)
    return 1 + max((condition_depth(c) for c in children), default=0)


def condition_nodes(cond: Condition) -> int:
    total = 1
    for child in (cond.all or []) + (cond.any or []):
        total += condition_nodes(child)
    if cond.not_ is not None:
        total += condition_nodes(cond.not_)
    return total


class RulesSpec(StrictModel):
    success: Condition | None = None
    failure: FailureRule | None = None
    warnings: list[WarningRule] = Field(default_factory=list, max_length=32)
    max_runtime_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    max_runtime_action: Literal["warn", "failed", "unknown"] = "warn"

    @model_validator(mode="after")
    def _limits(self) -> RulesSpec:
        for label, cond in (("success", self.success), ("failure", self.failure)):
            if cond is None:
                continue
            depth = condition_depth(cond)
            nodes = condition_nodes(cond)
            if depth > 8:
                raise ValueError(f"{label} 规则嵌套过深 ({depth} > 8)")
            if nodes > MAX_CONDITION_NODES:
                raise ValueError(f"{label} 规则节点过多 ({nodes})")
        for warning in self.warnings:
            if condition_depth(warning) > 8 or condition_nodes(warning) > MAX_CONDITION_NODES:
                raise ValueError(f"warning {warning.id} 规则过大")
        return self


class RegisterRequest(StrictModel):
    protocol: Literal["atw.task.v1"] = "atw.task.v1"  # type: ignore[assignment]
    operation: Literal["register"]
    request_id: str = Field(pattern=ID_PATTERN)
    task_id: str = Field(pattern=ID_PATTERN)
    revision: Literal[1]
    expires_at: UTCDateTime
    task: TaskMeta
    target: Target
    evidence: Evidence | None = None
    rules: RulesSpec
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)

    @model_validator(mode="after")
    def _evidence_required(self) -> RegisterRequest:
        if self.evidence is None:
            has_file_rule = False
            for cond in (self.rules.success, self.rules.failure):
                if cond is not None and _condition_has_evidence(cond):
                    has_file_rule = True
            if has_file_rule:
                raise ValueError("规则引用日志/结果文件时必须提供 evidence")
        return self


class UpdateChanges(StrictModel):
    task: TaskMeta | None = None  # whole-object replacement
    evidence: Evidence | None = None  # whole-object replacement
    rules: RulesSpec | None = None
    notifications: NotificationSettings | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> UpdateChanges:
        if not self.model_fields_set:
            raise ValueError("changes 至少包含一个可更新字段")
        return self


class UpdateRequest(StrictModel):
    protocol: Literal["atw.task.v1"] = "atw.task.v1"  # type: ignore[assignment]
    operation: Literal["update"]
    request_id: str = Field(pattern=ID_PATTERN)
    task_id: str = Field(pattern=ID_PATTERN)
    revision: int = Field(ge=2, le=2_147_483_647)
    expected_revision: int = Field(ge=1, le=2_147_483_647)
    expires_at: UTCDateTime
    changes: UpdateChanges


TaskRequest = Annotated[
    RegisterRequest | UpdateRequest,
    Field(discriminator="operation"),
]
_REQUEST_ADAPTER: TypeAdapter[Any] = TypeAdapter(TaskRequest)


def _condition_has_evidence(cond: Condition) -> bool:
    if cond.kind in {"log_contains", "file_exists_fresh", "file_size_above"}:
        return True
    for child in (cond.all or []) + (cond.any or []):
        if _condition_has_evidence(child):
            return True
    if cond.not_ is not None and _condition_has_evidence(cond.not_):
        return True
    return False


def validate_request_dict(data: dict[str, Any]) -> RegisterRequest | UpdateRequest:
    return _REQUEST_ADAPTER.validate_python(data)


def _compact_errors(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors()[:8]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid request"


def parse_request_json(raw: bytes) -> tuple[RegisterRequest | UpdateRequest, dict[str, Any]]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AtwError(INVALID_JSON, f"请求不是合法 UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AtwError(SCHEMA_VALIDATION_FAILED, "请求顶层必须是 JSON 对象")
    if data.get("protocol") != PROTOCOL_TASK:
        raise AtwError(
            UNSUPPORTED_PROTOCOL,
            f"不支持的协议版本: {data.get('protocol')!r}（期望 {PROTOCOL_TASK}）",
        )
    try:
        model = _REQUEST_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise AtwError(SCHEMA_VALIDATION_FAILED, _compact_errors(exc)) from exc
    return model, data


def request_content_hash(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BoundProcess(StrictModel):
    pid: int = Field(gt=0)
    create_time_epoch: float = Field(gt=0)


class ReceiptError(StrictModel):
    code: str
    message: str


class Receipt(StrictModel):
    protocol: Literal["atw.receipt.v1"] = PROTOCOL_RECEIPT  # type: ignore[assignment]
    request_id: str
    task_id: str
    revision: int
    status: Literal["accepted", "rejected"]
    monitoring: bool
    bound_process: BoundProcess | None = None
    accepted_at: UTCDateTime | None = None
    message: str = ""
    error: ReceiptError | None = None

    @model_validator(mode="after")
    def _consistency(self) -> Receipt:
        if self.status == "accepted":
            if not self.monitoring:
                raise ValueError("accepted 回执必须 monitoring=true")
            if self.bound_process is None:
                raise ValueError("accepted 回执必须包含 bound_process")
            if self.error is not None:
                raise ValueError("accepted 回执不得包含 error")
        else:
            if self.monitoring:
                raise ValueError("rejected 回执必须 monitoring=false")
            if self.error is None:
                raise ValueError("rejected 回执必须包含 error")
        return self

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)
