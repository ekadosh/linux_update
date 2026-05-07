#!/usr/bin/env python3
"""Roll back a Proxmox QEMU VM to an existing snapshot."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.parse import urlparse


class RollbackError(RuntimeError):
    """Raised when snapshot rollback cannot complete."""


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RollbackError(f"Missing required environment variable: {name}")
    return value


def parse_endpoint() -> dict[str, Any]:
    url = os.getenv("PROXMOX_URL", "https://localhost:8006")
    parsed = urlparse(url)
    host = os.getenv("PROXMOX_HOST") or parsed.hostname
    if not host:
        raise RollbackError("Set PROXMOX_HOST or a valid PROXMOX_URL")

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
        raise RollbackError(
            "Missing proxmoxer. Run `make bootstrap` before rolling back snapshots."
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
        raise RollbackError(
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
            raise RollbackError(f"Task {task_id} failed: {status}")
        time.sleep(1)
    raise RollbackError(f"Timed out waiting for task {task_id}")


def rollback(
    proxmox: Any,
    node: str,
    vmid: str,
    snapshot: str,
    timeout: int,
    start: bool,
) -> dict[str, Any]:
    snapshot_api = proxmox.nodes(node).qemu(vmid).snapshot(snapshot)
    task_id = snapshot_api.rollback.post(start=1 if start else 0)
    wait_for_task(proxmox, node, task_id, timeout)
    return {
        "node": node,
        "vmid": vmid,
        "snapshot": snapshot,
        "start": start,
        "task": task_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Roll back a Proxmox QEMU snapshot")
    parser.add_argument("--node", required=True, help="Proxmox node name")
    parser.add_argument("--vmid", required=True, help="QEMU VMID")
    parser.add_argument("--snapshot", required=True, help="Snapshot name to roll back to")
    parser.add_argument("--timeout", type=int, default=300, help="Task timeout seconds")
    parser.add_argument(
        "--start",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Start the VM after rollback, enabled by default",
    )
    args = parser.parse_args()

    if args.timeout < 1:
        print("timeout must be at least 1", file=sys.stderr)
        return 2

    try:
        result = rollback(
            connect(parse_endpoint()),
            args.node,
            args.vmid,
            args.snapshot,
            args.timeout,
            args.start,
        )
    except RollbackError as exc:
        print(f"Snapshot rollback error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
