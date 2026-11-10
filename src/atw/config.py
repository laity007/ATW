"""Configuration loading and ATW_HOME path resolution."""

from __future__ import annotations

import os
import socket
import tempfile
from dataclasses import dataclass
from datetime import UTC, tzinfo
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from atw.errors import SERVICE_UNAVAILABLE, AtwError
from atw.utils.atomic import secure_mkdir
from atw.utils.pathsafe import normalize_roots

DEFAULT_CONFIG_NAME = "config.yaml"


def default_home() -> Path:
    env = os.environ.get("ATW_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ATW"
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    if base:
        return Path(base) / "atw"
    return Path.home() / ".local" / "share" / "atw"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)


class PathsConfig(StrictModel):
    """Optional subdirectory overrides.  Empty means the protocol default."""

    inbox: str | None = None
    accepted: str | None = None
    rejected: str | None = None
    archive: str | None = None
    receipts: str | None = None
    data: str | None = None
    logs: str | None = None


class NotificationConfig(StrictModel):
    provider: Literal["auto", "dingtalk", "mock"] = "auto"
    webhook_env: str = "ATW_DINGTALK_WEBHOOK"
    secret_env: str = "ATW_DINGTALK_SECRET"
    keyword: str = "ATW"
    rate_limit_per_minute: int = Field(default=15, ge=1, le=20)
    retry_base_seconds: float = Field(default=2.0, ge=0.1)
    retry_max_seconds: float = Field(default=3600.0, ge=1.0)
    max_attempts: int = Field(default=0, ge=0)
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)


class HealthSummaryConfig(StrictModel):
    enabled: bool = False
    time: str = "08:00"
    timezone: str | None = None

    @field_validator("time")
    @classmethod
    def _time_format(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("health_summary.time 必须为 HH:MM")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ValueError("health_summary.time 必须为 HH:MM") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("health_summary.time 超出范围")
        return f"{hour:02d}:{minute:02d}"


class Config(StrictModel):
    instance_id: str = Field(default_factory=lambda: socket.gethostname())
    node_label: str | None = None
    scan_interval_seconds: float = Field(default=5.0, ge=1.0, le=300.0)
    sample_interval_seconds: float = Field(default=10.0, ge=1.0, le=3600.0)
    outbox_interval_seconds: float = Field(default=2.0, ge=1.0, le=300.0)
    finalize_grace_seconds: float = Field(default=15.0, ge=0.0, le=3600.0)
    timezone: str | None = None
    allowed_read_roots: list[str] = Field(default_factory=list)
    max_json_bytes: int = Field(default=262144, ge=1024, le=10 * 1024 * 1024)
    max_rule_depth: int = Field(default=8, ge=1, le=16)
    process_create_time_tolerance_seconds: float = Field(default=0.05, ge=0.0, le=5.0)
    retention_days: int = Field(default=30, ge=1, le=3650)
    max_log_scan_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    log_read_chunk_bytes: int = Field(default=1024 * 1024, ge=1024)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    health_summary: HealthSummaryConfig = Field(default_factory=HealthSummaryConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        if not value:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知时区: {value}") from exc
        return value

    @model_validator(mode="after")
    def _roots(self) -> Config:
        if not self.allowed_read_roots:
            self.allowed_read_roots = [str(Path.home()), tempfile.gettempdir()]
        return self

    def health_zone(self) -> tzinfo:
        """Timezone used for daily health summaries (configured or system-local)."""
        from datetime import datetime

        if self.timezone:
            return ZoneInfo(self.timezone)
        return datetime.now().astimezone().tzinfo or UTC


@dataclass(frozen=True, slots=True)
class AtwPaths:
    root: Path
    config_file: Path
    inbox: Path
    accepted: Path
    rejected: Path
    archive: Path
    receipts: Path
    data_dir: Path
    db_file: Path
    logs_dir: Path
    log_file: Path
    lock_file: Path
    mock_notifications_file: Path

    def ensure(self) -> None:
        for directory in (
            self.root,
            self.config_file.parent,
            self.inbox,
            self.accepted,
            self.rejected,
            self.archive,
            self.receipts,
            self.data_dir,
            self.logs_dir,
        ):
            secure_mkdir(directory)


def _resolve_override(root: Path, value: str | None, default: Path) -> Path:
    if not value:
        return default
    p = Path(value).expanduser()
    return p if p.is_absolute() else root / p


def build_paths(home: Path, cfg: Config, config_file: Path | None = None) -> AtwPaths:
    home = home.expanduser().resolve(strict=False)
    p = cfg.paths
    tasks = home / "tasks"
    data_dir = _resolve_override(home, p.data, home / "data")
    logs_dir = _resolve_override(home, p.logs, home / "logs")
    return AtwPaths(
        root=home,
        config_file=(config_file or (home / "config" / DEFAULT_CONFIG_NAME)),
        inbox=_resolve_override(home, p.inbox, tasks / "inbox"),
        accepted=_resolve_override(home, p.accepted, tasks / "accepted"),
        rejected=_resolve_override(home, p.rejected, tasks / "rejected"),
        archive=_resolve_override(home, p.archive, tasks / "archive"),
        receipts=_resolve_override(home, p.receipts, home / "receipts"),
        data_dir=data_dir,
        db_file=data_dir / "atw.db",
        logs_dir=logs_dir,
        log_file=logs_dir / "atw.log",
        lock_file=data_dir / "atw.lock",
        mock_notifications_file=logs_dir / "mock_notifications.jsonl",
    )


def load_yaml_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AtwError(SERVICE_UNAVAILABLE, f"无法读取配置文件 {path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AtwError(SERVICE_UNAVAILABLE, f"配置文件顶层必须是映射: {path}")
    try:
        return Config.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        raise AtwError(SERVICE_UNAVAILABLE, f"配置文件校验失败 {path}: {exc}") from exc


def load_config(
    home: Path | None = None,
    config_path: Path | None = None,
    *,
    create_dirs: bool = True,
) -> tuple[Config, AtwPaths]:
    """Resolve ATW_HOME, load config.yaml and prepare runtime directories."""
    resolved_home = (home or default_home()).expanduser()
    env_config = os.environ.get("ATW_CONFIG", "").strip()
    config_file = config_path or (
        Path(env_config) if env_config else resolved_home / "config" / DEFAULT_CONFIG_NAME
    )
    cfg = load_yaml_config(config_file)
    paths = build_paths(resolved_home, cfg, config_file)
    if create_dirs:
        paths.ensure()
    return cfg, paths


def dingtalk_credentials(cfg: Config) -> tuple[str | None, str | None]:
    webhook = os.environ.get(cfg.notification.webhook_env, "").strip() or None
    secret = os.environ.get(cfg.notification.secret_env, "").strip() or None
    return webhook, secret


def resolve_allowed_roots(cfg: Config) -> list[Path]:
    return normalize_roots(cfg.allowed_read_roots)
