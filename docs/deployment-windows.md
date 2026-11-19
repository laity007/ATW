# Windows Deployment

Target: Windows 10/11, Python >= 3.11, Task Scheduler.

ATW 1.1 handoff commands, progress configuration, and compatibility checks
are in [Agent handoff and progress](agent-progress.md).

ATW runs as a **user** task in the current user's security context. It does not
need a Windows service, a port, or administrator rights for normal operation.

## 1. Source installation

Source and wheel deployments require a dedicated Python virtual environment.
Run `-m pip` through its interpreter and point Task Scheduler to that
interpreter. The one-file executable can run directly.

```powershell
git clone <repo> agent-task-watcher
cd agent-task-watcher
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m atw doctor
```

For a wheel deployment, install the wheel using the same venv interpreter:

```powershell
.\.venv\Scripts\python.exe -m pip install 'C:\path\to\agent_task_watcher-1.1.0-py3-none-any.whl'
.\.venv\Scripts\python.exe -m pip check
```

Use the absolute venv interpreter path in the scheduled task. Run the
`atw` commands below from an activated venv or use its `python.exe -m atw`.

## 2. Configuration

`install-task.ps1` creates `%LOCALAPPDATA%\ATW\config\config.yaml` (or the
`-AtwHome` path) with Windows allowed roots:

```yaml
instance_id: <COMPUTERNAME>
allowed_read_roots:
  - C:/Users/<user>
  - C:/Users/<user>/AppData/Local/Temp
  - <project-root>
notification:
  provider: auto
```

Add every project directory the AI will hand off. Keep the file under the user
profile.

Set the DingTalk secrets as **User** environment variables (not in Git, not in
the task JSON):

```powershell
[Environment]::SetEnvironmentVariable(
  "ATW_DINGTALK_WEBHOOK",
  "https://oapi.dingtalk.com/robot/send?access_token=...",
  "User")
[Environment]::SetEnvironmentVariable(
  "ATW_DINGTALK_SECRET", "SEC...", "User")
```

Open a new shell/app after setting them, or set them in the current process
before starting the task. `atw notify test` should print `channel=dingtalk` and
`ok=true`.

## 3. Task Scheduler installation

```powershell
# Boot/no-login definition: requires an elevated PowerShell (S4U + BootTrigger)
.\deploy\windows\install-task.ps1 `
  -Python "$PWD\.venv\Scripts\python.exe" `
  -AtwHome "$env:LOCALAPPDATA\ATW" `
  -TaskName ATW `
  -TriggerType AtStartup

# Non-elevated functional fallback (runs at logon; NOT boot/no-login)
.\deploy\windows\install-task.ps1 `
  -Python "$PWD\.venv\Scripts\python.exe" `
  -AtwHome "$PWD\tests\_tmp\atw-home" `
  -TaskName ATW-Test `
  -TriggerType AtLogOn
```

The installer uses the Task Scheduler **COM API** because the CIM
`Register-ScheduledTask` cmdlet can be denied even when COM registration for the
current user is allowed. It configures:

- trigger: boot (`AtStartup`) or logon (`AtLogOn`);
- principal: current user, least privilege; S4U for AtStartup, interactive token
  for AtLogOn;
- action: `<python.exe> -m atw --home "<ATW_HOME>" service run`, working
  directory = project root;
- restart on failure every minute, unlimited execution time, `IgnoreNew`
  multiple-instance policy, start when available, no battery stop.

To migrate an existing task, install its current ATW version in the venv and
verify `python.exe -m atw --version` through that interpreter before switching.
Update the task action to use the venv's `python.exe` and preserve its
`--home`, configuration, account and trigger. Restart the scheduled task
and verify its status and any previously monitored tasks. The old task action
can be restored if the interpreter switch fails.

Verify:

```powershell
schtasks /Query /TN ATW /V /FO LIST
atw --home "$env:LOCALAPPDATA\ATW" status --json
atw --home "$env:LOCALAPPDATA\ATW" doctor --json
Get-Content "$env:LOCALAPPDATA\ATW\logs\atw.log" -Tail 50
```

The V1 acceptance host is a standard non-elevated user session. Registering
the AtStartup/S4U definition returned `Access is denied` in that environment;
the AtLogOn COM definition registered, started and was verified. **A physical
reboot with no logged-in user was not performed**, so AT-035 is recorded as
NOT_VERIFIED in [`acceptance.md`](acceptance.md). Run the AtStartup command from
an elevated PowerShell and perform the reboot test on the target machine.

## 4. No-login behavior

AtStartup + S4U is the intended no-login mode. It requires the task definition
to be created with sufficient rights (elevated shell or policy grant). AtLogOn
is only a non-elevated fallback: it runs when the user logs on, not at boot,
and cannot be used to claim AT-035.

## 5. Uninstall

```powershell
.\deploy\windows\uninstall-task.ps1 -TaskName ATW
.\deploy\windows\uninstall-task.ps1 -TaskName ATW -Purge   # remove data too
```

Stopping a Task Scheduler task can terminate the ATW process without its
graceful SIGTERM path, leaving the session marked unclean. The next start then
performs the normal recovery checks; this is safe but may send a
`SERVICE_RECOVERED` notification.

## 6. Troubleshooting

| Symptom | Check |
|---|---|
| `Access is denied` registering AtStartup | Run elevated; S4U/boot tasks are privileged in this environment |
| Task registered but no lock | `schtasks /Query /TN ATW /V`, `<ATW_HOME>\logs\atw.log` |
| No DingTalk message | User env vars set? `atw notify test`; new process? |
| Python not found | Pass absolute `-Python`; Task Scheduler has a minimal PATH |
| Service already running | Single-instance lock; stop the existing ATW before starting the task |
| `UNSAFE_PATH` | Add the project directory to `allowed_read_roots` |
| NVML GPU shows UNKNOWN | Windows WDDM often hides per-process memory; CPU monitoring is unaffected |
