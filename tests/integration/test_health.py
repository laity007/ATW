from __future__ import annotations

from atw.utils.timeutil import utc_now


def test_daily_health_deduplicated(atw_home, repo, supervisor) -> None:
    cfg, paths = atw_home
    cfg.health_summary.enabled = True
    cfg.health_summary.time = "00:00"
    cfg.timezone = "UTC"
    assert supervisor.maybe_daily_health(utc_now()) is True
    assert supervisor.maybe_daily_health(utc_now()) is False
    events = [row["event_type"] for row in repo.recent_events(None, 50)]
    assert events.count("DAILY_HEALTH") == 1
    assert repo.outbox_counts().get("pending") == 1


def test_daily_health_disabled(atw_home, repo, supervisor) -> None:
    cfg, paths = atw_home
    cfg.health_summary.enabled = False
    assert supervisor.maybe_daily_health(utc_now()) is False
    assert "DAILY_HEALTH" not in [row["event_type"] for row in repo.recent_events(None, 10)]
