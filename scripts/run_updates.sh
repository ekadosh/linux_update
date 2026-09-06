#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
LOG_DIR="$ROOT_DIR/logs"
PLAYBOOK="$ROOT_DIR/playbooks/update_ubuntu.yml"
STATIC_INVENTORY="$ROOT_DIR/inventory/static_hosts.yml"
PROXMOX_INVENTORY="$ROOT_DIR/inventory/proxmox_guest_agent.py"

if [[ ! -d "$ROOT_DIR/.venv" ]]; then
  echo "Missing .venv. Run: make bootstrap" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE. Copy .env.example to .env and fill in values." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
export ANSIBLE_REMOTE_USER="${ANSIBLE_SSH_USER:-ansible}"
DEFAULT_ANSIBLE_SSH_ARGS="${ANSIBLE_SSH_ARGS:--o ControlMaster=auto -o ControlPersist=60s}"
SSH_COMPATIBILITY_MODE="${SSH_COMPATIBILITY_MODE:-auto}"
SSH_COMPATIBILITY_ARGS="${SSH_COMPATIBILITY_ARGS:--o ControlMaster=no -o KexAlgorithms=curve25519-sha256 -o HostKeyAlgorithms=ssh-ed25519 -o IPQoS=none}"
SSH_PREFLIGHT_CONNECT_TIMEOUT="${SSH_PREFLIGHT_CONNECT_TIMEOUT:-10}"
SSH_PREFLIGHT_WALL_TIMEOUT="${SSH_PREFLIGHT_WALL_TIMEOUT:-45}"
SSH_PREFLIGHT_REQUIRE_ALL="${SSH_PREFLIGHT_REQUIRE_ALL:-false}"

if [[ -n "${ANSIBLE_PRIVATE_KEY_FILE:-}" ]]; then
  ANSIBLE_PRIVATE_KEY_FILE="${ANSIBLE_PRIVATE_KEY_FILE/#\~/$HOME}"
  export ANSIBLE_PRIVATE_KEY_FILE
fi

mkdir -p "$LOG_DIR"
timestamp="$(date +%Y%m%d-%H%M%S)"
log_file="$LOG_DIR/update-$timestamp.log"
started_at="$(date -Is)"

cd "$ROOT_DIR"
. "$ROOT_DIR/.venv/bin/activate"

run_mode() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --check | -C)
        echo "dry-run"
        return 0
        ;;
      *)
        shift
        ;;
    esac
  done

  echo "update"
}

validate_ssh_key() {
  if [[ -z "${ANSIBLE_PRIVATE_KEY_FILE:-}" ]]; then
    return 0
  fi

  if [[ -f "$ANSIBLE_PRIVATE_KEY_FILE" ]]; then
    return 0
  fi

  cat >&2 <<EOF
SSH private key not found.

Configured path:
  ANSIBLE_PRIVATE_KEY_FILE=$ANSIBLE_PRIVATE_KEY_FILE

Fix .env so ANSIBLE_PRIVATE_KEY_FILE points to the actual key on this runner.
For example:
  ANSIBLE_PRIVATE_KEY_FILE=$HOME/.ssh/ansible_ed25519

EOF
  return 1
}

connectivity_args=()
user_limit=""
inventory_hosts=()
preflight_reachable=()
preflight_unreachable=()
chosen_reachable=()
chosen_unreachable=()
unreachable_hosts=()
playbook_limit_args=()

list_inventory_hosts() {
  ansible-inventory -i "$STATIC_INVENTORY" -i "$PROXMOX_INVENTORY" --list "$@" 2>/dev/null \
    | python -c '
import json
import sys

inventory = json.load(sys.stdin)
hosts = set()
seen_groups = set()
pending = ["linux_update_targets"]

while pending:
    group = pending.pop()
    if group in seen_groups:
        continue
    seen_groups.add(group)
    data = inventory.get(group)
    if not isinstance(data, dict):
        continue
    hosts.update(data.get("hosts", []))
    pending.extend(data.get("children", []))

for host in sorted(hosts):
    print(host)
'
}

