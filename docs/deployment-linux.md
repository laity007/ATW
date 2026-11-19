# Linux Deployment

Target: Ubuntu 22.04/Debian, Python >= 3.11, systemd. ATW runs as a **user**
service, never root.

Source and wheel deployments require a dedicated Python virtual environment.
Run `-m pip` through its interpreter and use the absolute path to that
interpreter in `ExecStart`. The one-file executable can run directly.

## 1. Source installation (with internet)

```bash
git clone <repo> agent-task-watcher
cd agent-task-watcher
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m atw doctor
```

The `install.sh` helper creates a venv at `$ATW_HOME/venv` by default. Install
the venv component for your chosen Python if needed. On hosts where
`python3` is older than 3.11, pass an absolute path to a supported interpreter
with `--python`.

```bash
ATW_HOME="$HOME/.local/share/atw" deploy/linux/install.sh
```

Use `--no-install` with a virtual environment that already has ATW installed:

```bash
ATW_HOME="$HOME/.local/share/atw" \
ATW_PYTHON="$HOME/.local/share/atw/venv/bin/python" \
deploy/linux/install.sh --no-install
```

## 2. Offline / no-internet hosts

On a connected workstation, prepare the ATW wheel and its dependency wheels
for the target OS, architecture and Python version. For Linux x86_64 with
Python 3.12, choose compatible manylinux wheels. If the host's system Python
is too old, upload a standalone Python runtime with `venv` and `ensurepip`
support. Use that runtime to create the ATW virtual environment.

```bash
ATW_HOME="$HOME/.local/share/atw"
BASE_PYTHON="$HOME/atw-runtime/python/bin/python3"
WHEELHOUSE="$HOME/atw-runtime/wheels"
ATW_WHEEL="$WHEELHOUSE/agent_task_watcher-1.1.0-py3-none-any.whl"

"$BASE_PYTHON" -m venv "$ATW_HOME/venv"
ATW_PYTHON="$ATW_HOME/venv/bin/python"
"$ATW_PYTHON" -m pip install --no-index --find-links "$WHEELHOUSE" "$ATW_WHEEL"
"$ATW_PYTHON" -m pip check
"$ATW_PYTHON" -m atw --version
ATW_HOME="$ATW_HOME" ATW_PYTHON="$ATW_PYTHON" deploy/linux/install.sh --no-install
```

Adjust the three source paths to your uploaded runtime and wheel files.
Subsequent package installs and upgrades use the venv interpreter.

### Migrate an existing service to a venv

Create and verify the venv before restarting ATW. Follow the offline commands
through `atw --version` and skip the installer command. Use the wheel for the
currently running ATW version so the interpreter switch and a package upgrade
can be verified separately. Keep the existing `ATW_HOME` and configuration.

Review the current `ExecStart` with
`systemctl --user show atw.service -p ExecStart`. Create a dedicated drop-in
under `~/.config/systemd/user/atw.service.d/`, for example
`90-atw-venv.conf`. Reset `ExecStart` and repeat the original command with
only the interpreter path replaced. Keep any existing `--home` and
`--config` arguments. For the default home the drop-in looks like:

```ini
[Service]
ExecStart=
ExecStart=%h/.local/share/atw/venv/bin/python -m atw --home %h/.local/share/atw service run
```

Reload and restart the user unit, then check its status and command:

```bash
systemctl --user daemon-reload
systemctl --user restart atw.service
systemctl --user is-active atw.service
systemctl --user show atw.service -p ExecStart
"$ATW_PYTHON" -m atw --home "$ATW_HOME" status --json
```

Verify the running service, its task state, and fresh observations for any
previously monitored tasks. To roll back the interpreter switch, remove the
migration drop-in you added, reload systemd and restart the service.

## 3. Configuration

`install.sh` generates `<ATW_HOME>/config/config.yaml` with:

```yaml
instance_id: <hostname>
scan_interval_seconds: 5
sample_interval_seconds: 10
allowed_read_roots:
  - /home/<user>
  - /tmp
notification:
  provider: auto
```

Add every project directory the AI will hand off under `allowed_read_roots`.
Keep the file user-only (`0600`).

Secrets are **not** in this file. Put them in
`~/.config/atw/atw.env` (created mode 0600 by the installer):

```bash
ATW_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=...
ATW_DINGTALK_SECRET=SEC...
```

The generated unit uses `EnvironmentFile=-%h/.config/atw/atw.env`.
Alternatively export the variables in the service manager environment.

### 3.1 Outbound HTTP proxy (optional)

If ordinary HTTPS requests time out but an explicit local proxy works, give
**only ATW** a proxy rather than changing machine-wide routes or TUN settings.
The DingTalk HTTP client uses HTTPX's environment proxy support.

Create `~/.config/systemd/user/atw.service.d/20-notification-proxy.conf`:

```ini
[Service]
Environment="HTTP_PROXY=http://127.0.0.1:7890"
Environment="HTTPS_PROXY=http://127.0.0.1:7890"
Environment="NO_PROXY=localhost,127.0.0.1,::1"
```

`7890` is an example: use the actual HTTP/mixed proxy listener on the host.
Even for HTTPS destinations, an HTTP CONNECT proxy uses an `http://` proxy URL.
Do not put a subscription URL or proxy credentials in shared documentation.

```bash
systemctl --user daemon-reload
systemctl --user restart atw.service
systemctl --user is-active atw.service
```

