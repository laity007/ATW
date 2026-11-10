"""Time helpers.  All persisted timestamps are timezone-aware UTC ISO strings."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def dt_to_z(value: datetime | None) -> str | None:
    """Serialize to the protocol's ISO-8601 ``...Z`` form."""
    if value is None:
        return None
    return ensure_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(text))


def epoch_to_dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


def duration_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分钟"
    if minutes:
        return f"{minutes}分钟{secs}秒"
    return f"{secs}秒"
