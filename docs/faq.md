# FAQ and Troubleshooting

## Does ATW start, stop, kill or retry my task?

No. ATW is read-only with respect to compute processes. It never sends signals,
never restarts a process and never changes GPU allocation. The AI/external
program owns the task lifecycle.

## Why is the task UNKNOWN instead of SUCCESS?

SUCCESS requires **all** of:

1. the configured success rules evaluate TRUE;
2. the process tree has exited;
3. at least one positive evidence leaf (`log_contains`, `file_exists_fresh`,
   `file_size_above`) is part of the success rule;
4. the failure rule is not TRUE/UNKNOWN in a way that prevents success.

If the log/result path is missing, stale, unreadable, or the process vanished
without completion evidence, ATW deliberately returns UNKNOWN. It never treats
"process gone" as success.

## Why is the exit code null/unknown?

ATW is not the parent of an AI-started process, so it cannot obtain a reliable
exit status without privileged OS-specific instrumentation. V1 reports unknown
rather than guessing 0.

## Why is GPU utilization UNKNOWN?

NVML provides device utilization, not per-process utilization. Device
utilization is background only; using it for a single task would be wrong.
`gpu_util_below` therefore stays UNKNOWN. `gpu_memory_above` uses per-process
NVML memory when available; on Windows WDDM it is often unavailable.

## Why did my request get rejected?

Common receipt codes:

| Code | Cause / fix |
|---|---|
| `INVALID_JSON` | File is not complete UTF-8 JSON; write `.tmp` then atomic rename |
| `SCHEMA_VALIDATION_FAILED` | Missing/extra/wrong field; validate with `atw validate <file> --json` |
| `UNSUPPORTED_PROTOCOL` | Must be `atw.task.v1` |
| `REQUEST_EXPIRED` | `expires_at` passed before intake; submit a fresh request |
| `PROCESS_NOT_FOUND` | PID exited or name/cmdline match found nothing |
| `PROCESS_IDENTITY_MISMATCH` | PID exists but create time differs (PID reuse); query psutil again |
| `AMBIGUOUS_PROCESS` | More than one match; add distinguishing cmdline fragments or use PID |
| `ACCESS_DENIED` | Target/path belongs to another user or is unreadable |
| `UNSAFE_PATH` | Path outside `allowed_read_roots`, relative, or symlink escape |
| `TASK_ALREADY_EXISTS` | Use `operation=update` or a new `task_id` |
| `TASK_FINALIZED` | Terminal task cannot reopen; start a new run |
| `REVISION_CONFLICT` | Send `revision=current+1`, `expected_revision=current` |
| `REQUEST_CONFLICT` | Same `request_id` was used for different content |
| `FILE_TOO_LARGE` | Request exceeds `max_json_bytes` (default 256 KiB) |
| `INVALID_REQUEST_ID` | Inbox filename stem differs from `request_id` |

## The request sits in `tasks/inbox/` forever

- Is the extension exactly `.json` (not `.json.tmp`)?
- Is the file complete and readable by the ATW user?
- Is the service running? `atw status --json`, `systemctl --user status atw`.
- Check `logs/atw.log`.

## The service starts but exits immediately

Check the single-instance lock and the log:

```bash
atw status --json
tail -50 ~/.local/share/atw/logs/atw.log
```

If another process holds `data/atw.lock`, stop it first. On Windows check
`schtasks /Query /TN ATW /V`.

## DingTalk messages do not arrive

1. `atw doctor --json` shows `effective=dingtalk` and credentials configured.
2. `atw notify test` returns HTTP 2xx and `errcode=0`.
3. The robot's keyword is present (`[ATW]` titles contain `ATW`).
4. Check the outbox: `atw notifications --json`.
5. Check network/firewall; errors are sanitized and stored in `last_error`.

The mock provider writes to `logs/mock_notifications.jsonl` when no credentials
are configured.

## Why is my warning not repeating / is repeating too often?

Warnings are events, not states. A warning fires when its sustained condition
becomes TRUE, repeats at `warning_repeat_minutes`, and re-arms after it becomes
FALSE. `duration_seconds`/`for_seconds` must elapse first. A transient metric
(especially the first CPU sample) is UNKNOWN and delays a warning.

## Why did the task become INTERRUPTED after a reboot?

ATW compares the Linux boot_id / Windows MachineGuid+boot time. If the host
rebooted, every previously MONITORING task is marked INTERRUPTED and is never
reattached to a new process. This is intentional: a process cannot survive a
host reboot.

## Why did SERVICE_RECOVERED appear after a service restart, but not after a clean stop?

The notification is sent only after an **unclean** session with at least one
recovering task. A clean restart and first install do not send it. This avoids
false "crash" reports.

## Linux: service does not start at boot

Enable linger:

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
systemctl --user is-enabled atw
```

Without linger, a user service starts when the user manager starts, usually on
login.

## Windows: AtStartup registration says Access denied

Creating a BootTrigger + S4U task requires elevation/policy permission on many
Windows systems. Run the installer from an elevated PowerShell. The non-elevated
`-TriggerType AtLogOn` fallback is only a functional test, not a boot/no-login
equivalent.

## Can I use this on AMD/Intel GPUs?

GPU telemetry is NVIDIA NVML only in V1. CPU monitoring works normally; GPU
rules evaluate UNKNOWN. AMD/Intel telemetry is a V1 non-goal.

## Can I query ATW remotely?

Over SSH with the CLI, e.g.:

```bash
ssh B 'atw show <task_id> --json'
ssh B 'atw history --limit 20 --json'
```

There is no HTTP API or Web UI by design.

## Is delivery exactly-once?

No. ATW persists an outbox, deduplicates event instances, retries with backoff,
and confirms HTTP 2xx + `errcode=0`. A timeout may still have delivered the
message; the system targets reliable at-least-once attempts, not strict
exactly-once HTTP delivery.
