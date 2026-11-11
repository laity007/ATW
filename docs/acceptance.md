# ATW V1.0 Acceptance Record

- Version: 1.0.0
- Baseline: `ATW_V1_Development_Spec.md` (1.0-baseline, 2026-11-09)
- Recorded: 2026-11-10 (America/Los_Angeles) / 2026-11-10 CST (Ubuntu host)
- Status legend: **PASS** = executed and observed (by this session or by the
  maintainer on an independent target machine, recorded as attestation);
  **PASS\*** = behavior executed, with a stated environment-limited sub-case;
  **NOT_VERIFIED** = not executed in this environment (never reported as PASS).
- The two items that were initially environment-limited (Windows AtStartup/
  no-login and Linux physical reboot) were later completed by the maintainer on
  independent target machines. Raw logs were not exported to this workspace;
  the record below is an explicit maintainer attestation, not a fabricated
  local command transcript.

## 1. Environment

| Component | Windows acceptance host | Linux acceptance host |
|---|---|---|
| OS | Windows 10 Pro 10.0.19045 | Ubuntu 22.04.5 LTS, kernel 6.8.0-138-generic, systemd 249 |
| Python | 3.13.2 (uv venv; system 3.13.16) | 3.12.16 standalone (system Python is 3.10.12 < required 3.11; host has no outbound internet) |
| GPU | NVIDIA GTX 1650, driver 591.44 | 2× NVIDIA RTX 3090, driver 580.178.04 |
| ATW | 1.0.0 source + PyInstaller `atw.exe` (19.8 MB) | 1.0.0 source + PyInstaller `atw` ELF (42 MB) |
| Notification | mock for E2E; one real DingTalk test | mock for E2E |
| Service manager | Task Scheduler (COM registration) | per-user systemd + linger |

The Linux runtime was uploaded offline as python-build-standalone
`cpython-3.12.16+20261026-x86_64-unknown-linux-gnu-install_only` with a
manylinux2014/20261026 wheel set (pydantic-core 2.50.4, psutil 7.2.2,
PyYAML 6.0.3, etc.). Source was run with the standalone interpreter; no system
Python upgrade and no root service installation.

## 2. Test summary

| Suite | Command | Result |
|---|---|---|
| Windows unit/integration/GPU/E2E | `.venv\Scripts\python.exe -m pytest -q` | **116 passed, 1 skipped** (GPU per-process memory unavailable under WDDM) |
| Linux unit/integration/GPU/E2E | `../runtime/python/bin/python3 -m pytest -q` in the source tree | **117 passed** |
| Schema drift | `python -m atw.schema_export --check` | passed |
| Windows bundled exe | `atw.exe --version`, `doctor --json`, `service run --max-seconds 2` | passed |
| Linux bundled binary | `atw --version`, `doctor --json`, `service run --max-seconds 2` | passed |
| Real DingTalk | `atw notify test --json` with user-provided webhook/secret | `{"ok": true, "channel": "dingtalk"}` |
| GitHub Actions v1.0.0 | run 42968507644 (Windows + Ubuntu test, both executables, release) | all jobs success |
| Published Linux asset | downloaded v1.0.0 `atw`; `--version`, `doctor`, `service run` on Ubuntu 22.04 | works after the Ubuntu 22.04 rebuild |

The Windows GPU test is skipped only for the per-process memory value
(NVML returns `None` on this WDDM driver); device discovery and process
recognition still pass. Linux reports real per-process memory (512 MiB for a
256 MiB CUDA allocation) and the `gpu_memory_above` rule fires.

## 3. Acceptance matrix

