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

mkdir -p "$LOG_DIR"
timestamp="$(date +%Y%m%d-%H%M%S)"
log_file="$LOG_DIR/update-$timestamp.log"
started_at="$(date -Is)"

cd "$ROOT_DIR"
. "$ROOT_DIR/.venv/bin/activate"

run_connectivity_check() {
  local ssh_args="$1"
  shift

  ANSIBLE_SSH_ARGS="$ssh_args" \
    ansible -i "$STATIC_INVENTORY" -i "$PROXMOX_INVENTORY" \
    linux_update_targets -m ansible.builtin.ping "$@"
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
  if run_connectivity_check "$DEFAULT_ANSIBLE_SSH_ARGS" "${connectivity_args[@]}"; then
    ANSIBLE_SSH_ARGS="$DEFAULT_ANSIBLE_SSH_ARGS"
    export ANSIBLE_SSH_ARGS
    return 0
  fi

  echo "Default SSH connectivity failed. Retrying with compatibility SSH options:"
  echo "  $SSH_COMPATIBILITY_ARGS"
  if run_connectivity_check "$SSH_COMPATIBILITY_ARGS" "${connectivity_args[@]}"; then
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

if [[ "${ALERT_EMAIL_ENABLED:-true}" == "true" ]]; then
  if ! "$ROOT_DIR/scripts/send_run_email.py" \
    --status "$ansible_status" \
    --log-file "$log_file" \
    --started-at "$started_at" \
    --finished-at "$finished_at"; then
    echo "Email alert failed" >&2
  fi
fi

exit "$ansible_status"
