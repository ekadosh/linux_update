#!/usr/bin/env python3
"""Refresh SSH known_hosts from the current inventory.

For every inventory host the script scans the live SSH host keys and compares
them with known_hosts:

* unknown host  -> keys are added
* matching keys -> nothing to do
* changed keys  -> the new key is verified out-of-band before it replaces the
  old one. Proxmox VMs are verified by reading the guest's own
  /etc/ssh/ssh_host_*_key.pub through the QEMU guest agent, which does not
  travel over the SSH connection an attacker could intercept. A key that
  cannot be verified is left alone and reported, so the update run skips the
  host instead of trusting a possibly hostile key.

Set KNOWN_HOSTS_TRUST_CHANGED_KEYS=true to accept changed keys that could not be
verified (for example static hosts outside Proxmox). This weakens protection
against man-in-the-middle attacks and is off by default.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_INVENTORY = ROOT_DIR / "inventory" / "static_hosts.yml"
PROXMOX_INVENTORY = ROOT_DIR / "inventory" / "proxmox_guest_agent.py"

# Maps the key type ssh-keyscan reports to the file name sshd uses for it.
HOST_KEY_FILES = {
    "ssh-ed25519": "/etc/ssh/ssh_host_ed25519_key.pub",
    "ssh-rsa": "/etc/ssh/ssh_host_rsa_key.pub",
    "ecdsa-sha2-nistp256": "/etc/ssh/ssh_host_ecdsa_key.pub",
    "ecdsa-sha2-nistp384": "/etc/ssh/ssh_host_ecdsa_key.pub",
    "ecdsa-sha2-nistp521": "/etc/ssh/ssh_host_ecdsa_key.pub",
}

KeyMap = Dict[str, str]


class RefreshError(RuntimeError):
    """Raised when known_hosts cannot be refreshed at all."""


class VerificationUnavailable(RuntimeError):
    """Raised when a changed key cannot be checked out-of-band."""


@dataclass
class HostEntry:
    name: str
    address: str
    proxmox_node: Optional[str] = None
    proxmox_vmid: Optional[str] = None

    @property
    def is_proxmox_vm(self) -> bool:
        return bool(self.proxmox_node and self.proxmox_vmid)

    @property
    def label(self) -> str:
        if self.name == self.address:
            return self.name
        return f"{self.name} ({self.address})"


@dataclass
class ScanResult:
    keys: KeyMap = field(default_factory=dict)
    hashed_lines: List[str] = field(default_factory=list)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_key_lines(text: str) -> KeyMap:
    """Parse ssh-keyscan or `ssh-keygen -F` output into {key_type: key_data}."""
    keys: KeyMap = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if line.startswith("@"):
            # Marker lines: "@cert-authority host type key" / "@revoked ...".
            parts = parts[1:]
        if len(parts) < 3:
            continue
        key_type, key_data = parts[1], parts[2]
        if not key_type.startswith(("ssh-", "ecdsa-", "sk-")):
            continue
        keys.setdefault(key_type, key_data)
    return keys


def classify(stored: KeyMap, live: KeyMap) -> str:
    """Return one of: unreachable, new, known, changed."""
    if not live:
        return "unreachable"
    if not stored:
        return "new"
    common = set(stored) & set(live)
    if not common:
        # Nothing to compare (for example only an RSA key was stored and the
        # host now only offers ed25519). Treat as a change so it is verified.
        return "changed"
    if all(stored[key_type] == live[key_type] for key_type in common):
        return "known"
    return "changed"


def fingerprint(key_type: str, key_data: str) -> str:
    try:
        result = subprocess.run(
            ["ssh-keygen", "-lf", "-"],
            input=f"{key_type} {key_data}\n",
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return f"{key_type} {key_data[:16]}..."
    parts = result.stdout.split()
    return parts[1] if len(parts) > 1 else result.stdout.strip()


def decide(
    status: str,
    verifier: Optional[Callable[[], KeyMap]],
    live: KeyMap,
    trust_changed: bool,
) -> tuple[str, str]:
    """Decide what to do about a host key change.

    Returns (action, reason) where action is "replace" or "keep".
    """
    if status != "changed":
        return "keep", ""

    if verifier is not None:
        try:
            guest_keys = verifier()
        except VerificationUnavailable as exc:
            unavailable_reason = str(exc)
        else:
            comparable = set(guest_keys) & set(live)
            if comparable and all(guest_keys[t] == live[t] for t in comparable):
                return "replace", "verified through the Proxmox guest agent"
            if comparable:
                return (
                    "keep",
                    "the key presented on the network does not match the key "
                    "inside the VM (possible man-in-the-middle); left unchanged",
                )
            unavailable_reason = "the guest agent returned no comparable host key"
    else:
        unavailable_reason = "no out-of-band verification is available for this host"

    if trust_changed:
        return (
            "replace",
            f"UNVERIFIED ({unavailable_reason}); accepted because "
            "KNOWN_HOSTS_TRUST_CHANGED_KEYS=true",
        )
    return "keep", f"could not be verified: {unavailable_reason}; left unchanged"


def remediation_lines(entry: HostEntry, known_hosts: Path, live: KeyMap) -> List[str]:
    lines = []
    for key_type in sorted(live):
        lines.append(f"    new {key_type} fingerprint: {fingerprint(key_type, live[key_type])}")
    lines.append("    if this host was rebuilt on purpose, confirm the fingerprint on its console and run:")
    lines.append(f"      ssh-keygen -R '{entry.address}' -f '{known_hosts}'")
    lines.append(f"      ssh-keyscan -H '{entry.address}' >> '{known_hosts}'")
    if entry.is_proxmox_vm:
        lines.append(
            "    to let future runs verify rebuilt VMs automatically, grant the Proxmox API "
            "token the VM.GuestAgent.FileRead privilege (see README)."
        )
    return lines


def load_inventory_entries(inventory_json: Dict[str, Any]) -> List[HostEntry]:
    hostvars = inventory_json.get("_meta", {}).get("hostvars", {})
    entries: List[HostEntry] = []
    for name, variables in sorted(hostvars.items()):
        address = str(variables.get("ansible_host") or name)
        if not address:
            continue
        node = variables.get("proxmox_node")
        vmid = variables.get("proxmox_vmid")
        entries.append(
            HostEntry(
                name=name,
                address=address,
                proxmox_node=str(node) if node else None,
                proxmox_vmid=str(vmid) if vmid else None,
            )
        )
    return entries


def run_ansible_inventory() -> Dict[str, Any]:
    command = [
        "ansible-inventory",
        "-i",
        str(STATIC_INVENTORY),
        "-i",
        str(PROXMOX_INVENTORY),
        "--list",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RefreshError("ansible-inventory not found; run `make bootstrap`") from exc
    except subprocess.CalledProcessError as exc:
        raise RefreshError(f"ansible-inventory failed:\n{exc.stderr.strip()}") from exc
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RefreshError("ansible-inventory returned invalid JSON") from exc


def stored_keys(address: str, known_hosts: Path) -> KeyMap:
    result = subprocess.run(
        ["ssh-keygen", "-F", address, "-f", str(known_hosts)],
        capture_output=True,
        text=True,
    )
    return parse_key_lines(result.stdout)


def scan_host(address: str, timeout: int) -> ScanResult:
    result = subprocess.run(
        ["ssh-keyscan", "-T", str(timeout), "-H", address],
        capture_output=True,
        text=True,
    )
    hashed_lines = [
        line for line in result.stdout.splitlines() if line.strip() and not line.startswith("#")
    ]
    return ScanResult(keys=parse_key_lines(result.stdout), hashed_lines=hashed_lines)


def remove_host(address: str, known_hosts: Path) -> None:
    result = subprocess.run(
        ["ssh-keygen", "-R", address, "-f", str(known_hosts)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RefreshError(
            f"ssh-keygen -R {address} failed; fix {known_hosts} by hand:\n{result.stderr.strip()}"
        )


def append_lines(known_hosts: Path, lines: List[str]) -> None:
    with known_hosts.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line.rstrip("\n") + "\n")


class ProxmoxVerifier:
    """Reads a guest's own host-key files through the QEMU guest agent."""

    def __init__(self) -> None:
        self._client: Any = None
        self._connect_error: Optional[str] = None

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        if self._connect_error:
            raise VerificationUnavailable(self._connect_error)
        try:
            spec = importlib.util.spec_from_file_location("proxmox_inventory", PROXMOX_INVENTORY)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            self._client = module.connect(module.parse_proxmox_endpoint())
        except Exception as exc:  # noqa: BLE001 - report, do not crash the refresh.
            self._connect_error = f"Proxmox API connection failed: {exc}"
            raise VerificationUnavailable(self._connect_error) from exc
        return self._client

    def guest_keys(self, entry: HostEntry, key_types: List[str]) -> KeyMap:
        client = self._connect()
        keys: KeyMap = {}
        last_error = ""
        for key_type in key_types:
            path = HOST_KEY_FILES.get(key_type)
            if not path:
                continue
            try:
                response = (
                    client.nodes(entry.proxmox_node)
                    .qemu(entry.proxmox_vmid)
                    .agent("file-read")
                    .get(file=path)
                )
            except Exception as exc:  # noqa: BLE001 - permission or agent errors.
                last_error = str(exc)
                if "403" in last_error or "Permission check failed" in last_error:
                    raise VerificationUnavailable(
                        "the Proxmox API token lacks VM.GuestAgent.FileRead"
                    ) from exc
                continue
            content = response.get("content", "") if isinstance(response, dict) else ""
            keys.update(parse_guest_key_file(content))
        if not keys:
            raise VerificationUnavailable(
                f"guest agent file-read returned no host key ({last_error or 'empty response'})"
            )
        return keys