run_connectivity_check() {
  local label="$1"
  shift
  local ssh_args="$1"
  shift
  local status=0
  local output_file
  output_file="$(mktemp)"

  echo "SSH preflight [$label]"
  echo "  connect timeout: ${SSH_PREFLIGHT_CONNECT_TIMEOUT}s"
  echo "  wall timeout: ${SSH_PREFLIGHT_WALL_TIMEOUT}s"
  echo "  ssh args: $ssh_args"
  if [[ ${#connectivity_args[@]} -gt 0 ]]; then
    echo "  inventory limit args: ${connectivity_args[*]}"
  fi

  timeout --foreground "$SSH_PREFLIGHT_WALL_TIMEOUT" \
    env ANSIBLE_SSH_ARGS="$ssh_args" \
    ansible -i "$STATIC_INVENTORY" -i "$PROXMOX_INVENTORY" \
    linux_update_targets -m ansible.builtin.raw -a true \
    -e ansible_become=false -T "$SSH_PREFLIGHT_CONNECT_TIMEOUT" "$@" \
    >"$output_file" 2>&1
  status=$?

  cat "$output_file"

  # A host counts as reachable only when it answered this preflight. Anything
  # else -- rejected key, timeout, or no output at all -- is treated as
  # unreachable so the run can continue without it.
  mapfile -t preflight_reachable < <(
    awk '$2 == "|" && ($3 == "SUCCESS" || $3 == "CHANGED") { print $1 }' "$output_file" | sort -u
  )
  mapfile -t preflight_unreachable < <(
    awk '$2 == "|" && $3 ~ /^UNREACHABLE/ { print $1 }' "$output_file" | sort -u
  )
  rm -f "$output_file"

  case "$status" in
    0)
      echo "SSH preflight [$label] succeeded."
      ;;
    124)
      echo "SSH preflight [$label] timed out after ${SSH_PREFLIGHT_WALL_TIMEOUT}s." >&2
      ;;
    *)
      echo "SSH preflight [$label] failed with exit status $status." >&2
      ;;
  esac

  return "$status"
}

build_connectivity_args() {
  connectivity_args=()
  user_limit=""

  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --limit | -l)
        if [[ "$#" -lt 2 ]]; then
          echo "$1 requires a value" >&2
          return 1
        fi
        connectivity_args+=("$1" "$2")
        user_limit="$2"
        shift 2
        ;;
      --limit=*)
        connectivity_args+=("$1")
        user_limit="${1#*=}"
        shift
        ;;
      *)
        shift
        ;;
    esac
  done
}

