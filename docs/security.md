# Security and Privacy

## 1. Trust model

ATW is a local, single-user supervisor. The JSON inbox is writable by the same
OS user (and by anyone who can write that user's files). Protect `ATW_HOME`
with user-only permissions and treat shell access as equivalent to ATW control.
ATW does not run as root and does not need administrator/root privileges.

## 2. No code execution from JSON

The request is declarative and validated against a strict Pydantic model:

- unknown top-level fields are rejected (`extra="forbid"`);
- unknown rule kinds and irrelevant parameters are rejected;
- only the fixed rule vocabulary is accepted;
- no shell, Python, regex script or command field exists;
- rule depth and node count are bounded;
- request size is bounded (default 256 KiB).

`tests/integration/test_security.py` submits a poisoned request containing a
`command` field and a `shell` rule; it is rejected and no command runs.

## 3. Path containment

- Every evidence path must be absolute and resolve under `allowed_read_roots`.
- `..` traversal and symlinks that point outside the roots are rejected with
  `UNSAFE_PATH`.
- The root process/evidence directories must be readable by the ATW user; the
  service does not elevate.
- Only log/result paths declared by the task are read; there is no generic file
  browser or API.
- Log previews in notifications are short; full logs are never sent.

## 4. Secrets

DingTalk credentials are read only from:

- `ATW_DINGTALK_WEBHOOK` and `ATW_DINGTALK_SECRET` environment variables; or
- the user systemd environment file (`~/.config/atw/atw.env`, mode 0600) or a
  protected local secret manager.

They are never written to task JSON, receipts, SQLite, ATW logs, the mock
notification file or Git. `.gitignore` excludes `key.txt`, `webhook.txt`,
`*.secret`, `.env`, and local config. `atw doctor` reports only whether
credentials are configured and a token length; it never prints the URL,
token or secret.

Notification error strings are sanitized: the webhook URL, token, secret and
signature are replaced with `***` before logging or persisting. In addition,
the HTTP client loggers (`httpx`, `httpcore`, `urllib3`) are forced to
`WARNING` so their INFO request-line (which contains the full signed URL) is
never written, and every ATW log handler carries a redaction filter for
`access_token` / `sign` / `secret` / `webhook` query parameters. Regression
tests: `tests/unit/test_logging_secrets.py`.

If upgrading an earlier pre-release build, inspect and redact existing
`logs/atw.log*` files for `access_token` / `sign` query values and rotate the
DingTalk webhook if any signed URL was written before this fix.

## 5. File permissions

On POSIX, ATW creates `ATW_HOME` and subdirectories with mode 0700 and receipts,
config, databases and logs with 0600 where it creates them. The installer also
sets `~/.config/atw` to 0700. On Windows, files live under the user profile
(`%LOCALAPPDATA%\ATW`) and inherit the user's ACL; do not grant other users
access.

## 6. DingTalk channel risks

- Custom robot webhooks are a bearer credential. Anyone who obtains the URL can
  post messages to the group; rotate it if leaked.
- DingTalk's signed custom robot has HTTP rate limits (ATW defaults to 15/min,
  below the documented 20/min cap).
- Delivery is at-least-once attempt with a persistent outbox and dedupe; HTTP
  webhooks cannot guarantee strict exactly-once delivery. A timeout may have
  delivered the message.
- DingTalk may change or retire custom robots; the notifier is isolated behind
  the `Notifier` interface so another channel can be added later.
- ATW never receives DingTalk messages and never exposes a chat command.

## 7. Service isolation

- Linux: a **user** systemd unit, `NoNewPrivileges=true`, `User=` is the
  invoking user (not root). `loginctl enable-linger` is needed for boot without
  a login; it does not grant root.
- Windows: a Task Scheduler task runs with the current user's least-privilege
  token. The AtStartup/S4U definition requires elevation to register on many
  systems; the non-elevated AtLogOn fallback is not a boot/no-login equivalent.
- A single-instance lock prevents two ATW writers.

## 8. Known limitations

- ATW cannot monitor processes of another OS user.
- Polling cannot guarantee discovery of a child created and fully detached
  within one sampling interval.
- GPU per-process utilization is unavailable in NVML; ATW reports UNKNOWN.
- If ATW itself is completely down, it cannot notify until it restarts; V1 has
  no external heartbeat.
- No browser UI / HTTP API is provided, reducing attack surface but also
  preventing remote queries except through SSH + CLI.
