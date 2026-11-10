# Changelog

All notable changes to Agent Task Watcher (ATW) are documented here.
Format follows Keep a Changelog; versioning follows Semantic Versioning.

## [1.0.0] — 2026-11-10

### Added
- AFHP/1 JSON file handoff protocol with atomic inbox intake, accepted/rejected
  receipts, request idempotency and optimistic revision updates.
- SQLite persistence (WAL) with versioned migrations, task state machine,
  observations, events and a durable notification outbox.
- psutil process identity binding (PID + create time), recursive process tree
  tracking, CPU/RSS sampling and PID-reuse detection.
- Tri-state (TRUE/FALSE/UNKNOWN) rule engine with AND/OR/NOT, sustained
  conditions, warnings, progress and terminal detection.
- This-run log reading (hot attach, append, truncation, rotation) and result
  file freshness checks.
- NVIDIA NVML integration with graceful `unavailable` degradation; device
  metrics are never substituted for per-task metrics.
- DingTalk custom-robot Markdown notifier with HMAC-SHA256 signing, rate
  limiting, deduplication and persistent retry.
- Service restart recovery, host reboot detection (INTERRUPTED), terminal-state
  immutability and single-instance locking.
- CLI: `status`, `list`, `show`, `history`, `validate`, `receipt`, `doctor`,
  `notify test`, `notifications`, `service run`.
- `atw-task-handoff` Skill for AI agents (path question, JSON generation, atomic
  SSH delivery, receipt and process verification).
- Linux systemd and Windows Task Scheduler deployment scripts; PyInstaller
  build configuration; GitHub Actions release workflow.
