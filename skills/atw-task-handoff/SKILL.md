---
name: atw-task-handoff
description: Hand a long-running CPU/GPU compute task to Agent Task Watcher (ATW) over SSH. Use when an AI agent has started (or is about to start) a task on a remote Linux/Windows host and must exit the session while ATW supervises and sends DingTalk notifications.
---

# ATW Task Handoff (1.1)

Use this Skill when the user wants a long-running compute task (simulation,
training, test) to be supervised by ATW after the AI session ends. ATW only
**watches and notifies**; it never starts, stops, retries or kills the compute
process. The AI/external program starts the task independently of the SSH
session.

## 0. Resolve actual run paths

For an existing run, reuse the real log and result paths from its launch command,
configuration or artifacts. Use paths the user supplied when they match the run.
Ask a single focused question only when a required fact cannot be established.
For a new run you launch, derive the paths from its actual configuration.

Interpretation:

| User answer | Action |
|---|---|
| Both paths | Use them after checking absolute path, permissions, ownership and that the program can actually write there. |
| Only one path | Use it; derive the other from the real run layout. |
| Empty / "默认" / "default" | Create an independent run directory and use defaults: `runs/<task-id>/run.log` and `runs/<task-id>/results/`. |
| "Already running, don't change" | Read the **real existing** log/result paths from the program's config/command line. Do **not** claim to move an already-running program's outputs; do **not** create empty files to fake results. |

Never repeat a path question for information the user already supplied. If the
user gives a directory rather than a file, explain the expected file path and
propose the minimal correction.

## 1. Preconditions and facts to establish

1. Identify B host, SSH access, OS, project directory, launch command, result
   definition and expected runtime.
2. The compute process must be independent of the SSH session (nohup/systemd/
   scheduler/`setsid`/Windows detached process, as appropriate).
3. Supply the root PID on B. `atw handoff` reads and verifies the process
   creation time. If you already have a trusted creation time recorded on B
   for this run, pass it as `--create-time`.
4. If a PID cannot be identified reliably, use `target.kind=match` with the
   process name and **all** distinguishing command-line fragments. `match` is
   only accepted when exactly one process matches; never guess.
5. Determine the actual completion/failure markers and result files. Do not
   weaken rules just to obtain a SUCCESS notification.

## 2. Prefer single-command handoff on the compute node

Run `atw handoff` directly on B with the service's home/config and account.
Choose a stable request ID before invoking SSH and save the prepared request
in the run directory. Use the real project's success marker.

```bash
atw handoff --task-id yolo-s07 --request-id req-yolo-s07-01 \
  --name 'UAV-YOLO S07' --pid 12345 --run-dir /runs/s07 \
  --log /runs/s07/train.log --result /runs/s07/best.pt \
  --success-log '<actual completion marker>' \
  --progress-csv /runs/s07/results.csv --progress-column epoch \
  --counter-base 1 --total 200 --unit epoch \
  --request-out /runs/s07/atw-request.json --wait 30 --json
```

- Select progress explicitly on every handoff. Use the run's real CSV column,
  total and counter base, or use `--progress-plan /absolute/progress-plan.json`
  for a queue. Use `--progress-unknown '<reason>'` only when reliable counting
  is unavailable. Keep the name stable and put counters in the progress source.
- For a queue, declare all planned item IDs, total, unit, weight and required
  status in the manifest. Bind a queue root whose lifetime represents this run.
  Each item has a dedicated report path and one writer. Use
  `atw progress report --plan PLAN --item S07 --attempt 1 --state queued`
  for positively known queued items. Report real state and completed counters as
  they change. An item may read its counter from an existing CSV. Give retries
  the same item ID, a higher attempt and separate CSVs when applicable. Report
  acceptance only after the project's validation. ATW computes weighted
  execution and independent acceptance totals.
- Supply `--run-started-at` in timezone-aware ISO 8601 when process creation
  does not represent the start of this run. A dedicated process can use its
  creation time. Use `--dry-run` to validate the source and preview a sample.
- For custom success/failure rule trees, prepare the declarative request and
  use `atw submit FILE --wait 30 --json`. On timeout, check the same request ID
  and replay the saved request with `atw submit` if needed.
- For an explicit install or upgrade request, a missing ATW command, an
  unavailable service, or `UNSUPPORTED_PROTOCOL`, see
  [deployment and recovery](references/deployment.md).

## 3. Legacy AFHP/1 builder and delivery

The helper in this Skill directory generates a valid request (stdlib only;
optional local validation when `atw` is importable):

