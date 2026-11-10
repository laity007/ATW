# AFHP/1 — ATW File Handoff Protocol v1

Status: frozen by `ATW_V1_Development_Spec.md` (1.0-baseline).
Machine-readable schema: [`schemas/task-v1.schema.json`](../schemas/task-v1.schema.json)
and [`schemas/receipt-v1.schema.json`](../schemas/receipt-v1.schema.json).
Examples: [`examples/`](../examples/).

## 1. Directory contract

`ATW_HOME` is resolved from `--home`, then `ATW_HOME`, then the platform default
(Linux `~/.local/share/atw`, Windows `%LOCALAPPDATA%\ATW`).

```text
ATW_HOME/
├── config/config.yaml
├── tasks/inbox/          # the only new-request entry point
├── tasks/accepted/       # accepted request files
├── tasks/rejected/       # rejected request files
├── tasks/archive/        # terminal task JSON snapshots
├── receipts/<request_id>.json
├── data/atw.db           # authoritative state (SQLite WAL)
└── logs/atw.log
```

Only `tasks/inbox/*.json` is consumed. `*.tmp` is ignored and never registered.
The file name stem must equal `request_id`.

## 2. Atomic submission

1. Write the complete JSON to `inbox/<request_id>.json.tmp`.
2. Close the file handle.
3. Atomically rename to `inbox/<request_id>.json` on the same filesystem.
4. ATW validates and consumes it; the original file moves to `accepted/` or
   `rejected/`, and a receipt appears in `receipts/`.

The scanner has a periodic fallback (default 5 s) plus a watchdog wake-up.
Default request size limit: 256 KiB.

## 3. Identifiers

