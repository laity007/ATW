#!/usr/bin/env bash
# Stop/remove the ATW per-user systemd unit. Data is kept unless --purge.
set -euo pipefail

ATW_HOME="${ATW_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/atw}"
PURGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) ATW_HOME="$2"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    -h|--help) echo "Usage: $0 [--home PATH] [--purge]"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

systemctl --user disable --now atw.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/atw.service"
systemctl --user daemon-reload
echo "ATW user service removed. Data remains at $ATW_HOME"
if [[ "$PURGE" -eq 1 ]]; then
  rm -rf "$ATW_HOME"
  echo "purged $ATW_HOME"
fi
