#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKER="# linux_update_ansible"
DEFAULT_PRESET="weekly"
DEFAULT_TIME="03:00"
DEFAULT_WEEKDAY="0"
DEFAULT_MONTH_DAY="1"

preset="${INSTALL_CRON_PRESET:-}"
run_time="${INSTALL_CRON_TIME:-}"
weekday="${INSTALL_CRON_WEEKDAY:-}"
month_day="${INSTALL_CRON_MONTH_DAY:-}"
cron_expression="${INSTALL_CRON_EXPRESSION:-}"
dry_run=false

usage() {
  cat <<EOF
Usage: scripts/install_cron.sh [options]

Options:
  --preset daily|weekly|monthly|weekdays|custom
  --time HH:MM
  --weekday 0-7|sun|mon|tue|wed|thu|fri|sat
  --month-day 1-31
  --cron-expression "MIN HOUR DOM MON DOW"
  --dry-run
  -h, --help

Without options, the installer prompts for a schedule. The default is weekly
Sunday at 03:00.
EOF
}

require_option_value() {
  local option="$1"
  local value="${2:-}"

  if [[ -z "$value" || "$value" == --* ]]; then
    echo "$option requires a value" >&2
    exit 2
  fi
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --preset)
      require_option_value "$1" "${2:-}"
      preset="${2:-}"
      shift 2
      ;;
    --preset=*)
      preset="${1#*=}"
      shift
      ;;
    --time)
      require_option_value "$1" "${2:-}"
      run_time="${2:-}"
      shift 2
      ;;
    --time=*)
      run_time="${1#*=}"
      shift
      ;;
    --weekday)
      require_option_value "$1" "${2:-}"
      weekday="${2:-}"
      shift 2
      ;;
    --weekday=*)
      weekday="${1#*=}"
      shift
      ;;
    --month-day)
      require_option_value "$1" "${2:-}"
      month_day="${2:-}"
      shift 2
      ;;
    --month-day=*)
      month_day="${1#*=}"
      shift
      ;;
    --cron-expression)
      require_option_value "$1" "${2:-}"
      cron_expression="${2:-}"
      shift 2
      ;;
    --cron-expression=*)
      cron_expression="${1#*=}"
      shift
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

prompt_value() {
  local prompt="$1"
  local default="$2"
  local value=""

  read -r -p "$prompt [$default]: " value
  printf '%s\n' "${value:-$default}"
}

prompt_preset() {
  local value=""

  cat >&2 <<EOF
Choose when linux_update should run:
  1) Daily
  2) Weekly
  3) Monthly
  4) Weekdays
  5) Custom cron expression
EOF
  read -r -p "Schedule [2]: " value
  case "${value:-2}" in
    1 | daily) printf '%s\n' "daily" ;;
    2 | weekly) printf '%s\n' "weekly" ;;
    3 | monthly) printf '%s\n' "monthly" ;;
    4 | weekdays) printf '%s\n' "weekdays" ;;
    5 | custom) printf '%s\n' "custom" ;;
    *)
      echo "Invalid schedule choice: $value" >&2
      return 1
      ;;
  esac
}

validate_time() {
  local value="$1"
  if [[ ! "$value" =~ ^([01]?[0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    echo "Invalid time: $value. Use HH:MM in 24-hour time." >&2
    return 1
  fi
}

cron_minute() {
  printf '%s\n' "$((10#${1#*:}))"
}

cron_hour() {
  printf '%s\n' "$((10#${1%:*}))"
}

normalize_weekday() {
  local value=""
  value="$(printf '%s\n' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    sun | sunday) printf '%s\n' "0" ;;
    mon | monday) printf '%s\n' "1" ;;
    tue | tues | tuesday) printf '%s\n' "2" ;;
    wed | wednesday) printf '%s\n' "3" ;;
    thu | thur | thurs | thursday) printf '%s\n' "4" ;;
    fri | friday) printf '%s\n' "5" ;;
    sat | saturday) printf '%s\n' "6" ;;
    [0-7]) printf '%s\n' "$value" ;;
    *)
      echo "Invalid weekday: $1. Use 0-7 or a weekday name." >&2
      return 1
      ;;
  esac
}

validate_month_day() {
  local value="$1"
  if [[ ! "$value" =~ ^[0-9]+$ ]] || (( value < 1 || value > 31 )); then
    echo "Invalid month day: $value. Use 1-31." >&2
    return 1
  fi
}

