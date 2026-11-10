#!/usr/bin/env bash
# Build the Linux one-file executable on the target Linux host.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
python3 -m PyInstaller packaging/atw.spec --noconfirm \
  --distpath dist/linux --workpath build/pyinstaller
./dist/linux/atw --version
echo "built dist/linux/atw"
