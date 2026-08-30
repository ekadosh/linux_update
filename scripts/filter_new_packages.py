#!/usr/bin/env python3
"""Track first-seen dates for upgradable apt packages.

Subcommands
-----------
record
    Parse the current apt upgrade simulation, add any previously-unseen
    name=version pairs to the on-disk database with today's timestamp, and
    save the database.  Prints nothing to stdout.

list-holdable --min-age DAYS
    Print a JSON array of package names whose candidate version has been in
    the database for fewer than DAYS days.  Packages not yet in the database
    are always included.  The list is used by the Ansible playbook to mark
    those packages on hold before upgrading so that only packages that have
    been available for at least DAYS days are installed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def get_upgradable() -> dict[str, str]:
    """Return {package_name: candidate_version} by simulating apt-get upgrade."""
    result = subprocess.run(
        ["apt-get", "--just-print", "upgrade"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )
    packages: dict[str, str] = {}
    for line in result.stdout.splitlines():
        # Output lines look like:
        #   Inst unifi [8.1.113] (8.2.93 unknown [amd64])
        if not line.startswith("Inst "):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[1]
        for part in parts[2:]:
            if part.startswith("("):
                packages[name] = part.lstrip("(")
                break
    return packages


def load_db(db_path: Path) -> dict[str, str]:
    if db_path.exists():
        try:
            return json.loads(db_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_db(db_path: Path, db: dict[str, str]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text(json.dumps(db, indent=2, sort_keys=True) + "\n")


def cmd_record(db_path: Path) -> int:
    db = load_db(db_path)
    upgradable = get_upgradable()
    now = datetime.now(timezone.utc).isoformat()
    changed = False
    for name, version in upgradable.items():
        key = f"{name}={version}"
        if key not in db:
            db[key] = now
            changed = True
    if changed:
        save_db(db_path, db)
    return 0


def cmd_list_holdable(db_path: Path, min_age_days: int) -> int:
    db = load_db(db_path)
    upgradable = get_upgradable()
    now = datetime.now(timezone.utc)
    to_hold: list[str] = []
    for name, version in upgradable.items():
        key = f"{name}={version}"
        if key not in db:
            to_hold.append(name)
            continue
        first_seen = datetime.fromisoformat(db[key])
        if (now - first_seen).days < min_age_days:
            to_hold.append(name)
    print(json.dumps(sorted(to_hold)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--db-path",
        default="/var/lib/ansible-linux-update/package-first-seen.json",
        help="Path to the first-seen JSON database on the managed host",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("record", help="Record first-seen dates for upgradable packages")

    list_p = sub.add_parser("list-holdable", help="List packages to hold due to age filter")
    list_p.add_argument("--min-age", type=int, required=True, metavar="DAYS", help="Minimum age in days")

    args = parser.parse_args()
    db_path = Path(args.db_path)

    if args.command == "record":
        return cmd_record(db_path)
    if args.command == "list-holdable":
        return cmd_list_holdable(db_path, args.min_age)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
