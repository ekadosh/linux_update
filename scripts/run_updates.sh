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

mkdir -p "$LOG_DIR"
timestamp="$(date +%Y%m%d-%H%M%S)"
log_file="$LOG_DIR/update-$timestamp.log"
started_at="$(date -Is)"

cd "$ROOT_DIR"
. "$ROOT_DIR/.venv/bin/activate"

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
