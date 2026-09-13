#!/usr/bin/env bash
# Refresh ~/.ssh/known_hosts from the current inventory. Changed host keys are
# verified through the Proxmox guest agent before they replace the old entry;
# see scripts/refresh_known_hosts.py for the rules.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

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

cd "$ROOT_DIR"
. "$ROOT_DIR/.venv/bin/activate"
export ANSIBLE_REMOTE_USER="${ANSIBLE_SSH_USER:-ansible}"

exec python "$ROOT_DIR/scripts/refresh_known_hosts.py" "$@"
