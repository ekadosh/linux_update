#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m venv .venv
. .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

ansible-galaxy collection install -r requirements.yml -p ./.ansible/collections

echo "Bootstrap complete. Create .env from .env.example before running inventory."

