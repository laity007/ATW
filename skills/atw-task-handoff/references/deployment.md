# Install, upgrade, or recover ATW

Official source: [laity007/ATW](https://github.com/laity007/ATW).
For `atw.task.v1.1`, use the [1.1.0 release](https://github.com/laity007/ATW/releases/tag/v1.1.0).
Compare downloaded assets against the release's `SHA256SUMS`. For a source
installation, check out the corresponding release tag. The guides cover
[Linux](https://github.com/laity007/ATW/blob/v1.1.0/docs/deployment-linux.md) and
[Windows](https://github.com/laity007/ATW/blob/v1.1.0/docs/deployment-windows.md).

- For a missing command, check the service account's existing interpreter,
  installation, and ATW home before installing anything. Reuse a working
  installation by invoking its interpreter directly.
- For an unavailable service, inspect its status and logs. Recover the existing
  unit or scheduled task with its original config and ATW home.
- For `UNSUPPORTED_PROTOCOL`, check the running service with `atw status --json`.
  Progress requires `atw.task.v1.1` in `active_protocols`. Use the platform
  upgrade guide when authorized, or use the legacy handoff when only basic
  supervision is needed.
- For an authorized source or wheel installation, use a dedicated Python 3.11+
  venv. Use its interpreter for pip and configure the service with its absolute
  path. On offline nodes, follow the Linux guide for a base Python and matching
  wheelhouse.
- After installation, upgrade, or an interpreter or service configuration
  change, run `atw doctor --json` and `atw status --json` once. Verify the
  service, its protocol, and the existing monitored tasks if applicable.
  Continue with the requested task and use its real handoff receipt to close
  the loop. Use a temporary mock task only when a separate smoke test is needed.