For a legacy PID request, obtain the exact creation time from psutil on B
and pass it as `--create-time`:

```bash
python - <<'PY'
import psutil
p = psutil.Process(12345)  # Replace with the root PID.
print(p.pid, repr(p.create_time()))
PY
```

```bash
python skills/atw-task-handoff/scripts/build_task_json.py \
  --task-id "cfd-20261110-001" \
  --name "油气管道甲烷扩散 CFD 仿真" \
  --project "pipeline-simulation" \
  --project-dir /home/simrunner/project \
  --pid 12345 --create-time 1794254400.125 \
  --success-log "Simulation completed" \
  --failure-log "CUDA out of memory" \
  --max-runtime-seconds 14400 \
  --ensure-dirs \
  --output /home/simrunner/project/runs/cfd-20261110-001/task.json
```

Path rules:
- `--log` / `--result` are the user's explicit paths (absolute).
- If omitted, defaults are `<project-dir>/runs/<task-id>/run.log` and
  `<project-dir>/runs/<task-id>/results/result.csv`.
- `--ensure-dirs` creates only directories, never an empty result file.
- Supply `--run-started-at` when the real launch time differs from the bound
  process creation time. With PID, the builder uses its given creation time
  by default. With process match and no known start time, it leaves it unset.
- `expires_at` defaults to now + 60 minutes; an unaccepted request expires, an
  accepted task is not affected by request expiry.

Review the JSON fields (PID, create time, paths, success/failure markers) before
delivery.

### Deliver atomically to B

ATW scans `$ATW_HOME/tasks/inbox/` for complete `*.json` files. The file name
stem **must equal** `request_id`.

Linux B:

```bash
# 1) write to .tmp on the same filesystem
scp task.json B:~/.local/share/atw/tasks/inbox/<request_id>.json.tmp
# 2) atomically rename after the write is complete
ssh B 'mv ~/.local/share/atw/tasks/inbox/<request_id>.json.tmp ~/.local/share/atw/tasks/inbox/<request_id>.json'
```

Windows B (PowerShell), use `scp`/`Copy-Item` to a `.tmp` then
`Move-Item -Force` within the same directory. Do not copy directly to a
`*.json` name; a partially copied file must never be consumed.

Never put a DingTalk webhook or secret in the task JSON. The task JSON is
declarative and read-only; ATW will reject shell/code injection attempts.

## 4. Use the receipt

Use the receipt returned by a successful `handoff --wait 30 --json` directly.
Query the receipt separately for legacy file delivery or when submission times
out. For a timeout, use the original request ID and retain the prepared request.

```bash
ssh B 'atw receipt <request_id> --wait 30 --json'
```

Success is **all** of:
- exit code `0`;
- `status == "accepted"`;
- `monitoring == true`;
- `bound_process.pid` and `bound_process.create_time_epoch` match the process
  facts you established;
- the target process is still alive and independent of the SSH session.

For `target.kind=match`, the accepted receipt contains the actual selected
PID + create time; record and check them.

After a rejection or a timeout that remains unresolved after querying the
original request ID:
- report the exact error code/message to the user;
- keep the compute process running;
- do **not** claim monitoring was taken over;
- retry only after fixing the cause (bad path, missing process, revision
  conflict, expired request, etc.).

## 5. Final report and exit

Report:
- `task_id` and `request_id`;
- monitoring state and bound PID/create time;
- log path and result path actually used;
- success/failure/warning notification policy;
- that ATW will notify DingTalk on accepted/terminal/warning events;
- that the AI is ending the goal and will not poll continuously.

Then **end the AI goal**. Do not establish a long polling loop or consume LLM
tokens while ATW supervises. ATW can be queried later with:

```bash
ssh B 'atw show <task_id> --json'
ssh B 'atw history --limit 20 --json'
```

## 6. Failure and safety rules

- ATW never controls the process. Do not ask it to kill/restart/retry.
- A disappearing process with no completion evidence becomes `UNKNOWN`, never
  `SUCCESS`; do not promise success.
- Exit codes of externally started processes are usually unavailable; ATW
  reports them as unknown, never as 0.
- GPU per-process utilization is not exposed by NVML in V1; `gpu_util_below`
  evaluates UNKNOWN. Device utilization is display-only.
- If the user insists on changing paths of an already-running program, explain
  that only the real existing paths can be monitored; do not fabricate files.
- If the B host is unreachable, report it and stop; do not invent a receipt.