# Turn the hosts that answered the preflight into a --limit for the playbook so
# one unreachable host cannot cancel updates for the whole fleet.
handle_partial_reachability() {
  local label="$1"
  local host=""
  local reached=""
  local matched=false
  local skip_pattern=""
  local limit_value=""

  unreachable_hosts=()
  if [[ ${#inventory_hosts[@]} -gt 0 ]]; then
    for host in "${inventory_hosts[@]}"; do
      matched=false
      for reached in "${chosen_reachable[@]}"; do
        if [[ "$host" == "$reached" ]]; then
          matched=true
          break
        fi
      done
      if [[ "$matched" == false ]]; then
        unreachable_hosts+=("$host")
      fi
    done
  else
    unreachable_hosts=("${chosen_unreachable[@]}")
  fi

  if [[ ${#unreachable_hosts[@]} -eq 0 ]]; then
    return 0
  fi

  if [[ "$SSH_PREFLIGHT_REQUIRE_ALL" == "true" ]]; then
    echo "SSH preflight [$label] could not reach: ${unreachable_hosts[*]}" >&2
    echo "SSH_PREFLIGHT_REQUIRE_ALL=true, so the whole run is aborted." >&2
    return 1
  fi

  for host in "${unreachable_hosts[@]}"; do
    echo "Skipping unreachable host: $host (SSH preflight [$label] could not connect)"
    skip_pattern+=":!$host"
  done

  limit_value="${user_limit:-all}$skip_pattern"
  playbook_limit_args=(--limit "$limit_value")
  echo "Continuing with ${#chosen_reachable[@]} reachable host(s): --limit $limit_value"
  return 0
}

choose_ssh_args() {
  build_connectivity_args "$@" || return 1

  case "$SSH_COMPATIBILITY_MODE" in
    always)
      echo "SSH compatibility mode forced on."
      ANSIBLE_SSH_ARGS="$SSH_COMPATIBILITY_ARGS"
      export ANSIBLE_SSH_ARGS
      return 0
      ;;
    never)
      echo "SSH compatibility fallback disabled."
      ANSIBLE_SSH_ARGS="$DEFAULT_ANSIBLE_SSH_ARGS"
      export ANSIBLE_SSH_ARGS
      return 0
      ;;
    auto)
      ;;
    *)
      echo "Invalid SSH_COMPATIBILITY_MODE=$SSH_COMPATIBILITY_MODE. Use auto, always, or never." >&2
      return 1
      ;;
  esac

  mapfile -t inventory_hosts < <(list_inventory_hosts "${connectivity_args[@]}")
  if [[ ${#inventory_hosts[@]} -eq 0 ]]; then
    echo "Could not list inventory hosts; falling back to preflight output to name unreachable hosts." >&2
  fi

  echo "Checking SSH connectivity with default SSH options"
  if run_connectivity_check "default" "$DEFAULT_ANSIBLE_SSH_ARGS" "${connectivity_args[@]}"; then
    ANSIBLE_SSH_ARGS="$DEFAULT_ANSIBLE_SSH_ARGS"
    export ANSIBLE_SSH_ARGS
    return 0
  fi
  local default_reachable=("${preflight_reachable[@]}")
  local default_unreachable=("${preflight_unreachable[@]}")

  echo "Default SSH connectivity failed. Retrying with compatibility SSH options:"
  echo "  $SSH_COMPATIBILITY_ARGS"
  if [[ "$SSH_COMPATIBILITY_ARGS" != *"HostKeyAlgorithms=ssh-ed25519"* ]]; then
    echo "  note: SSH_COMPATIBILITY_ARGS does not include HostKeyAlgorithms=ssh-ed25519" >&2
    echo "        add it in .env if this host stalls during SSH key exchange" >&2
  fi
  if run_connectivity_check "compatibility" "$SSH_COMPATIBILITY_ARGS" "${connectivity_args[@]}"; then
    echo "Compatibility SSH options succeeded; using them for this update run."
    ANSIBLE_SSH_ARGS="$SSH_COMPATIBILITY_ARGS"
    export ANSIBLE_SSH_ARGS
    return 0
  fi
  local compat_reachable=("${preflight_reachable[@]}")
  local compat_unreachable=("${preflight_unreachable[@]}")

  if [[ ${#default_reachable[@]} -eq 0 && ${#compat_reachable[@]} -eq 0 ]]; then
    echo "SSH connectivity failed with both default and compatibility SSH options." >&2
    return 1
  fi

  # Neither option set reached every host. Keep whichever reached more of them
  # and update those; the unreachable ones are reported and skipped.
  if [[ ${#compat_reachable[@]} -gt ${#default_reachable[@]} ]]; then
    echo "Compatibility SSH options reached more hosts; using them for this update run."
    ANSIBLE_SSH_ARGS="$SSH_COMPATIBILITY_ARGS"
    export ANSIBLE_SSH_ARGS
    chosen_reachable=("${compat_reachable[@]}")
    chosen_unreachable=("${compat_unreachable[@]}")
    handle_partial_reachability "compatibility"
    return $?
  fi

  echo "Default SSH options reached the most hosts; using them for this update run."
  ANSIBLE_SSH_ARGS="$DEFAULT_ANSIBLE_SSH_ARGS"
  export ANSIBLE_SSH_ARGS
  chosen_reachable=("${default_reachable[@]}")
  chosen_unreachable=("${default_unreachable[@]}")
  handle_partial_reachability "default"
}

echo "Writing Ansible output to $log_file"
mode="$(run_mode "$@")"
echo "Run mode: $mode"
set +e
{
  validate_ssh_key
  key_status=$?

  if [[ "$key_status" -ne 0 ]]; then
    exit "$key_status"
  fi

  echo "Refreshing SSH known_hosts from current inventory"
  "$ROOT_DIR/scripts/seed_known_hosts.sh"
  seed_status=$?

  if [[ "$seed_status" -ne 0 ]]; then
    echo "known_hosts refresh failed with exit status $seed_status"
    exit "$seed_status"
  fi

  choose_ssh_args "$@"
  ssh_check_status=$?

  if [[ "$ssh_check_status" -ne 0 ]]; then
    echo "SSH preflight failed with exit status $ssh_check_status"
    exit "$ssh_check_status"
  fi

  echo "Running Ansible playbook"
  ansible-playbook -i "$STATIC_INVENTORY" -i "$PROXMOX_INVENTORY" "$PLAYBOOK" "$@" \
    ${playbook_limit_args[@]+"${playbook_limit_args[@]}"}
} 2>&1 | tee "$log_file"
ansible_status=${PIPESTATUS[0]}
set -e

finished_at="$(date -Is)"

if [[ "$ansible_status" -eq 130 || "$ansible_status" -eq 141 ]]; then
  echo "Run interrupted by user; skipping email alert."
elif [[ "${ALERT_EMAIL_ENABLED:-true}" == "true" ]]; then
  if ! "$ROOT_DIR/scripts/send_run_email.py" \
    --status "$ansible_status" \
    --log-file "$log_file" \
    --started-at "$started_at" \
    --finished-at "$finished_at" \
    --mode "$mode"; then
    echo "Email alert failed" >&2
  fi
fi

exit "$ansible_status"