validate_cron_expression() {
  local value="$1"
  local field_count=0

  field_count="$(awk '{ print NF }' <<<"$value")"
  if [[ "$field_count" -ne 5 ]]; then
    echo "Invalid cron expression: expected 5 fields." >&2
    return 1
  fi
  if [[ "$value" == *$'\n'* ]]; then
    echo "Invalid cron expression: newlines are not allowed." >&2
    return 1
  fi
}

shell_quote() {
  local value="$1"
  printf "'%s'" "${value//\'/\'\\\'\'}"
}

build_cron_expression() {
  local selected_preset="$1"
  local selected_time="$2"
  local selected_weekday="$3"
  local selected_month_day="$4"
  local custom_expression="$5"
  local minute=""
  local hour=""

  case "$selected_preset" in
    daily | weekly | monthly | weekdays)
      validate_time "$selected_time" || return 1
      minute="$(cron_minute "$selected_time")"
      hour="$(cron_hour "$selected_time")"
      ;;
  esac

  case "$selected_preset" in
    daily)
      printf '%s\n' "$minute $hour * * *"
      ;;
    weekly)
      selected_weekday="$(normalize_weekday "$selected_weekday")" || return 1
      printf '%s\n' "$minute $hour * * $selected_weekday"
      ;;
    monthly)
      validate_month_day "$selected_month_day" || return 1
      printf '%s\n' "$minute $hour $selected_month_day * *"
      ;;
    weekdays)
      printf '%s\n' "$minute $hour * * 1-5"
      ;;
    custom)
      validate_cron_expression "$custom_expression" || return 1
      printf '%s\n' "$custom_expression"
      ;;
    *)
      echo "Invalid preset: $selected_preset" >&2
      return 1
      ;;
  esac
}

if [[ -z "$preset" ]]; then
  if [[ -t 0 ]]; then
    preset="$(prompt_preset)"
  else
    preset="$DEFAULT_PRESET"
  fi
fi

case "$preset" in
  daily | weekly | monthly | weekdays | custom)
    ;;
  *)
    echo "Invalid preset: $preset" >&2
    usage >&2
    exit 2
    ;;
esac

if [[ "$preset" == "custom" ]]; then
  if [[ -z "$cron_expression" ]]; then
    if [[ -t 0 ]]; then
      cron_expression="$(prompt_value "Cron expression" "0 3 * * 0")"
    else
      echo "--cron-expression is required with --preset custom" >&2
      exit 2
    fi
  fi
else
  if [[ -z "$run_time" ]]; then
    if [[ -t 0 ]]; then
      run_time="$(prompt_value "Run time (24-hour HH:MM)" "$DEFAULT_TIME")"
    else
      run_time="$DEFAULT_TIME"
    fi
  fi

  if [[ "$preset" == "weekly" && -z "$weekday" ]]; then
    if [[ -t 0 ]]; then
      weekday="$(prompt_value "Weekday (0=Sunday, 1=Monday, ... 6=Saturday)" "$DEFAULT_WEEKDAY")"
    else
      weekday="$DEFAULT_WEEKDAY"
    fi
  fi

  if [[ "$preset" == "monthly" && -z "$month_day" ]]; then
    if [[ -t 0 ]]; then
      month_day="$(prompt_value "Day of month" "$DEFAULT_MONTH_DAY")"
    else
      month_day="$DEFAULT_MONTH_DAY"
    fi
  fi
fi

if ! cron_schedule="$(build_cron_expression "$preset" "$run_time" "${weekday:-$DEFAULT_WEEKDAY}" "${month_day:-$DEFAULT_MONTH_DAY}" "$cron_expression")"; then
  exit 2
fi
quoted_root="$(shell_quote "$ROOT_DIR")"
quoted_runner="$(shell_quote "$ROOT_DIR/scripts/run_updates.sh")"
CRON_LINE="$cron_schedule cd $quoted_root && $quoted_runner >/dev/null 2>&1 $MARKER"

if [[ "$dry_run" == "true" ]]; then
  echo "Dry run: cron entry was not installed."
  echo "$CRON_LINE"
  exit 0
fi

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

echo "Installed cron entry:"
echo "$CRON_LINE"