| Field | Rule |
|---|---|
| `request_id` | Unique per submission, never reused. `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. Duplicate submission returns the original result. Different content under an old ID is rejected with `REQUEST_CONFLICT`. |
| `task_id` | Unique per actual compute run. Reruns need a new ID. Terminal tasks cannot reopen. |
| `revision` | `register` must be `1`; `update` must be current + 1. |
| `expected_revision` | Must equal the task's current revision (optimistic concurrency). |
| `operation` | `register` or `update`. |
| `expires_at` | Limits only not-yet-accepted requests. An accepted task is unaffected. |
| `target` | Immutable after registration (PID/create time or match rule). |

## 4. Register request

Fields (strict; unknown fields are rejected):

- `protocol`: `"atw.task.v1"`.
- `operation`: `"register"`.
- `request_id`, `task_id`, `revision: 1`, `expires_at`.
- `task`: `name` (required), `project`, `run_started_at`, `description`.
- `target`: one of:
  - PID: `{"kind":"pid","pid":12345,"create_time_epoch":1794254400.125,"tree":{"enabled":true}}`.
    `create_time_epoch` is mandatory. A matching PID alone is never treated as
    identity confirmation.
  - Match: `{"kind":"match","name":"python","cmdline_contains":["train.py","--run-id","run001"],"match_mode":"unique","tree":{"enabled":true}}`.
    Zero matches → `PROCESS_NOT_FOUND`; more than one → `AMBIGUOUS_PROCESS`.
    The selected PID + create time is persisted; matching never drifts later.
- `evidence`: `run_directory` (required if present), optional `log`
  (`path`, `scope:"current_run"`, `encoding`), `result_files`.
- `rules`: `success`, `failure` (`finalize: immediate|on_process_exit`),
  `warnings`, `max_runtime_seconds`, `max_runtime_action`
  (`warn|failed|unknown`).
- `notifications`: booleans and `progress_interval_minutes`,
  `warning_repeat_minutes`.

Example: [`examples/task-pid.json`](../examples/task-pid.json).

### 4.1 Rule grammar

A condition node is exactly one of:

```json
{"all": [ ... ]}
{"any": [ ... ]}
{"not": { ... }}
{"kind": "process_tree_exited"}
```

Leaves: `process_exited`, `process_tree_exited`, `log_contains` (`value`),
`log_not_updated` (`seconds`), `file_exists_fresh` (`path`), `file_size_above`
(`path`, `bytes`), `cpu_below` (`percent`), `gpu_util_below` (`percent`,
optional `gpu_uuid`), `gpu_memory_above` (`mb`, optional `gpu_uuid`),
`elapsed_above` (`seconds`).

Any node may carry `for_seconds` (or a warning's `duration_seconds`) for
continuous truth. Maximum depth 8, maximum 64 nodes per rule tree.

Values are three-state: `TRUE`, `FALSE`, `UNKNOWN`. Unavailable metrics are
`UNKNOWN`, never zero. A SUCCESS finalization additionally requires a positive
evidence leaf (`log_contains`, `file_exists_fresh`, or `file_size_above`) and the
process tree having exited; a process-only success rule becomes `UNKNOWN`.

`gpu_util_below` is `UNKNOWN` on standard NVML because only device utilization
(not per-process utilization) is exposed. `gpu_memory_above` uses per-process
NVML memory and is `UNKNOWN` when that metric is unavailable.

## 5. Update request

```json
{
  "protocol": "atw.task.v1",
  "operation": "update",
  "request_id": "req-002",
  "task_id": "cfd-001",
  "revision": 2,
  "expected_revision": 1,
  "expires_at": "2026-11-09T23:30:00Z",
  "changes": {
    "notifications": { "...": "whole object" }
  }
}
```

- `changes` must contain at least one of `task`, `evidence`, `rules`,
  `notifications`; each supplied object **replaces** the old object wholly (no
  deep merge).
- `target` cannot be updated.
- `task.run_started_at` is immutable when already set (it affects evidence
  ownership); attempting a different value is rejected with `IMMUTABLE_FIELD`.
- A stale `revision`/`expected_revision` is rejected with `REVISION_CONFLICT`
  and the old configuration remains.
- Accepted tasks ignore the request's `expires_at`; expired unconsumed updates
  are rejected with `REQUEST_EXPIRED`.
- Rule duration timers reset when rule semantics change.

## 6. Receipt

`receipts/<request_id>.json`:

```json
{
  "protocol": "atw.receipt.v1",
  "request_id": "req-20261109-cfd-001",
  "task_id": "cfd-20261109-001",
  "revision": 1,
  "status": "accepted",
  "monitoring": true,
  "bound_process": {"pid": 12345, "create_time_epoch": 1794254400.125},
  "accepted_at": "2026-11-09T20:01:00Z",
  "message": "已接管该计算任务的监督"
}
```

Rejected:

```json
{
  "protocol": "atw.receipt.v1",
  "request_id": "req-bad",
  "task_id": "bad",
  "revision": 1,
  "status": "rejected",
  "monitoring": false,
  "message": "未找到 PID 12345",
  "error": {"code": "PROCESS_NOT_FOUND", "message": "未找到 PID 12345"}
}
```

**Semantics:** `accepted + monitoring=true` means the target identity was
verified, the task was committed to SQLite and sampling started. It does not
mean the compute task will succeed and does not mean DingTalk delivery
succeeded.

CLI:

```text
atw receipt <request_id> --wait 30 --json
```

- accepted → JSON + exit 0;
- rejected → JSON + nonzero exit;
- timeout → `{"status":"timeout","error":{"code":"RECEIPT_TIMEOUT",...}}` + exit 2.

## 7. Rejection codes

Frozen codes: `INVALID_JSON`, `UNSUPPORTED_PROTOCOL`,
`SCHEMA_VALIDATION_FAILED`, `REQUEST_EXPIRED`, `PROCESS_NOT_FOUND`,
`PROCESS_IDENTITY_MISMATCH`, `AMBIGUOUS_PROCESS`, `ACCESS_DENIED`,
`TASK_ALREADY_EXISTS`, `TASK_FINALIZED`, `REVISION_CONFLICT`,
`REQUEST_CONFLICT`, `UNSAFE_PATH`.

Documented implementation codes: `FILE_TOO_LARGE`, `INVALID_REQUEST_ID`,
`TASK_NOT_FOUND`, `IMMUTABLE_FIELD`, `INTERNAL_ERROR`, `SERVICE_UNAVAILABLE`,
`RECEIPT_TIMEOUT`.

## 8. Path policy

- Evidence paths must be absolute and, after resolving symlinks, remain under
  one of `allowed_read_roots`. Escapes are rejected with `UNSAFE_PATH`.
- `run_directory` must exist and be a directory.
- Log/result files may be created later, but an existing one must be readable.
- Old log content (mtime before the declared run start) is ignored until it is
  updated. A result file must be fresh for `file_exists_fresh` /
  `file_size_above` to count for this run.
- On hot attach the tail (up to `max_log_scan_bytes`, default 8 MiB) is scanned;
  matched literals are sticky for the run across rotation/truncation.

## 9. Idempotency and crash repair

- The request row is written in the same SQLite transaction as the task,
  process identity, revision and accepted event/outbox row.
- Receipts are written with temp-file + fsync + rename.
- If the service dies after commit but before moving the request, the next
  startup scan replays the stored receipt idempotently and moves the file.
- Replaying the same request/content produces no second task and no second
  accepted notification.