def parse_guest_key_file(content: str) -> KeyMap:
    """Parse a ssh_host_*_key.pub file body ("type key comment")."""
    keys: KeyMap = {}
    for line in content.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
            keys.setdefault(parts[0], parts[1])
    return keys


def refresh(
    entries: List[HostEntry],
    known_hosts: Path,
    scan_timeout: int,
    trust_changed: bool,
    verifier: Optional[ProxmoxVerifier],
) -> int:
    problems = 0
    for entry in entries:
        stored = stored_keys(entry.address, known_hosts)
        scan = scan_host(entry.address, scan_timeout)
        status = classify(stored, scan.keys)

        if status == "unreachable":
            print(f"unreachable during key scan: {entry.label} (known_hosts left unchanged)")
            continue
        if status == "known":
            print(f"known: {entry.address}")
            continue
        if status == "new":
            append_lines(known_hosts, scan.hashed_lines)
            print(f"added: {entry.address} ({len(scan.hashed_lines)} key(s))")
            continue

        verify: Optional[Callable[[], KeyMap]] = None
        if verifier is not None and entry.is_proxmox_vm:
            verify = lambda entry=entry, scan=scan: verifier.guest_keys(  # noqa: E731
                entry, sorted(scan.keys)
            )

        action, reason = decide(status, verify, scan.keys, trust_changed)
        if action == "replace":
            try:
                remove_host(entry.address, known_hosts)
            except RefreshError as exc:
                problems += 1
                print(f"Host key changed: {entry.label}: NOT updated - {exc}")
                continue
            append_lines(known_hosts, scan.hashed_lines)
            print(f"Host key changed: {entry.label}: {reason}; known_hosts updated.")
            continue

        problems += 1
        print(f"Host key changed: {entry.label}: NOT updated - {reason}.")
        for line in remediation_lines(entry, known_hosts, scan.keys):
            print(line)

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--known-hosts",
        default=os.getenv("KNOWN_HOSTS") or str(Path.home() / ".ssh" / "known_hosts"),
        help="known_hosts file to refresh (default: ~/.ssh/known_hosts)",
    )
    parser.add_argument(
        "--scan-timeout",
        type=int,
        default=int(os.getenv("KNOWN_HOSTS_SCAN_TIMEOUT", "10")),
        help="ssh-keyscan timeout per host in seconds",
    )
    parser.add_argument(
        "--trust-changed-keys",
        action="store_true",
        default=env_bool("KNOWN_HOSTS_TRUST_CHANGED_KEYS"),
        help="accept changed keys that cannot be verified out-of-band (unsafe)",
    )
    parser.add_argument(
        "--no-proxmox-verify",
        action="store_true",
        help="skip guest-agent verification of changed keys",
    )
    args = parser.parse_args()

    known_hosts = Path(args.known_hosts).expanduser()
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.touch(exist_ok=True)
    known_hosts.chmod(0o600)

    try:
        entries = load_inventory_entries(run_ansible_inventory())
    except RefreshError as exc:
        print(f"known_hosts refresh error: {exc}", file=sys.stderr)
        return 1

    if not entries:
        print("No hosts discovered.", file=sys.stderr)
        return 1

    verifier = None if args.no_proxmox_verify else ProxmoxVerifier()
    problems = refresh(entries, known_hosts, args.scan_timeout, args.trust_changed_keys, verifier)
    print(f"Known hosts updated: {known_hosts}")
    if problems:
        print(
            f"{problems} host(s) have a changed SSH host key that was not accepted; "
            "they will be skipped by the SSH preflight.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
