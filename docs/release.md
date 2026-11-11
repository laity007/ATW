# Release Builds (V1.0.0)

- Public repository: <https://github.com/laity007/ATW>
- Default branch: `main`
- Public-repository PR: <https://github.com/laity007/ATW/pull/1> (merged as
  `8aa99e8`)
- Release status: **v1.0.0**. All AT-001..AT-040 items are PASS (the two
  environment-limited items were completed by the maintainer on independent
  target machines and recorded as attestation in
  [`acceptance.md`](acceptance.md)).

ATW is pure Python for the source deployment. Standalone one-file binaries are
built **on each target OS** with PyInstaller; the spec forbids cross-generating
incompatible binaries.

## Artifacts

Built on 2026-11-10:

| File | Platform | Size |
|---|---|---|
| `dist/release/atw-v1.0.0-windows-x86_64.zip` | Windows 10/11 x86_64 | ~18.5 MB |
| `dist/release/atw-v1.0.0-linux-x86_64.tar.gz` | Ubuntu 22.04 x86_64 (built on the target) | ~42.6 MB |

SHA256:

```text
93367547BF1714BA77F21DC7E04DF1039F8F1930543B7DE653B55D427F583AA7  atw.exe
99A4CC2A67CC76BCAA023961A81EA0441EEFE697A5C27BACDD70C8E8F6AA377F  atw (Linux)
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
