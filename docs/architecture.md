# ATW V1 Architecture

## 1. Goal and non-goals

ATW is a single-host, zero-LLM supervisor. An AI agent hands a compute task to
ATW by writing an AFHP/1 JSON file; ATW verifies the process identity, monitors
it and its discovered children, evaluates evidence with a three-state rule
engine, persists everything in SQLite, and sends one-way DingTalk Markdown
notifications.

ATW **does not** start, stop, retry, signal or control compute processes. It
does not expose an HTTP API, a Web UI, a multi-host console, or a DingTalk chat
command channel. It cannot recover a service that is completely down; it only
notifies after a detected recovery.

## 2. Runtime components

```text
File intake (watchdog + 5s scan + Pydantic)
  └─ TaskService (path/process validation, idempotency, receipts)
       └─ SQLite (WAL): requests, tasks, revisions, processes,
                        observations, events, outbox, sessions
Process monitor (psutil): PID + create_time identity, recursive tree, CPU/RSS
GPU monitor (NVML): device discovery, per-process memory, explicit unavailable
Evidence monitor: this-run log tail + incremental reads, rotation/truncation,
                            result freshness/size
Rule engine: TRUE/FALSE/UNKNOWN, AND/OR/NOT, sustained durations, conflict
Supervisor: sampling, WARNING/PROGRESS events, terminal decisions, archive
Outbox worker: priority, 15/min rate limit, dedupe, exponential retry
DingTalk notifier: HMAC-SHA256, Markdown, HTTP error classification
Recovery manager: service restart vs host boot, terminal immutability
```

### 2.1 Service loop

- Single-instance file lock (`data/atw.lock`) prevents two writers.
- Inbox scan every `scan_interval_seconds` (default 5 s); watchdog events also
  wake the scanner. `.tmp` is never consumed.
- Supervisor tick every `sample_interval_seconds` (default 10 s).
- Outbox worker every `outbox_interval_seconds` (default 2 s).
- Daily health check every 30 s when enabled; retention cleanup hourly.
- SIGINT/SIGTERM (and SIGBREAK on Windows) marks the session cleanly closed.

## 3. Registration transaction

```text
parse + schema validate
  → request_id replay check (same content → original receipt)
  → expires_at check
  → path safety / allowed roots
  → process identity validation (PID+create time or unique match)
  → BEGIN IMMEDIATE
      insert task (MONITORING)
      insert revision 1 snapshot
      insert root process identity
      insert MONITOR_ACCEPTED event + outbox (if enabled)
      insert request row (stored receipt)
    COMMIT
  → atomic receipt write
  → move inbox file to accepted/rejected
```

Because the request row and task are committed together, a crash after commit
is repaired by replaying the stored receipt. Duplicate request content cannot
create a second task. A rejected request also stores its receipt for replay.

## 4. Task state machine

```text
register accepted → MONITORING
   ├─ WARNING / PROGRESS events (state stays MONITORING)
   ├─ success rules TRUE + tree exited + positive evidence → SUCCESS
   ├─ failure rules satisfied (immediate or on exit)       → FAILED
   ├─ tree exited, evidence insufficient/UNKNOWN            → UNKNOWN
   └─ host boot change detected                             → INTERRUPTED
Terminal: SUCCESS | FAILED | UNKNOWN | INTERRUPTED
```

- Terminal states are immutable (`UPDATE ... WHERE state='MONITORING'`).
- A rule conflict (success and failure both TRUE) records `RULE_CONFLICT` and
  fails closed with FAILED.
- Failure has priority over success.
- A success rule with no positive evidence leaf cannot produce SUCCESS.
- Externally started processes usually have no exit code; ATW records it as
  unknown/null, never 0.

## 5. Process identity and tree

- The root identity is `(pid, create_time_epoch)`; a live PID with a different
  create time is considered the old process ended (PID reuse). ATW never
  re-binds to the new process.
- Every sample recursively discovers children and persists their identities.
  Previously discovered children continue to be checked even after the parent
  exits.
- A polling interval cannot observe a process that is created and detaches
  entirely between samples; V1 does not escalate privileges to close that gap.
- CPU is the sum of `psutil` CPU percentages for the tree (may exceed 100%);
  RSS is the sum of RSS (shared pages can be double-counted). The first CPU
  window is a warm-up and evaluates UNKNOWN.
- Access denied on a root process rejects registration; access denied on a
  child is logged and makes the tree incomplete, so success cannot be guessed.

## 6. Evidence

- `scope=current_run`: only content whose mtime is at/after the declared run
  start (or acceptance time) is trusted. Old content is ignored until updated.
- Hot attach reads the tail up to 8 MiB; incremental reads use a persisted
  offset and chunk size.
- Rotation is detected by file identity change; truncation by size < offset.
  Matched literals remain sticky for the run so a genuine completion marker is
  not lost when the log rotates.
- `file_exists_fresh` / `file_size_above` require the file to be newer than
  run start; an old leftover result cannot trigger success.
- UTF-8 multi-byte fragments are decoded with replacement; literal matching is
  byte-safe for normal markers.

## 7. GPU semantics

NVML exposes device utilization and per-process memory, but not per-process
utilization. Therefore:

- device utilization is background only and is never used for a task rule;
- `gpu_util_below` is UNKNOWN in V1;
- `gpu_memory_above` aggregates per-process memory for bound PIDs;
- if NVML is unavailable or a process's memory is `N/A` (e.g. Windows WDDM),
  the metric is UNKNOWN, never 0;
- CPU-only supervision works with no NVIDIA GPU.

## 8. Durability and recovery

- SQLite WAL + `BEGIN IMMEDIATE`; terminal state and its event/outbox row are in
  one transaction.
- `service_sessions` stores boot identity, PID, start/end and clean-shutdown.
- On startup:
  - boot_id changed → all MONITORING tasks become INTERRUPTED, one notification
    each, never reactivated;
  - same boot + previous unclean shutdown → revalidate each process identity;
    alive tasks resume, dead tasks are decided from evidence (SUCCESS/FAILED/
    UNKNOWN), and one `SERVICE_RECOVERED` is sent;
  - clean restart → resume quietly, no recovery notification;
  - first install → no recovery notification.
- Outbox rows persist; network failures retry with capped exponential backoff.
  Delivery is at-least-once attempt with dedupe keys, not exactly-once HTTP.

## 9. Notification pipeline

Event dedupe key = `task_id + event_type + event_instance`. Terminal and
accepted messages have priority 0/1; warnings 3; progress 5; health 6.
The outbox worker limits each bot to the configured rate (default 15/min,
DingTalk cap 20/min). A send is successful only for HTTP 2xx **and**
`errcode == 0`. Errors never include the webhook URL, access token, secret or
signature.

## 10. Security boundaries

- Same OS user only; no privilege escalation.
- No shell/Python/command execution from JSON; unknown fields/rules fail.
- Evidence paths are normalized, resolved and confined to allow-roots; symlink
  escapes are rejected.
- `ATW_HOME` directories are 0700 and files 0600 on POSIX; Windows inherits the
  user profile ACL.
- Secrets come only from environment variables (or a user env file with 0600);
  they are not stored in tasks, receipts, SQLite, logs or Git.
