#!/usr/bin/env python3
"""Dynamic inventory for Proxmox QEMU guests using QEMU guest agent IPs."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from typing import Any
from urllib.parse import urlparse


IGNORED_INTERFACE_PREFIXES = (
    "lo",
    "docker",
    "br-",
    "veth",
    "virbr",
)


class InventoryError(RuntimeError):
    """Raised when inventory cannot be built."""


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise InventoryError(f"Missing required environment variable: {name}")
    return value


def parse_proxmox_endpoint() -> dict[str, Any]:
    url = os.getenv("PROXMOX_URL", "https://localhost:8006")
    parsed = urlparse(url)
    host = os.getenv("PROXMOX_HOST") or parsed.hostname
    if not host:
        raise InventoryError("Set PROXMOX_HOST or a valid PROXMOX_URL")

    port_value = os.getenv("PROXMOX_PORT")
    port = int(port_value) if port_value else parsed.port or 8006
    scheme = parsed.scheme or "https"

    return {
        "host": host,
        "port": port,
        "scheme": scheme,
        "validate_certs": env_bool("PROXMOX_VALIDATE_CERTS", default=True),
    }


def parse_tags(raw_tags: Any) -> list[str]:
    if raw_tags is None:
        return []
    if isinstance(raw_tags, list):
        return [str(tag).strip() for tag in raw_tags if str(tag).strip()]
    return [
        tag.strip()
        for tag in str(raw_tags).replace(",", ";").split(";")
        if tag.strip()
    ]


def connect(endpoint: dict[str, Any]) -> Any:
    try:
        from proxmoxer import ProxmoxAPI
    except ImportError as exc:
        raise InventoryError(
            "Missing proxmoxer. Run `make bootstrap` before using inventory."
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
        raise InventoryError(
            "Set PROXMOX_TOKEN_ID and PROXMOX_TOKEN_SECRET, or PROXMOX_PASSWORD."
        )

    return ProxmoxAPI(endpoint["host"], backend="https", **kwargs)


def get_guest_interfaces(proxmox: Any, node: str, vmid: str) -> list[dict[str, Any]]:
    try:
        response = proxmox.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
    except Exception as exc:  # noqa: BLE001 - preserve inventory error context.
        raise InventoryError(
            f"VM {vmid} on node {node} did not return guest-agent network data: {exc}"
        ) from exc

    if isinstance(response, dict):
        interfaces = response.get("result", response.get("data", []))
    else:
        interfaces = response

    if not isinstance(interfaces, list):
        raise InventoryError(f"Unexpected guest-agent response for VM {vmid}")

    return interfaces


def first_usable_ipv4(interfaces: list[dict[str, Any]]) -> str | None:
    for interface in interfaces:
        name = str(interface.get("name", ""))
        if name.startswith(IGNORED_INTERFACE_PREFIXES):
            continue

        addresses = interface.get("ip-addresses") or interface.get("ip_addresses") or []
        for address in addresses:
            if address.get("ip-address-type") not in {None, "ipv4"}:
                continue

            raw_ip = str(address.get("ip-address", "")).split("/", 1)[0]
            if not raw_ip:
                continue

            try:
                ip = ipaddress.ip_address(raw_ip)
            except ValueError:
                continue

            if (
                ip.version == 4
                and not ip.is_loopback
                and not ip.is_link_local
                and not ip.is_unspecified
            ):
                return str(ip)
    return None


def build_inventory() -> dict[str, Any]:
    endpoint = parse_proxmox_endpoint()
    proxmox = connect(endpoint)
    required_tag = os.getenv("PROXMOX_VM_TAG", "ansible-update")
    ssh_user = require_env("ANSIBLE_SSH_USER")
    private_key_file = os.getenv("ANSIBLE_PRIVATE_KEY_FILE")

    try:
        resources = proxmox.cluster.resources.get(type="vm")
    except Exception as exc:  # noqa: BLE001 - return a readable inventory error.
        raise InventoryError(f"Unable to query Proxmox VM resources: {exc}") from exc

    hosts: list[str] = []
    hostvars: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []

    for vm in resources:
        if vm.get("type") != "qemu":
            continue
        if vm.get("status") != "running":
            continue

        tags = parse_tags(vm.get("tags"))
        if required_tag not in tags:
            continue

        node = str(vm.get("node", ""))
        vmid = str(vm.get("vmid", ""))
        name = str(vm.get("name") or f"vm-{vmid}")

        interfaces = get_guest_interfaces(proxmox, node, vmid)
        ansible_host = first_usable_ipv4(interfaces)
        if not ansible_host:
            skipped.append(f"{name} ({vmid})")
            continue

        host_vars: dict[str, Any] = {
            "ansible_host": ansible_host,
            "ansible_user": ssh_user,
            "ansible_become": True,
            "proxmox_vmid": vmid,
            "proxmox_node": node,
            "proxmox_name": name,
            "proxmox_tags": tags,
            "proxmox_api_host": endpoint["host"],
            "proxmox_api_port": endpoint["port"],
            "proxmox_validate_certs": endpoint["validate_certs"],
        }
        if private_key_file:
            host_vars["ansible_ssh_private_key_file"] = private_key_file

        hosts.append(name)
        hostvars[name] = host_vars

    inventory = {
        "all": {"children": ["linux_update_targets"]},
        "linux_update_targets": {"children": ["proxmox_ansible_update"]},
        "proxmox_ansible_update": {"hosts": sorted(hosts)},
        "_meta": {"hostvars": hostvars},
    }
    if skipped:
        inventory["_meta"]["skipped_no_guest_agent_ipv4"] = skipped

    return inventory


def main() -> int:
    parser = argparse.ArgumentParser(description="Proxmox QEMU guest-agent inventory")
    parser.add_argument("--list", action="store_true", help="Emit full inventory")
    parser.add_argument("--host", help="Emit host variables for one host")
    args = parser.parse_args()

    if args.host:
        print(json.dumps({}))
        return 0

    try:
        inventory = build_inventory()
    except InventoryError as exc:
        print(f"Inventory error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
