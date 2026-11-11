# Release Builds (V1.0.0)

- Public repository: <https://github.com/laity007/ATW>
- Default branch: `main`
- Public-repository PR: <https://github.com/laity007/ATW/pull/1> (merged as
  `8aa99e8`)
- Release: **v1.0.0**, <https://github.com/laity007/ATW/releases/tag/v1.0.0>
- Release status: published. All AT-001..AT-040 items are PASS (the two
  environment-limited items were completed by the maintainer on independent
  target machines and recorded as attestation in
  [`acceptance.md`](acceptance.md)).
- GitHub Actions run
  <https://github.com/laity007/ATW/actions/runs/42968507644>: Windows + Ubuntu
  tests, both executable builds and the release job all passed.

ATW is pure Python for the source deployment. Standalone one-file binaries are
built **on each target OS** with PyInstaller; the spec forbids cross-generating
incompatible binaries.

## Published GitHub Release assets

`v1.0.0`:

| Asset | Platform | Size | SHA256 |
|---|---|---|---|
| `atw.exe` | Windows x86_64 | 20,261,296 B | `692647770B8ED6D133F14807CCAD78AC8A442F5EEB79779E3B190B5E2E0231DF` |
| `atw` | Linux x86_64 (Ubuntu 22.04, glibc 2.35) | 33,712,352 B | `016F2915A2C34AA4F4D46CBEEE5CD123C0EB2AADB02F61A18894DEA41BC78016` |

Both assets were downloaded and smoke-tested: `--version`, `doctor --json`
(no FAIL checks) and `service run --max-seconds 2` / clean shutdown. The Linux
asset was rebuilt on **Ubuntu 22.04** after the initial `ubuntu-latest` (24.04)
build required `GLIBC_2.38`; the one-off repair workflow was removed and
[`release.yml`](../.github/workflows/release.yml) now pins `ubuntu-22.04` for
future releases.

Note: the `v1.0.0` tag itself was cut before the runner pin (its in-tag
`release.yml` still says `ubuntu-latest`). The **published Linux asset** was
rebuilt from the post-tag `main` with `ubuntu-22.04` and is verified. To
reproduce the 22.04-compatible binary from source, use `main` or cherry-pick
commit `bac0d80`; no runtime source change is involved.

## Local reproducible archives

`dist/release/atw-v1.0.0-windows-x86_64.zip` and
`dist/release/atw-v1.0.0-linux-x86_64.tar.gz` are auxiliary local packages
(binary + README/LICENSE/config/deploy/skill). Their binary hashes differ from
the CI assets because they were built on the acceptance hosts:

```text
93367547BF1714BA77F21DC7E04DF1039F8F1930543B7DE653B55D427F583AA7  local Windows atw.exe
99A4CC2A67CC76BCAA023961A81EA0441EEFE697A5C27BACDD70C8E8F6AA377F  local Linux atw
```

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
