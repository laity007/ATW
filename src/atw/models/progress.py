"""Declarative progress sources and per-item reports."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_ITEMS = 256
CSV_LIMIT = 8 * 1024 * 1024
REPORT_LIMIT = 16 * 1024
ItemId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
FilePath = Annotated[str, Field(min_length=1, max_length=4096)]
Count = Annotated[int, Field(strict=True, ge=0, le=10**12)]
State = Literal["queued", "running", "completed", "failed", "skipped"]
Acceptance = Literal["accepted", "rejected", "pending"]


class ProgressModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CsvSource(ProgressModel):
    path: FilePath
    column: str = Field(min_length=1, max_length=128)
    counter_base: Literal[0, 1] = 1


class CsvProgress(CsvSource):
    kind: Literal["csv"]
    total: Count = Field(gt=0)
    unit: str = Field(min_length=1, max_length=64)
    stale_after_seconds: int | None = Field(default=None, gt=0)


class QueueItem(ProgressModel):
    id: ItemId
    total: Count = Field(gt=0)
    unit: str = Field(min_length=1, max_length=64)
    weight: float = Field(default=1, gt=0)
    required: bool = True
    report: FilePath
    csv: CsvSource | None = None


class QueueProgress(ProgressModel):
    kind: Literal["queue"]
    items: list[QueueItem] = Field(min_length=1, max_length=MAX_ITEMS)
    stale_after_seconds: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def unique_items(self) -> QueueProgress:
        if len({item.id.casefold() for item in self.items}) != len(self.items):
            raise ValueError("队列子任务 ID 必须唯一")
        return self


ProgressSpec = Annotated[CsvProgress | QueueProgress, Field(discriminator="kind")]


class QueuePlan(ProgressModel):
    schema_version: Literal["atw.progress.plan.v1"] = Field(alias="schema")
    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    items: list[QueueItem] = Field(min_length=1, max_length=MAX_ITEMS)
    stale_after_seconds: int | None = Field(default=None, gt=0)


class ItemReport(ProgressModel):
    task_id: str
    item_id: ItemId
    attempt: int = Field(strict=True, ge=1, le=2**31 - 1)
    state: State
    completed: Count | None = None
    updated_at: datetime
    acceptance: Acceptance | None = None

    @model_validator(mode="after")
    def validate_report(self) -> ItemReport:
        if self.updated_at.tzinfo is None:
            raise ValueError("updated_at 必须含时区")
        self.updated_at = self.updated_at.astimezone(UTC)
        if self.state in {"queued", "skipped"} and self.completed not in {None, 0}:
            raise ValueError("排队和跳过的子任务完成量必须为 0")
        if self.acceptance in {"accepted", "rejected"} and self.state != "completed":
            raise ValueError("验收需要子任务已执行完成")
        return self