| ID | Scenario | Result | Evidence |
|---|---|---|---|
| AT-001 | Atomic JSON delivery | **PASS** | Linux 5-repeat latency: max **0.492 s** from rename to receipt (target ≤30 s); Windows E2E `test_service_hot_intake...`; default scan 5 s |
| AT-002 | Half-written `.tmp` | **PASS** | `test_tmp_file_is_ignored`; `.tmp` is never scanned |
| AT-003 | Bad JSON/schema | **PASS** | `test_invalid_json_rejected_without_task`, `test_schema_error_rejected`; no crash, exact codes |
| AT-004 | Duplicate `request_id` | **PASS** | `test_duplicate_request_is_idempotent`; one task, original receipt replayed |
| AT-005 | Legal revision+1 | **PASS** | `test_update_revision_and_history`; revision 2, whole-object replacement, history rows 1/2 |
| AT-006 | Stale/conflicting revision | **PASS** | `test_revision_conflict_preserves_old_config`; `REVISION_CONFLICT`, old config retained |
| AT-007 | Expired unaccepted request | **PASS** | `test_expired_request_rejected`; accepted task unaffected (`test_expired_update_...`) |
| AT-008 | PID+create_time match | **PASS** | `test_validate_pid_identity_current_process`; real Linux receipts bound exact PID+create time |
| AT-009 | PID reused | **PASS** | `test_pid_reuse_is_treated_as_ended`; never binds new process |
| AT-010 | Ambiguous name/cmdline | **PASS** | `test_match_ambiguous`, `test_ambiguous_match`; `AMBIGUOUS_PROCESS`, no guess |
| AT-011 | Parent exits, child runs | **PASS** | `test_parent_exits_child_survives`; tree continues on discovered child |
| AT-012 | Command line access denied | **PASS\*** | `test_pid_access_denied_propagates` simulates OS denial; real cross-user process not available on either host |
| AT-013 | Completion log + result + tree exit | **PASS** | E2E `test_success_with_log_and_result`; exactly one SUCCESS notification after extra ticks |
| AT-014 | Failure log | **PASS** | E2E `test_failure_log`; FAILED per `on_process_exit` |
| AT-015 | Process gone, no evidence | **PASS** | E2E `test_unknown_when_process_disappears_without_evidence`; UNKNOWN, never SUCCESS |
| AT-016 | GPU metric unavailable | **PASS** | Unit unavailable/None cases; Windows WDDM `gpu_memory_above` => UNKNOWN; `gpu_util_below` always UNKNOWN without per-process util |
| AT-017 | CPU low, duration not elapsed | **PASS** | `test_sustained_condition_requires_duration` |
| AT-018 | CPU low, duration elapsed | **PASS** | Same unit + `test_warning_duration_and_single_alert`; WARNING once, monitoring continues |
| AT-019 | Old result file | **PASS** | `test_old_result_file_does_not_trigger_success`; UNKNOWN |
| AT-020 | Hot attach after log written | **PASS** | `test_hot_attach_existing_current_run_log`; current-run history parsed |
| AT-021 | Log truncation/rotation | **PASS** | `test_log_truncation_keeps_matches`, `test_log_rotation_keeps_matches` |
| AT-022 | ATW service crash/restart | **PASS** | Windows E2E hard restart; Linux `kill -9` systemd MainPID 683388 → 687903, task resumed, no duplicate accept |
| AT-023 | Host reboot | **PASS** | Maintainer-rebooted independent Linux target: pre-reboot non-terminal tasks were INTERRUPTED, not reactivated; boot-id recovery unit test also covers the logic. Raw logs not attached |
| AT-024 | Terminal task in inbox/archive | **PASS** | `test_terminal_task_is_not_reactivated`; re-register rejected `TASK_FINALIZED` |
| AT-025 | HTTP 200 but errcode ≠ 0 | **PASS** | `test_dingtalk_http_200_but_errcode_nonzero_is_failure` |
| AT-026 | DingTalk network failure | **PASS** | `test_dingtalk_network_error_does_not_leak_credentials`; outbox retry `test_retry_backoff_and_eventual_delivery` |
| AT-027 | Burst >15/min | **PASS** | `test_rate_limit_15_per_minute`: 15 delivered, 5 pending, 5 delivered after 61 s |
| AT-028 | Repeated warning | **PASS** | `test_sustained_condition_requires_duration`, warning dedupe/repeat in `test_warning_duration_and_single_alert` |
| AT-029 | Skill, user gives both paths | **PASS** | Live Linux `linux-e2e-002` with explicit log/result; receipt bound PID 684024 |
| AT-030 | Skill, user gives one path | **PASS** | Live Linux `linux-e2e-005`: explicit log, generated result path; accepted |
| AT-031 | Skill, both defaults | **PASS** | Live Linux `linux-e2e-004`: `runs/<task-id>/run.log` + `results/result.csv` generated; accepted and SUCCESS |
| AT-032 | Receipt rejected/timeout | **PASS** | Live Linux `req-linux-reject-001` → `ACCESS_DENIED`, exit 1, file in `rejected/`; CLI timeout test exit 2. Skill states never claim success |
| AT-033 | SSH disconnect | **PASS** | Linux `linux-e2e-002`: SSH session closed after accept; service stayed active; task later SUCCESS and mock notification delivered |
| AT-034 | Linux systemd, boot/no-login, restart | **PASS** | Maintainer rebooted an independent Linux target: user unit enabled with linger, auto-started without login, and recovery behaved correctly; this C2 dev host itself was not the reboot target. Unit/linger/crash/SSH tests remain in this workspace |
| AT-035 | Windows Task Scheduler boot | **PASS** | Maintainer ran `install-task.ps1 -TriggerType AtStartup` elevated on an independent Windows target, rebooted without an interactive login, and verified the service/notification. No raw task log was exported. The non-elevated dev host could not register S4U (documented, not the target) |
| AT-036 | SERVICE_RECOVERED once | **PASS** | Windows E2E and Linux systemd kill: one `SERVICE_RECOVERED`, accepted event count stays 1 |
| AT-037 | Daily health once | **PASS** | `test_daily_health_deduplicated`; second call same date does not enqueue |
| AT-038 | Shell/path injection | **PASS** | `tests/integration/test_security.py` (command field, shell rule, outside root, traversal, symlink), secrets redaction |
| AT-039 | No GPU / NVML unavailable | **PASS\*** | GPU-unavailable unit tests; Windows WDDM returns UNKNOWN (not 0) and CPU tasks succeed; no physical GPU-less host available |
| AT-040 | ≥5 parallel tasks | **PASS** | Windows E2E `test_five_parallel_tasks_are_isolated` (5×SUCCESS); Linux 5 concurrent `lat-*` tasks monitoring/finalizing |

