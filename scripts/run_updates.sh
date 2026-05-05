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
SSH_COMPATIBILITY_ARGS="${SSH_COMPATIBILITY_ARGS:--o ControlMaster=auto -o ControlPersist=60s -o KexAlgorithms=curve25519-sha256 -o IPQoS=none}"
SSH_PREFLIGHT_CONNECT_TIMEOUT="${SSH_PREFLIGHT_CONNECT_TIMEOUT:-10}"
SSH_PREFLIGHT_WALL_TIMEOUT="${SSH_PREFLIGHT_WALL_TIMEOUT:-45}"

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

run_connectivity_check() {
  local label="$1"
  shift
  local ssh_args="$1"
  shift
  local status=0

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
    linux_update_targets -m ansible.builtin.ping \
    -T "$SSH_PREFLIGHT_CONNECT_TIMEOUT" "$@"
  status=$?

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

  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --limit | -l)
        if [[ "$#" -lt 2 ]]; then
          echo "$1 requires a value" >&2
          return 1
        fi
        connectivity_args+=("$1" "$2")
        shift 2
        ;;
      --limit=*)
        connectivity_args+=("$1")
        shift
        ;;
      *)
        shift
        ;;
    esac
  done
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

  echo "Checking SSH connectivity with default SSH options"
  if run_connectivity_check "default" "$DEFAULT_ANSIBLE_SSH_ARGS" "${connectivity_args[@]}"; then
    ANSIBLE_SSH_ARGS="$DEFAULT_ANSIBLE_SSH_ARGS"
    export ANSIBLE_SSH_ARGS
    return 0
  fi

  echo "Default SSH connectivity failed. Retrying with compatibility SSH options:"
  echo "  $SSH_COMPATIBILITY_ARGS"
  if run_connectivity_check "compatibility" "$SSH_COMPATIBILITY_ARGS" "${connectivity_args[@]}"; then
    echo "Compatibility SSH options succeeded; using them for this update run."
    ANSIBLE_SSH_ARGS="$SSH_COMPATIBILITY_ARGS"
    export ANSIBLE_SSH_ARGS
    return 0
  fi

  echo "SSH connectivity failed with both default and compatibility SSH options." >&2
  return 1
}

echo "Writing Ansible output to $log_file"
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
  ansible-playbook -i "$STATIC_INVENTORY" -i "$PROXMOX_INVENTORY" "$PLAYBOOK" "$@"
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
    --finished-at "$finished_at"; then
    echo "Email alert failed" >&2
  fi
fi

exit "$ansible_status"
