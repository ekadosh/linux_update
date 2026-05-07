#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_CRON="$ROOT_DIR/scripts/install_cron.sh"
ROLLBACK="$ROOT_DIR/scripts/rollback_snapshot.py"
CRON_COMMAND="cd '$ROOT_DIR' && '$ROOT_DIR/scripts/run_updates.sh' >/dev/null 2>&1 # linux_update_ansible"

assert_cron_line() {
  local expected="$1"
  shift
  local output=""
  local actual=""

  output="$("$INSTALL_CRON" --dry-run "$@")"
  actual="$(printf '%s\n' "$output" | tail -n 1)"

  if [[ "$actual" != "$expected $CRON_COMMAND" ]]; then
    echo "unexpected cron line" >&2
    echo "expected: $expected $CRON_COMMAND" >&2
    echo "actual:   $actual" >&2
    return 1
  fi
}

assert_fails() {
  if "$@"; then
    echo "expected command to fail: $*" >&2
    return 1
  fi
}

assert_cron_line "15 4 * * *" --preset daily --time 04:15
assert_cron_line "0 3 * * 0" --preset weekly
assert_cron_line "30 2 * * 1" --preset weekly --time 02:30 --weekday mon
assert_cron_line "45 1 12 * *" --preset monthly --time 01:45 --month-day 12
assert_cron_line "5 6 * * 1-5" --preset weekdays --time 06:05
assert_cron_line "*/20 * * * *" --preset custom --cron-expression "*/20 * * * *"

assert_fails "$INSTALL_CRON" --dry-run --preset weekly --weekday nonsense
assert_fails "$INSTALL_CRON" --dry-run --preset monthly --month-day 32
assert_fails "$INSTALL_CRON" --dry-run --preset daily --time 24:00
assert_fails "$INSTALL_CRON" --dry-run --preset custom --cron-expression "* * *"
assert_fails "$INSTALL_CRON" --dry-run --time

"$ROLLBACK" --help >/dev/null
assert_fails "$ROLLBACK" --node pve --vmid 100 --snapshot pre-update --timeout 0

echo "shell checks passed"
