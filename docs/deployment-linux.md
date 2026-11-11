# Linux Deployment

Target: Ubuntu 22.04/Debian, Python >= 3.11, systemd. ATW runs as a **user**
service, never root.

## 1. Source installation (with internet)

```bash
git clone <repo> agent-task-watcher
cd agent-task-watcher
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
atw doctor
```

The `install.sh` helper creates a venv at `$ATW_HOME/venv` by default:

```bash
sudo apt-get install -y python3-venv    # once, if needed
ATW_HOME="$HOME/.local/share/atw" deploy/linux/install.sh
```

Use `--no-install` when ATW is already importable by the chosen interpreter
(e.g. an offline standalone Python):

```bash
ATW_HOME="$HOME/.local/share/atw" \
ATW_PYTHON="$HOME/atw-runtime/python/bin/python3" \
deploy/linux/install.sh --no-install --enable-linger
```

## 2. Offline / no-internet hosts

The acceptance host had system Python 3.10 and no outbound internet. The tested
recipe:

1. On a Windows/macOS workstation with internet, download an official
   python-build-standalone **Linux x86_64** runtime (Python 3.12) and a matching
   manylinux wheel set (`pip download --platform manylinux2014_x86_64
   --python-version 3.12 --only-binary=:all:`).
2. Upload/extract the runtime to `~/atw-runtime`.
3. `python -m pip install --no-index --find-links wheels agent-task-watcher`
   (or upload the source tree and run with `PYTHONPATH=src`).
4. Run `install.sh --no-install` with `ATW_PYTHON` pointing at the standalone
   interpreter.

The runtime/wheel hashes and versions used in the V1 acceptance run are in
[`acceptance.md`](acceptance.md).

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

## 5. Upgrade

```bash
systemctl --user stop atw.service
cd agent-task-watcher && git pull        # or install the new wheel
# with a venv: .venv/bin/pip install -e .
systemctl --user start atw.service
```

The SQLite schema is versioned with idempotent migrations. Back up
`<ATW_HOME>/data/atw.db` before major upgrades.

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
