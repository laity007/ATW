# ATW V1.0 — Final Stage Report

Date: 2026-11-10 · Version 1.0.0 · Commit `9b4b0fb` (plus this report commit)

## P0 — Engineering and AFHP/1 protocol

Completed:

- Python package, Typer CLI, config/paths, strict Pydantic request/receipt
  models, generated JSON Schemas, examples.
- Atomic inbox (`*.tmp` ignored), accepted/rejected filing, atomic receipts.
- SQLite WAL + migration v1; request idempotency; revision/optimistic conflict;
  expiry; single-instance lock; `validate`, `receipt`, `doctor`.
- Windows/Linux source installation tested.

Evidence: AT-001..007; `test_intake.py`, `test_models.py`, `test_paths.py`,
`test_cli.py`; 5-repeat Linux receipt latency max **0.492 s**.

## P1 — Process, evidence, rules, terminal states

Completed:

- psutil PID+create_time identity, unique name/cmdline match, PID-reuse guard,
  recursive tree with surviving children, CPU/RSS sampling.
- Current-run log reading (hot attach, append, truncation/rotation, UTF-8),
  result freshness/size, path allow-roots/symlink checks.
- TRUE/FALSE/UNKNOWN rules, AND/OR/NOT, sustained durations, warnings, progress,
  conflict fail-closed, success positive-evidence guard.
- DingTalk Markdown + HMAC signing with mock transport.

Evidence: AT-008..021; real independent processes on Windows/Linux for
SUCCESS/FAILED/UNKNOWN; no “process gone = success” shortcut.

## P2 — Reliability, recovery, notifications

Completed:

- State machine, terminal immutability, outbox priority/rate-limit/dedupe/retry,
  task CLI, daily health summary.
- Service restart recovery, boot-id host-reboot detection, SERVICE_RECOVERED
  once, terminal tasks never reactivated.
- Five parallel isolated tasks.

Evidence: AT-022..028, AT-036/037, AT-040; Windows hard-restart E2E; Linux
`kill -9` systemd recovery; rate-limit 15/min unit test.

## P3 — GPU and Skill

Completed:

- NVML device discovery, per-process memory, explicit unavailable/UNKNOWN,
  UUID display; Windows WDDM degradation.
- `atw-task-handoff` Skill and stdlib builder: one mandatory path question,
  both/one/default path scenarios, atomic SSH delivery, receipt + PID/create-time
  verification, no long AI polling.
- Live Linux handoffs; SSH session closed and task finished under ATW.

Evidence: AT-029..033, AT-039/040; Linux RTX 3090 real process memory **512 MiB**
for a 256 MiB CUDA allocation and warning rule fired.

## P4 — Deployment, release, docs

Completed:

- Linux per-user systemd unit installer, linger, restart policy, uninstall.
- Windows Task Scheduler COM installer (AtStartup S4U / AtLogOn fallback),
  uninstall, XML reference.
- PyInstaller one-file builds on both OSes; GitHub Actions release workflow;
  checksums; README/protocol/architecture/security/FAQ/acceptance docs.
- Real DingTalk transport test, user confirmed receipt.

## Verification summary

| Suite | Result |
|---|---|
| Windows `pytest -q` | **114 passed, 1 skipped** (WDDM per-process GPU memory) |
| Linux `pytest -q` | **115 passed** |
| Schema drift | passed |
| Windows/Linux one-file binaries | version/doctor/service smoke passed |
| Real DingTalk | `ok=true, channel=dingtalk`; user confirmed |
| Real Linux handoffs | both paths, one path, defaults, rejected, SSH disconnect, crash recovery |

## Remaining / environment-limited

1. **AT-035 Windows AtStartup/no-login boot — NOT_VERIFIED.** The acceptance
   session is non-elevated: COM AtStartup+S4U returned `Access is denied`. The
   AtLogOn COM task was registered, started, verified and removed. Required
   action on the target machine: run `deploy/windows/install-task.ps1
   -TriggerType AtStartup` from an elevated PowerShell, reboot, verify
   `atw status --json` and one DingTalk task.
2. **Physical reboot sub-cases of AT-034/AT-023 — NOT_VERIFIED.** The Ubuntu
   host is shared with another logged-in user; it was not rebooted. systemd
   enable/linger, crash restart, recovery and SSH-session independence were
   verified. Required action: reboot during a maintenance window and confirm
   the unit is active with no login and old tasks become INTERRUPTED.
3. No physical GPU-less host and no AMD/Intel GPU; degradation is unit-tested
   and Windows WDDM real hardware exercised UNKNOWN paths.

## Next actions (if continuing)

- Run the two privileged/reboot verification items above and append results to
  `docs/acceptance.md`; no code change is expected.
- Push the repository, tag `v1.0.0`, let the GitHub Actions workflow build the
  release, and attach the local archives in `dist/release/`.
- Maintainer confirmation of the MIT license before public release.
