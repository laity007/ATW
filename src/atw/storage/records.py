"""Typed task records loaded from SQLite."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from atw.models.task import (
    Evidence,
    NotificationSettings,
    RulesSpec,
    Target,
)
from atw.utils.timeutil import parse_iso


def _model_dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    name: str
    project: str | None
    description: str | None
    run_started_at: datetime | None
    state: str
    revision: int
    target: Target
    evidence: Evidence | None
    rules: RulesSpec
    notifications: NotificationSettings
    runtime: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    accepted_at: datetime | None = None
    finalized_at: datetime | None = None
    finalize_reason: str | None = None
    finalize_evidence: str | None = None
    session_id: str | None = None
    boot_id: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> TaskRecord:
        return cls(
            task_id=row["task_id"],
            name=row["name"],
            project=row["project"],
            description=row["description"],
            run_started_at=parse_iso(row["run_started_at"]),
            state=row["state"],
            revision=row["current_revision"],
            target=Target.model_validate_json(row["target_json"]),
            evidence=Evidence.model_validate_json(row["evidence_json"])
            if row["evidence_json"]
            else None,
            rules=RulesSpec.model_validate_json(row["rules_json"]),
            notifications=NotificationSettings.model_validate_json(row["notifications_json"]),
            runtime=json.loads(row["runtime_json"] or "{}"),
            created_at=parse_iso(row["created_at"]),
            updated_at=parse_iso(row["updated_at"]),
            accepted_at=parse_iso(row["accepted_at"]),
            finalized_at=parse_iso(row["finalized_at"]),
            finalize_reason=row["finalize_reason"],
            finalize_evidence=row["finalize_evidence"],
            session_id=row["session_id"],
            boot_id=row["boot_id"],
        )

    @property
    def terminal(self) -> bool:
        return self.state in {"SUCCESS", "FAILED", "UNKNOWN", "INTERRUPTED"}

    def task_meta_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.project is not None:
            out["project"] = self.project
        if self.description is not None:
            out["description"] = self.description
        if self.run_started_at is not None:
            out["run_started_at"] = self.run_started_at.isoformat()
        return out

    def config_snapshot(self) -> dict[str, Any]:
        return {
            "task": self.task_meta_dict(),
            "target": _model_dump(self.target),
            "evidence": _model_dump(self.evidence) if self.evidence else None,
            "rules": _model_dump(self.rules),
            "notifications": _model_dump(self.notifications),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "revision": self.revision,
            "task": self.task_meta_dict(),
            "target": _model_dump(self.target),
            "evidence": _model_dump(self.evidence) if self.evidence else None,
            "rules": _model_dump(self.rules),
            "notifications": _model_dump(self.notifications),
            "runtime": self.runtime,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
            "finalized_at": self.finalized_at.isoformat() if self.finalized_at else None,
            "finalize_reason": self.finalize_reason,
            "finalize_evidence": self.finalize_evidence,
        }
