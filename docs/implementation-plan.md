# ATW V1.0 Implementation Plan

Derived from the frozen baseline `ATW_V1_Development_Spec.md` (v1.0-baseline,
2026-11-09) and `CODEX_EXECUTION_PROMPT.md`. This file records the phase plan,
environment constraints and any implementation decisions taken inside the
frozen scope. It is updated as phases close.

## 1. Repository and environment baseline (2026-11-09)

| Item | Value |
|---|---|
| Repository | `E:\Data\Desktop\Project\APP\ATW`, newly initialized git repository (no pre-existing code) |
| Development host | Windows 11, Python 3.13.16, uv 0.11.8, git 2.49 |
| Test host | `C2-Ubuntu` (Ubuntu 22.04.5, kernel 6.8, 2× RTX 3090, driver 580.178.04), SSH host id `912bb2e6-6895-4843-8697-23e281801b88` |
| Remote Python | System Python is 3.10.12; no 3.11+ package is available and the host has **no outbound internet**. |
| Remote privileges | Passwordless sudo available; `systemctl --user` works; linger is currently disabled. |

Remote Python 3.12 will be provided by uploading an official python-build-standalone
Linux x86_64 runtime plus an offline wheel set (downloaded on the Windows host).
This keeps the deployed service within the frozen `Python >= 3.11` requirement
without changing the target OS. Detail is recorded in `docs/acceptance.md`.

## 2. Package layout

```
src/atw/
  config.py            configuration + path resolution (+ env secrets)
  models/              Pydantic AFHP/1 request and receipt models
  storage/             SQLite WAL database, migrations, repositories
  intake/              inbox scanner, atomic consumption, receipts
  tasks/               registration/update service, path & process validation
  monitoring/          process tree, resources, evidence, supervisor
  rules/               tri-state rule engine
  notifications/       notifier abstraction, DingTalk, outbox, templates
  health/              boot identity, doctor, daily summary
  cli/                 Typer CLI (read-only task queries)
```

Runtime layout follows spec §3.1 under `ATW_HOME`:
`config/`, `tasks/{inbox,accepted,rejected,archive}/`, `receipts/`,
`data/atw.db`, `logs/atw.log`.

## 3. Phase plan and exit criteria

### P0 — engineering and handoff protocol
Deliver: package + CLI skeleton, config, strict models and exported JSON Schema,
atomic inbox processing, accepted/rejected receipts, SQLite migrations,
`request_id` idempotency, revision/optimistic concurrency, expiry, single
instance lock, `atw validate|receipt|doctor`.

Exit: AT-001..AT-007 automated tests pass; examples validate against the
generated schema; invalid input produces exact rejection codes without crash.

### P1 — process/evidence/rules core
Deliver: psutil identity binding, process tree, CPU/RSS sampling, this-run log
and result evidence, tri-state rule engine, SUCCESS/FAILED/UNKNOWN, Chinese
DingTalk Markdown + signing with a mock transport.

Exit: AT-008..AT-021 tests pass, including real background processes and the
"process disappeared without positive evidence => UNKNOWN" guard.

### P2 — reliability and notifications
Deliver: state machine persistence, service restart recovery, boot identity /
INTERRUPTED, outbox with dedupe + rate limit + retry, task CLI, daily health
summary, SERVICE_RECOVERED.

Exit: AT-022..AT-028 and AT-036/037 pass; five concurrent tasks isolated;
terminal tasks never re-enter MONITORING.

### P3 — GPU and Skill
Deliver: NVML device/per-process memory sampling with `unavailable` semantics,
the `atw-task-handoff` Skill, SSH atomic delivery and receipt verification,
real Linux GPU/CPU acceptance.

Exit: AT-029..AT-033, AT-039/040 pass; the Skill questions exactly once,
generates paths when omitted, and never claims success on rejected/timeout.

### P4 — cross-platform deployment and release
Deliver: systemd unit + install script, Windows Task Scheduler scripts,
PyInstaller build, GitHub Actions, README/protocol/architecture/deployment/
security/FAQ docs, acceptance report with PASS/FAIL/NOT_VERIFIED evidence.

Exit: AT-034/035/038 and full AT-001..AT-040 re-review; honest record of any
environment-limited item (actual unattended reboot cannot be simulated).

## 4. Key implementation decisions inside the frozen scope