## 4. Inbox and terminal latency (Linux, default 5 s scan / 10 s sample)

Five requests were atomically renamed at the systemd service with the default
configuration. Receipt latency (rename → receipt file):

| Request | Latency |
|---|---|
| req-lat-001 | 0.030 s |
| req-lat-002 | 0.490 s |
| req-lat-003 | 0.492 s |
| req-lat-004 | 0.490 s |
| req-lat-005 | 0.490 s |
| **max** | **0.492 s** (target ≤30 s) |

Terminal latency for the same five tasks was measured from the run log's final
mtime to `tasks.finalized_at` (the process exits about 1 s after the log write):

| Task | Terminal latency |
|---|---|
| lat-001 | 1.78 s |
| lat-002 | 1.78 s |
| lat-003 | 1.78 s |
| lat-004 | 1.78 s |
| lat-005 | 1.78 s |

All five reached SUCCESS and had isolated task rows/events; the target was
≤30 s.

## 5. Live Linux handoff records

| Task | Scenario | Receipt | Final |
|---|---|---|---|
| `linux-e2e-002` | both paths explicit; SSH closed after accept | accepted PID 684024 | SUCCESS, mock success notification |
| `linux-e2e-003` | systemd `kill -9` during monitoring | accepted PID 687395 | RECOVERY_RESUMED, then SUCCESS, one SERVICE_RECOVERED |
| `linux-e2e-004` | both paths default | accepted PID 691471 | SUCCESS |
| `linux-e2e-005` | only log path supplied; result generated | accepted PID 694463 | SUCCESS |
| `req-linux-reject-001` | intentionally invalid run directory | rejected `ACCESS_DENIED`, exit 1 | no task created |
| `lat-001..005` | 5 parallel requests, same long process | all accepted | all SUCCESS |

The real Linux half also verified:

- `linux-e2e-002` completed while the SSH session was closed; `systemctl --user
  is-active atw` stayed `active` (linger enabled).
- `linux-e2e-003`: `kill -9` on MainPID `683388`; systemd restarted as
  `687903`; task remained MONITORING with `RECOVERY_RESUMED`; accepted event
  count stayed 1; `SERVICE_RECOVERED` appeared once.
- The systemd unit is enabled and linger is enabled:
  `loginctl show-user simrunner -p Linger` → `Linger=yes`.

## 6. Real DingTalk test

One message was sent with the user-provided credentials (never echoed, never
committed):

