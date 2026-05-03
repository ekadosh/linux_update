#!/usr/bin/env python3
"""Prune only automation-created Proxmox snapshots for one QEMU VM."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.parse import urlparse


SNAPSHOT_PREFIX = "ansible-pre-update-"


class PruneError(RuntimeError):
    """Raised when snapshot pruning cannot complete."""


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise PruneError(f"Missing required environment variable: {name}")
    return value


def parse_endpoint() -> dict[str, Any]:
    url = os.getenv("PROXMOX_URL", "https://localhost:8006")
    parsed = urlparse(url)
    host = os.getenv("PROXMOX_HOST") or parsed.hostname
    if not host:
        raise PruneError("Set PROXMOX_HOST or a valid PROXMOX_URL")

    port_value = os.getenv("PROXMOX_PORT")
    return {
        "host": host,
        "port": int(port_value) if port_value else parsed.port or 8006,
        "scheme": parsed.scheme or "https",
        "validate_certs": env_bool("PROXMOX_VALIDATE_CERTS", default=True),
    }


def connect(endpoint: dict[str, Any]) -> Any:
    try:
        from proxmoxer import ProxmoxAPI
    except ImportError as exc:
        raise PruneError(
            "Missing proxmoxer. Run `make bootstrap` before pruning snapshots."
        ) from exc

    user = require_env("PROXMOX_USER")
    token_id = os.getenv("PROXMOX_TOKEN_ID")
    token_secret = os.getenv("PROXMOX_TOKEN_SECRET")
    password = os.getenv("PROXMOX_PASSWORD")

    if token_id and "!" in token_id:
        token_id = token_id.split("!", 1)[1]

    kwargs: dict[str, Any] = {
        "user": user,
        "port": endpoint["port"],
        "verify_ssl": endpoint["validate_certs"],
        "timeout": int(os.getenv("PROXMOX_TIMEOUT", "15")),
    }

    if token_id and token_secret:
        kwargs["token_name"] = token_id
        kwargs["token_value"] = token_secret
    elif password:
        kwargs["password"] = password
    else:
        raise PruneError(
            "Set PROXMOX_TOKEN_ID and PROXMOX_TOKEN_SECRET, or PROXMOX_PASSWORD."
        )

    return ProxmoxAPI(endpoint["host"], backend="https", **kwargs)


def wait_for_task(proxmox: Any, node: str, task_id: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = proxmox.nodes(node).tasks(task_id).status.get()
        if status.get("status") == "stopped":
            if status.get("exitstatus") == "OK":
                return
            raise PruneError(f"Task {task_id} failed: {status}")
        time.sleep(1)
    raise PruneError(f"Timed out waiting for task {task_id}")


def prune(proxmox: Any, node: str, vmid: str, keep: int, timeout: int) -> dict[str, Any]:
    snapshots = proxmox.nodes(node).qemu(vmid).snapshot.get()
    automation_snapshots = [
        snapshot
        for snapshot in snapshots
        if str(snapshot.get("name", "")).startswith(SNAPSHOT_PREFIX)
    ]
    automation_snapshots.sort(key=lambda snapshot: int(snapshot.get("snaptime", 0)))

    delete_count = max(0, len(automation_snapshots) - keep)
    to_delete = automation_snapshots[:delete_count]
    deleted: list[str] = []

    for snapshot in to_delete:
        name = str(snapshot["name"])
        task_id = proxmox.nodes(node).qemu(vmid).snapshot(name).delete()
        wait_for_task(proxmox, node, task_id, timeout)
        deleted.append(name)

    return {
        "matched": [str(snapshot["name"]) for snapshot in automation_snapshots],
        "deleted": deleted,
        "kept": keep,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prune Ansible-created snapshots")
    parser.add_argument("--node", required=True, help="Proxmox node name")
    parser.add_argument("--vmid", required=True, help="QEMU VMID")
    parser.add_argument("--keep", type=int, default=3, help="Snapshots to keep")
    parser.add_argument("--timeout", type=int, default=120, help="Task timeout seconds")
    args = parser.parse_args()

    if args.keep < 1:
        print("keep must be at least 1", file=sys.stderr)
        return 2

    try:
        result = prune(connect(parse_endpoint()), args.node, args.vmid, args.keep, args.timeout)
    except PruneError as exc:
        print(f"Snapshot prune error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
