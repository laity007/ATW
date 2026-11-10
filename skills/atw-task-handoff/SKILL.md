---
name: atw-task-handoff
description: Hand a long-running CPU/GPU compute task to Agent Task Watcher (ATW) over SSH. Use when an AI agent has started (or is about to start) a task on a remote Linux/Windows host and must exit the session while ATW supervises and sends DingTalk notifications.
---

# ATW Task Handoff (V1)

Use this Skill when the user wants a long-running compute task (simulation,
training, test) to be supervised by ATW after the AI session ends. ATW only
**watches and notifies**; it never starts, stops, retries or kills the compute
process. The AI/external program starts the task independently of the SSH
session.

## 0. Mandatory one-time path question

On **every invocation**, before generating JSON, ask the user exactly once:

> 本次任务的日志路径和结果输出路径分别是什么？两项都可留空或回复“默认”，未指定项由 AI 按项目结构自行确定，并写入监控 JSON。

Do not ask a second path question in the same invocation.

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
3. Obtain the **root PID and exact create time** on B:
   ```bash
   python - <<'PY'
   import psutil
   p = psutil.Process(<PID>)
   print(p.pid, repr(p.create_time()), p.name())
   print(" ".join(p.cmdline()))
   PY
   ```
   `create_time` must come from psutil on B, not from the AI's local clock.
4. If a PID cannot be identified reliably, use `target.kind=match` with the
   process name and **all** distinguishing command-line fragments. `match` is
   only accepted when exactly one process matches; never guess.
5. Determine the actual completion/failure markers and result files. Do not
   weaken rules just to obtain a SUCCESS notification.

## 2. Build the AFHP/1 JSON

The helper in this Skill directory generates a valid request (stdlib only;
optional local validation when `atw` is importable):

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
- `run_started_at` is the real launch time (defaults to now, UTC).
- `expires_at` defaults to now + 60 minutes; an unaccepted request expires, an
  accepted task is not affected by request expiry.

Review the JSON fields (PID, create time, paths, success/failure markers) before
delivery.

## 3. Deliver atomically to B

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

## 4. Verify the receipt — mandatory

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

If `status == "rejected"` or the command times out (`RECEIPT_TIMEOUT`, nonzero
exit code):
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