1. **Schema source of truth** — Pydantic models generate
   `schemas/task-v1.schema.json` and `schemas/receipt-v1.schema.json` via
   `python -m atw.schema_export`; CI/tests fail if committed schemas drift.
2. **Receipt beats file movement** — registration commits request + task +
   process identity + accepted event/outbox in one `BEGIN IMMEDIATE`
   transaction, then atomically writes the receipt and moves the request. A
   crash after commit is repaired on startup by replaying the stored receipt.
3. **Conservative success** — a SUCCESS finalization requires a positive
   evidence condition (`log_contains`, `file_exists_fresh`, `file_size_above`)
   in the success rule and the process tree having exited. A cycle-only success
   rule is marked UNKNOWN instead of guessing.
4. **No exit-code guessing** — ATW is not the parent of AI-started processes;
   exit codes are recorded as `null`/unknown and never fabricated as 0.
5. **Rule conflict** — simultaneous TRUE success and failure rules produce a
   persisted `RULE_CONFLICT` event and are finalized as FAILED (failure wins).
6. **GPU** — NVML exposes per-process memory but not per-process utilization;
   `gpu_util_below` therefore evaluates UNKNOWN unless a real per-process
   utilization source exists. Device utilization is display-only.
7. **Runtime state** — log offsets, matched literals, warning timers and
   progress timers are stored as JSON on the task row so service restarts are
   lossless.
8. **Notification retry** — outbox rows remain `pending` with capped exponential
   backoff; important events are never silently dropped. Success requires HTTP
   2xx **and** DingTalk `errcode == 0`.
9. **Secrets** — only `ATW_DINGTALK_WEBHOOK` / `ATW_DINGTALK_SECRET`
   environment variables (or a protected local env file) are used. `key.txt`
   and `webhook.txt` are git-ignored and never logged; CLI diagnostics mask them.
10. **Allowed read roots** — evidence paths are normalized/resolved and must
    stay under configured roots (default: user home + temp). Symlink escape is
    rejected with `UNSAFE_PATH`.

## 5. Test strategy

- `tests/unit`: models/schema, rules tri-state, evidence readers, signature,
  limiter, path safety, state transitions.
- `tests/integration`: SQLite migrations/transactions, intake idempotency,
  revision/conflict, HTTP mock for DingTalk, recovery with simulated boot ids.
- `tests/e2e`: real background Python tasks on Windows and Linux exercising
  SUCCESS/FAILED/UNKNOWN, hot attach, log rotation, warnings, five-task
  parallelism and service restart. Mock notifier only; one explicitly approved
  real DingTalk message is sent separately for transport verification.
- Every acceptance line is recorded in `docs/acceptance.md` as PASS, FAIL or
  NOT_VERIFIED with the exact command and observed result.

## 6. Known constraints / not verified yet

- True unattended "reboot with no logged-in user" was not performed on the
  Windows workstation (non-elevated session cannot register the AtStartup/S4U
  task; no reboot). The COM AtLogOn task was registered, started and removed;
  AT-035 is **NOT_VERIFIED**.
- The Ubuntu host is shared with another logged-in user and was not rebooted.
  systemd unit/linger, crash restart, recovery, and SSH-logout persistence were
  verified; the physical reboot sub-case of AT-034/AT-023 is **NOT_VERIFIED**.
- Remote Linux host has no internet; Python 3.12.16 standalone + manylinux
  wheels were uploaded offline. Details are in `docs/acceptance.md`.
- Windows WDDM does not expose per-process GPU memory through NVML; the GPU
  memory rule is UNKNOWN there (verified), while Linux RTX 3090 exposes real
  per-process memory (verified).
- AMD/Intel GPU telemetry and multi-host consoles are explicit V1 non-goals.

## 7. Phase status (2026-11-10)

| Phase | Status | Evidence |
|---|---|---|
| P0 engineering/protocol | **complete** | 40+ P0 tests, schema drift check, live receipts |

| P1 process/evidence/rules | **complete** | real process trees, SUCCESS/FAILED/UNKNOWN E2E |
| P2 reliability/notifications | **complete** | hard restart E2E, boot-id simulation, outbox/rate limits |
| P3 GPU + Skill | **complete** | Linux NVML 512 MiB process sample; live 3 path scenarios |
| P4 deployment/release | **complete with 2 environment-limited items** | systemd + Task Scheduler scripts, Windows/Linux PyInstaller builds, docs, GitHub Actions |

Full acceptance matrix and limitations: [`acceptance.md`](acceptance.md).
