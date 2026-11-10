"""This-run log and result-file evidence.

Design choices:
- ``scope=current_run`` only trusts a log whose mtime is at/after the task's
  declared start (or acceptance time).  Old content is never used for success.
- On hot attach the tail (up to ``max_log_scan_bytes``) is scanned so a task
  already running before registration can still be evaluated.
- Matched literals are sticky for the run, so log rotation/truncation cannot
  erase a completion marker that was genuinely observed.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atw.errors import AtwError
from atw.models.task import Condition, RulesSpec
from atw.storage.records import TaskRecord
from atw.utils.pathsafe import validate_read_path

CARRY_CHARS = 8192


@dataclass(slots=True)
class FileEvidence:
    path: str
    exists: bool
    fresh: bool
    size: int | None
    mtime: float | None
    error: str | None = None


@dataclass(slots=True)
class LogEvidence:
    path: str | None
    available: bool
    fresh: bool
    size: int | None
    mtime: float | None
    matched: set[str] = field(default_factory=set)
    error: str | None = None
    rotated: bool = False
    truncated: bool = False
    age_seconds: float | None = None


@dataclass(slots=True)
class EvidenceSnapshot:
    log: LogEvidence
    files: dict[str, FileEvidence] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def file(self, path: str) -> FileEvidence:
        return self.files.get(path) or FileEvidence(
            path=path, exists=False, fresh=False, size=None, mtime=None, error="未评估"
        )

    def log_contains(self, value: str) -> bool | None:
        if value in self.log.matched:
            return True
        if self.log.error and not self.log.available:
            return None
        if self.log.path is None:
            return False
        if not self.log.fresh:
            return None
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "log": {
                "path": self.log.path,
                "available": self.log.available,
                "fresh": self.log.fresh,
                "size": self.log.size,
                "mtime": self.log.mtime,
                "matched": sorted(self.log.matched),
                "error": self.log.error,
                "rotated": self.log.rotated,
                "truncated": self.log.truncated,
                "age_seconds": self.log.age_seconds,
            },
            "files": {
                path: {
                    "exists": f.exists,
                    "fresh": f.fresh,
                    "size": f.size,
                    "mtime": f.mtime,
                    "error": f.error,
                }
                for path, f in self.files.items()
            },
            "errors": self.errors,
        }


def _walk(cond: Condition | None) -> Iterable[Condition]:
    if cond is None:
        return
    yield cond
    for child in (cond.all or []) + (cond.any or []):
        yield from _walk(child)
    if cond.not_ is not None:
        yield from _walk(cond.not_)


def log_literals(rules: RulesSpec) -> list[str]:
    values: set[str] = set()
    for root in (rules.success, rules.failure, *rules.warnings):
        for node in _walk(root):
            if node.kind == "log_contains" and node.value:
                values.add(node.value)
    return sorted(values)


def evidence_paths(rules: RulesSpec, result_files: list[str]) -> list[str]:
    paths = set(result_files)
    for root in (rules.success, rules.failure, *rules.warnings):
        for node in _walk(root):
            if node.kind in {"file_exists_fresh", "file_size_above"} and node.path:
                paths.add(node.path)
    return sorted(paths)


class EvidenceMonitor:
    def __init__(
        self,
        *,
        roots: list[Path],
        max_scan_bytes: int = 8 * 1024 * 1024,
        chunk_bytes: int = 1024 * 1024,
        freshness_tolerance_seconds: float = 2.0,
    ):
        self.roots = roots
        self.max_scan_bytes = max(1024, max_scan_bytes)
        self.chunk_bytes = max(1024, chunk_bytes)
        self.freshness_tolerance = freshness_tolerance_seconds

    def _start_epoch(self, record: TaskRecord) -> float:
        reference = record.run_started_at or record.accepted_at
        if reference is None:
            return 0.0
        return reference.timestamp()

    def refresh(
        self, record: TaskRecord, runtime: dict[str, Any]
    ) -> tuple[EvidenceSnapshot, dict[str, Any]]:
        state: dict[str, Any] = runtime.setdefault("evidence", {})
        started = self._start_epoch(record)
        snapshot = EvidenceSnapshot(
            log=LogEvidence(path=None, available=False, fresh=False, size=None, mtime=None)
        )
        log_spec = record.evidence.log if record.evidence else None
        literals = log_literals(record.rules)

        if log_spec is not None:
            snapshot.log, state["log"] = self._refresh_log(
                log_spec.path, log_spec.encoding, literals, state.get("log", {}), started
            )
        else:
            state["log"] = {}

        result_files = record.evidence.result_files if record.evidence else []
        for path in evidence_paths(record.rules, result_files):
            file_ev = self._check_file(path, started)
            snapshot.files[path] = file_ev
            if file_ev.error:
                snapshot.errors.append(file_ev.error)

        runtime["evidence"] = state
        return snapshot, runtime

    def _safe(self, path: str, *, require_exists: bool = False) -> Path:
        return validate_read_path(path, self.roots, require_exists=require_exists)

    def _check_file(self, path: str, started: float) -> FileEvidence:
        try:
            resolved = self._safe(path)
            st = os.stat(resolved)
        except AtwError as exc:
            return FileEvidence(path, False, False, None, None, exc.message)
        except FileNotFoundError:
            return FileEvidence(path, False, False, None, None, "文件不存在")
        except OSError as exc:
            return FileEvidence(path, False, False, None, None, f"文件不可读: {exc}")
        fresh = st.st_mtime >= (started - self.freshness_tolerance)
        return FileEvidence(
            path=str(resolved),
            exists=True,
            fresh=fresh,
            size=int(st.st_size),
            mtime=float(st.st_mtime),
            error=None if fresh else "文件早于本次运行，不视为本次结果",
        )

    def _refresh_log(
        self,
        path: str,
        encoding: str,
        literals: list[str],
        prev: dict[str, Any],
        started: float,
    ) -> tuple[LogEvidence, dict[str, Any]]:
        matched = set(prev.get("matched") or [])
        if prev.get("path") not in (None, path):
            # Evidence target changed via an update: stale matches do not apply.
            matched = set()
            prev = {}
        state = dict(prev)
        state["path"] = path
        state["matched"] = sorted(matched)
        state["literals"] = list(literals)
        try:
            resolved = self._safe(path)
            st = os.stat(resolved)
        except FileNotFoundError:
            state.update({"offset": 0, "size": 0, "identity": None, "fresh": False})
            return (
                LogEvidence(
                    path=path,
                    available=False,
                    fresh=False,
                    size=None,
                    mtime=None,
                    matched=matched,
                    error="日志文件尚不存在",
                    age_seconds=None,
                ),
                state,
            )
        except AtwError as exc:
            state["fresh"] = False
            return (
                LogEvidence(
                    path=path,
                    available=False,
                    fresh=False,
                    size=None,
                    mtime=None,
                    matched=matched,
                    error=exc.message,
                ),
                state,
            )
        except OSError as exc:
            state["fresh"] = False
            return (
                LogEvidence(
                    path=path,
                    available=False,
                    fresh=False,
                    size=None,
                    mtime=None,
                    matched=matched,
                    error=f"日志不可读: {exc}",
                ),
                state,
            )

        identity = f"{st.st_dev}:{st.st_ino}"
        size = int(st.st_size)
        mtime = float(st.st_mtime)
        fresh = mtime >= (started - self.freshness_tolerance)
        rotated = bool(state.get("identity")) and state.get("identity") != identity
        truncated = size < int(state.get("offset") or 0)
        if not fresh:
            state.update({"identity": identity, "size": size, "mtime": mtime, "fresh": False})
            # Never read stale/old-run content.  Once the file becomes fresh we
            # rescan from the tail and begin accumulating matches for this run.
            state["offset"] = 0
            state["stale_identity"] = identity
            return (
                LogEvidence(
                    path=path,
                    available=True,
                    fresh=False,
                    size=size,
                    mtime=mtime,
                    matched=matched,
                    error="日志早于本次运行，暂不采用其内容",
                    age_seconds=0.0,
                ),
                state,
            )

        try:
            text = self._read_increment(resolved, size, state, rotated=rotated or truncated)
        except OSError as exc:
            state["fresh"] = fresh
            return (
                LogEvidence(
                    path=path,
                    available=False,
                    fresh=fresh,
                    size=size,
                    mtime=mtime,
                    matched=matched,
                    error=f"读取日志失败: {exc}",
                ),
                state,
            )

        carry = str(state.get("carry") or "")
        combined = carry + text
        for literal in literals:
            if literal in combined:
                matched.add(literal)
        state["carry"] = combined[-CARRY_CHARS:]
        state.update(
            {
                "identity": identity,
                "size": size,
                "mtime": mtime,
                "fresh": True,
                "matched": sorted(matched),
                "literals": list(literals),
            }
        )
        return (
            LogEvidence(
                path=path,
                available=True,
                fresh=True,
                size=size,
                mtime=mtime,
                matched=matched,
                error=None,
                rotated=rotated,
                truncated=truncated,
                age_seconds=0.0,
            ),
            state,
        )

    def _read_increment(
        self, resolved: Path, size: int, state: dict[str, Any], *, rotated: bool
    ) -> str:
        offset = int(state.get("offset") or 0)
        # A rules update with new literals, a rotation/truncation, or a stale
        # file becoming fresh triggers a one-time tail rescan.
        needs_tail_scan = (
            rotated
            or not state.get("identity")
            or bool(state.get("stale_identity"))
            or set(state.get("literals") or []) != set(state.get("_scanned_literals") or [])
        )

        with open(resolved, "rb") as handle:
            if needs_tail_scan:
                start = max(0, size - self.max_scan_bytes)
                handle.seek(start)
                data = handle.read(self.max_scan_bytes)
                state["offset"] = max(start + len(data), size)
                state["_scanned_literals"] = list(state.get("literals") or [])
                state.pop("stale_identity", None)
                return data.decode("utf-8", errors="replace")
            if size <= offset:
                state["offset"] = size
                return ""
            handle.seek(offset)
            data = handle.read(min(self.chunk_bytes, size - offset))
            state["offset"] = offset + len(data)
            return data.decode("utf-8", errors="replace")
