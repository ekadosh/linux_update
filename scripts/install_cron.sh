#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKER="# linux_update_ansible"
CRON_LINE="0 3 * * 0 cd $ROOT_DIR && $ROOT_DIR/scripts/run_updates.sh >/dev/null 2>&1 $MARKER"

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT

if crontab -l >"$tmp_file" 2>/dev/null; then
  grep -vF "$MARKER" "$tmp_file" >"$tmp_file.next" || true
  mv "$tmp_file.next" "$tmp_file"
else
  : >"$tmp_file"
fi

printf '%s\n' "$CRON_LINE" >>"$tmp_file"
crontab "$tmp_file"

echo "Installed weekly Sunday 03:00 cron entry:"
echo "$CRON_LINE"

