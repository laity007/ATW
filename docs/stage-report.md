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
| Windows `pytest -q` | **116 passed, 1 skipped** (WDDM per-process GPU memory) |
| Linux `pytest -q` | **117 passed** |
| Schema drift | passed |
| Windows/Linux one-file binaries | version/doctor/service smoke passed |
| Real DingTalk | `ok=true, channel=dingtalk`; user confirmed |
| Real Linux handoffs | both paths, one path, defaults, rejected, SSH disconnect, crash recovery |
| GitHub Actions v1.0.0 | Windows/Ubuntu tests, both executables and release job **success** |
| Published assets | `atw.exe` and `atw` downloaded, `--version`/`doctor`/service smoke passed; Linux asset verified on Ubuntu 22.04 |

## Previously environment-limited — now closed by maintainer acceptance

1. **AT-035 Windows AtStartup/no-login boot — PASS.** The maintainer ran the
   elevated AtStartup/S4U installation and a reboot without login on an
   independent Windows target machine. Raw logs were not exported to this
   workspace; recorded as maintainer attestation in `acceptance.md`.
2. **Physical reboot sub-cases of AT-034/AT-023 — PASS.** The maintainer
   rebooted an independent Linux target; systemd auto-started the user unit,
   pre-reboot monitoring did not attach to new processes, and the expected
   INTERRUPTED/recovery behavior was observed. Raw logs were not exported.
3. No physical GPU-less host and no AMD/Intel GPU; degradation is unit-tested
   and Windows WDDM real hardware exercised UNKNOWN paths.

## Release outcome

- `v1.0.0` tag and public Release published:
  <https://github.com/laity007/ATW/releases/tag/v1.0.0>
- GitHub Actions run 42968507644: Windows + Ubuntu tests, both executable
  builds, release job all successful.
- Post-release verification found the first Linux asset (built on
  `ubuntu-latest`/24.04) required `GLIBC_2.38` and failed on Ubuntu 22.04. It
  was replaced with a binary built on **Ubuntu 22.04** via a one-off workflow;
  the replacement asset passes `--version`, `doctor` and clean `service run` on
  the C2 Ubuntu 22.04 host. `release.yml` now pins `ubuntu-22.04` for future
  releases.
- Maintainer confirmed MIT (README/license updated in `b2e903f`).
- DingTalk webhook was rotated after the pre-release log-redaction fix;
  `05a4266` suppresses HTTP-client URL logging and adds a secret filter.

No blocking work remains; monitor future issue reports.
