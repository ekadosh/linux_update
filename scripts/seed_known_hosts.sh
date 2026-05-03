#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
STATIC_INVENTORY="$ROOT_DIR/inventory/static_hosts.yml"
PROXMOX_INVENTORY="$ROOT_DIR/inventory/proxmox_guest_agent.py"
KNOWN_HOSTS="${KNOWN_HOSTS:-$HOME/.ssh/known_hosts}"

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

mkdir -p "$(dirname "$KNOWN_HOSTS")"
touch "$KNOWN_HOSTS"
chmod 600 "$KNOWN_HOSTS"

cd "$ROOT_DIR"
. "$ROOT_DIR/.venv/bin/activate"
export ANSIBLE_REMOTE_USER="${ANSIBLE_SSH_USER:-ansible}"

mapfile -t hosts < <(
  ansible-inventory -i "$STATIC_INVENTORY" -i "$PROXMOX_INVENTORY" --list \
    | python -c '
import json
import sys

inventory = json.load(sys.stdin)
hostvars = inventory.get("_meta", {}).get("hostvars", {})
for name, vars in sorted(hostvars.items()):
    host = vars.get("ansible_host") or name
    if host:
        print(host)
'
)

if [[ ${#hosts[@]} -eq 0 ]]; then
  echo "No hosts discovered." >&2
  exit 1
fi

for host in "${hosts[@]}"; do
  if ssh-keygen -F "$host" -f "$KNOWN_HOSTS" >/dev/null; then
    echo "known: $host"
    continue
  fi

  echo "scanning: $host"
  ssh-keyscan -T 10 -H "$host" >> "$KNOWN_HOSTS"
done

echo "Known hosts updated: $KNOWN_HOSTS"