```text
{"ok": true, "channel": "dingtalk", "title": "[ATW] 通知通道测试", "error": null}
```

The credential files remain in the workspace root and are git-ignored
(`key.txt`, `webhook.txt`). `atw doctor` reports only `credentials=configured`
and a token length, never the webhook/secret.

The user confirmed receipt in the DingTalk group: message title
`[ATW] 通知通道测试`, node `DESKTOP-LFE44U2`, channel `dingtalk`, result
“通道可用”. This is the one approved real-transport test; all E2E tests use
the mock sink.

Pre-release security hardening: a final log review found that the `httpx`
client's INFO line included the full signed DingTalk URL. Commit `05a4266`
disables the HTTP-client INFO loggers, adds a query-secret redaction filter to
every ATW log handler, and adds `tests/unit/test_logging_secrets.py`. Existing
remote ATW logs were redacted and the maintainer rotated the webhook credential
before release. No real webhook/token is present in the public repository.

## 7. Windows Task Scheduler detail

The development workstation was non-elevated: CIM `Register-ScheduledTask`
and COM AtStartup+S4U both returned `Access is denied` there, while COM AtLogOn
registered, started, verified and was removed. This established the install
script and service action but was not the boot/no-login acceptance.

The maintainer subsequently ran the elevated **AtStartup/S4U** installation on
an independent Windows target and rebooted without an interactive login. The
service came up and notifications worked. That is recorded as AT-035 **PASS**
by attestation; the raw Task Scheduler history was not exported. The
non-elevated limitation on the development workstation remains documented for
reproducibility.

## 8. Deviations and limitations (within V1 scope)

1. **Python 3.11 requirement honored offline.** Ubuntu 22.04 shipped 3.10 and
   had no internet, so a standalone CPython 3.12.16 runtime was uploaded. This
   is a deployment detail, not a requirement change.
2. **Extra documented rejection codes** (`FILE_TOO_LARGE`,
   `INVALID_REQUEST_ID`, `TASK_NOT_FOUND`, `IMMUTABLE_FIELD`,
   `INTERNAL_ERROR`, `RECEIPT_TIMEOUT`) supplement the frozen common codes.
3. **Success guard.** A success rule that only evaluates process state is
   intentionally downgraded to UNKNOWN; V1 requires positive evidence. This
   follows spec §4.3.
4. **`task.run_started_at` is immutable once set**, because it controls
   evidence freshness. Display fields (`name`, `project`, `description`) remain
   updatable.
5. **Hot-attach log scan is tail-bounded** (8 MiB default). A completion marker
   older than that tail is not claimed; this is documented in protocol.md.
6. **GPU per-process utilization is not available** from NVML; the rule is
   UNKNOWN. Per-process memory is used where the driver exposes it.
7. **Privileged/reboot acceptance was performed on independent target machines,
   not on the shared C2 development host.** The maintainer completed the Linux
   physical reboot and Windows AtStartup/no-login checks there; raw logs were
   not exported to this workspace. The C2-host systemd/linger/restart and
   boot-id simulation remain documented separately.
8. **No AMD/Intel GPU, no multi-host console, no Web API** — explicit V1
   non-goals.
9. **Task Scheduler stop is hard.** Stopping a task can leave the session
   unclean; the next start performs the normal recovery path (safe, may send
   SERVICE_RECOVERED).

## 9. Reproduce

```powershell
# Windows
.\.venv\Scripts\python.exe -m pytest -q
python -m atw.schema_export --check
.\.venv\Scripts\python.exe -m atw --home tests\_tmp\demo doctor --json
```

```bash
# Linux (offline runtime)
cd ~/atw-deploy/src
../runtime/python/bin/python3 -m pytest -q
../runtime/python/bin/atw --home ~/.local/share/atw doctor --json
systemctl --user status atw
```

Artifacts from the live handoffs are in `tests/_artifacts/` locally (git
ignored) and the remote `~/atw-runs/` directories; secrets are not included.

Published release: <https://github.com/laity007/ATW/releases/tag/v1.0.0>.
The Linux asset was rebuilt on Ubuntu 22.04 after the initial CI build required
`GLIBC_2.38`; see [`release.md`](release.md) for asset hashes and the
`release.yml` runner pin.
