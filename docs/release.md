# Release Builds (V1.0.0)

- Public repository: <https://github.com/laity007/ATW>
- Default branch: `main`
- Public-repository PR: <https://github.com/laity007/ATW/pull/1> (merged as
  `8aa99e8`)
- Release status: implementation merged; `v1.0.0` tag/Release intentionally not
  created yet, pending the two environment-limited acceptance items
  (Windows AtStartup/no-login and physical reboot) recorded in
  [`acceptance.md`](acceptance.md).

ATW is pure Python for the source deployment. Standalone one-file binaries are
built **on each target OS** with PyInstaller; the spec forbids cross-generating
incompatible binaries.

## Artifacts

Built on 2026-11-10:

| File | Platform | Size |
|---|---|---|
| `dist/release/atw-v1.0.0-windows-x86_64.zip` | Windows 10/11 x86_64 | ~19.5 MB |
| `dist/release/atw-v1.0.0-linux-x86_64.tar.gz` | Ubuntu 22.04 x86_64 (built on the target) | ~42.6 MB |

SHA256:

```text
C8836D47F2AC6A54E2C05CD1BD010022F0038812D42A6B2E4566C71C9535723A  atw.exe
EE1D365900AC0DEABFC3035259EF90FFE6C14D7297DCF9AAE68032420AF71560  atw (Linux)
```

The archives contain the binary, `README.md`, `LICENSE`, `config.example.yaml`,
`README-RELEASE.txt`, deployment scripts and the `atw-task-handoff` Skill.

## Build from source

Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\packaging\build.ps1
```

Linux:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
./packaging/build.sh
```

CI: [`.github/workflows/release.yml`](../.github/workflows/release.yml) runs the
test suite on Windows/Linux, builds one-file executables on each OS, and creates
a GitHub Release on a `v*` tag. The workflow was not executed in this
environment (no GitHub remote/tag was used); it is provided as release
automation.

## Version / schema

- ATW version: `1.0.0`
- AFHP/1 task schema: `atw.task.v1`
- Receipt schema: `atw.receipt.v1`
- SQLite schema migration: version 1