Restarting ATW does not stop or restart compute tasks. The local proxy must
remain available; this drop-in does not start it or guarantee its boot order.
To roll back, remove only this drop-in, then reload and restart ATW again.

An interactive `atw notify test --json` does **not** inherit the service's
environment. Supply the same proxy and DingTalk credentials to the test
process without printing them. A successful test requires `ok=true` and
`channel=dingtalk`, not merely HTTP reachability or `doctor` passing.
Existing queued notifications retain their backoff schedule after restart;
do not edit SQLite to force delivery. Check `atw notifications --json` for
actual `channel=dingtalk, status=delivered` records. `mock` delivery counts
are not evidence of DingTalk delivery.

`atw notifications` reports the latest service session's HTTP client route
as direct, direct via `NO_PROXY`, or proxy (scheme and loopback/remote only).
The JSON output contains `service_route` and each item's `last_failure` and
`next_attempt_at`. The latter is when a pending item becomes eligible for
retry. The outbox worker cadence and rate limit still apply. A successful retry
keeps the most recent failure's code, timestamp, summary and route. The error
summary uses the HTTP status or DingTalk numeric business code when available.
Webhook URLs, signatures, proxy credentials and raw server responses are
excluded. `atw notify test` reports the interactive process's route.

## 4. systemd user service

The installer generates `~/.config/systemd/user/atw.service`:

```ini
[Unit]
Description=Agent Task Watcher (ATW) local task supervisor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=<project-root>
Environment=ATW_HOME=/home/<user>/.local/share/atw
EnvironmentFile=-%h/.config/atw/atw.env
ExecStart=<python> -m atw --home /home/<user>/.local/share/atw service run
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

Enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now atw.service
systemctl --user status atw.service
```

### Boot without an interactive login

A user service normally starts with the user manager at login. For boot
operation without a login, enable lingering:

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger     # Linger=yes
```

Verify:

```bash
systemctl --user is-enabled atw
ATW_HOME=... <python> -m atw --home ... doctor --json
journalctl --user -u atw -n 50 --no-pager
```

`Restart=always` plus `RestartSec=10` restarts after a crash; ATW then detects
the previous unclean session, revalidates process identities, resumes
MONITORING tasks and sends one `SERVICE_RECOVERED` notification. It does not
resend the accepted notification.

## 5. Verified wheel upgrade and rollback

Run the upgrade as the user who owns the active ATW systemd unit, after that
unit uses the dedicated venv. Provide the target wheel and the exact known-good
wheel currently installed in the venv. Keep wheel files from each release.
The helper works offline with the venv's existing dependencies. The system
Python can run the helper (3.10 or later). The helper installs ATW into the
interpreter in `ExecStart`, which must be the ATW venv with Python 3.11 or later.
It supports one
`<python> -m atw --home <path> [--config <path>] service run` unit. Its default
is `atw.service`. The helper also honors `ATW_CONFIG` from the running service
when the unit omits `--config`.

```bash
python3 deploy/linux/upgrade.py upgrade \
  --wheel /path/to/target/agent_task_watcher-NEW-py3-none-any.whl \
  --previous-wheel /path/to/previous/agent_task_watcher-OLD-py3-none-any.whl
```

The helper checks the unit's interpreter and home against its running process,
then stages both wheels, checks dependencies and rehearses the schema migration
on a SQLite copy. It stops only the ATW unit, takes a verified SQLite backup
including WAL data, and saves the wheels, their hashes and the pre-upgrade
state in the private directory printed as `Upgrade record`.
After starting the unit it verifies the installed package, a new service
session with the target version and systemd PID, fresh samples for the
previously monitored tasks, and the continued presence of their pending
outbox entries. `--timeout 90` controls the startup verification window. Allow
at least one configured sampling interval plus startup time.

If installation or startup verification fails, the helper attempts to
reinstall and verify the previous wheel against the live database. That path
accepts the same schema or the verified additive migration from schema 1 to
schema 2. You can roll back a completed upgrade the same way:

```bash
python3 deploy/linux/upgrade.py rollback --record /path/from/upgrade-output
```

For an unrecognized schema combination the unit stays stopped. Review
post-upgrade writes and external effects such as deliveries and inbox receipts
before using `rollback --record ... --restore-db`. That option saves a separate
`pre-restore-*.db` snapshot of the live database, then returns the active
database to the pre-upgrade point. Reconcile any later receipts, task updates
and notifications before treating the restored instance as current.

Editable source installations continue to use the source workflow. Preserve
the current source revision and a consistent SQLite backup, stop the unit,
update and reinstall with the interpreter from its `ExecStart`, then start and
verify the unit. Migrate to an installed wheel before using the automated
wheel upgrade helper.

## 6. Uninstall

```bash
deploy/linux/uninstall.sh            # keeps data
deploy/linux/uninstall.sh --purge    # removes ATW_HOME too
```

## 7. Troubleshooting

| Symptom | Check |
|---|---|
| `active (running)` then exits | `journalctl --user -u atw -n 100`, `<ATW_HOME>/logs/atw.log` |
| Request stays in inbox | file is `*.json`, complete, size <= 256 KiB, file name stem == request_id |
| `ACCESS_DENIED` | target process/path belongs to another user or is not readable |
| `UNSAFE_PATH` | path outside `allowed_read_roots` or symlink escape |
| No DingTalk message | `atw doctor`, env file permissions, `atw notify test`, bot keyword `ATW` |
| Service absent after reboot | linger disabled (`loginctl show-user ...`) or unit not enabled |
