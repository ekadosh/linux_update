#!/usr/bin/env python3
"""Checks for the host-key comparison rules in scripts/refresh_known_hosts.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def load_module():
    spec = importlib.util.spec_from_file_location(
        "refresh_known_hosts", ROOT_DIR / "scripts" / "refresh_known_hosts.py"
    )
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules, so register first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


KEYSCAN_OUTPUT = """# 10.0.0.9:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13
|1|abc=|def= ssh-rsa AAAAB3RSA
# 10.0.0.9:22 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13
|1|ghi=|jkl= ssh-ed25519 AAAAC3NEW
"""

KEYGEN_F_OUTPUT = """# Host 10.0.0.9 found: line 12
|1|abc=|def= ssh-rsa AAAAB3RSA
# Host 10.0.0.9 found: line 40
|1|mno=|pqr= ssh-ed25519 AAAAC3OLD
"""


def check_parse_key_lines(module) -> None:
    live = module.parse_key_lines(KEYSCAN_OUTPUT)
    assert live == {"ssh-rsa": "AAAAB3RSA", "ssh-ed25519": "AAAAC3NEW"}, live
    stored = module.parse_key_lines(KEYGEN_F_OUTPUT)
    assert stored == {"ssh-rsa": "AAAAB3RSA", "ssh-ed25519": "AAAAC3OLD"}, stored
    plain = module.parse_key_lines("vm1,10.0.0.9 ssh-ed25519 AAAAC3X comment\n")
    assert plain == {"ssh-ed25519": "AAAAC3X"}, plain


def check_classify(module) -> None:
    assert module.classify({}, {}) == "unreachable"
    assert module.classify({"ssh-ed25519": "old"}, {}) == "unreachable"
    assert module.classify({}, {"ssh-ed25519": "new"}) == "new"
    assert module.classify({"ssh-ed25519": "same"}, {"ssh-ed25519": "same"}) == "known"
    assert module.classify({"ssh-ed25519": "old"}, {"ssh-ed25519": "new"}) == "changed"
    # A matching ed25519 key is enough even when the host stopped offering RSA.
    assert (
        module.classify({"ssh-ed25519": "same", "ssh-rsa": "r"}, {"ssh-ed25519": "same"})
        == "known"
    )
    # Nothing comparable is treated as a change so it gets verified.
    assert module.classify({"ssh-rsa": "r"}, {"ssh-ed25519": "e"}) == "changed"


def check_decide_verified_match_replaces(module) -> None:
    live = {"ssh-ed25519": "new"}
    action, reason = module.decide("changed", lambda: {"ssh-ed25519": "new"}, live, False)
    assert action == "replace", (action, reason)
    assert "verified" in reason, reason


def check_decide_verified_mismatch_keeps(module) -> None:
    live = {"ssh-ed25519": "attacker"}
    action, reason = module.decide("changed", lambda: {"ssh-ed25519": "new"}, live, False)
    assert action == "keep", (action, reason)
    assert "man-in-the-middle" in reason, reason
    # Even the unsafe opt-in must not accept a key the guest itself disowns.
    action, _ = module.decide("changed", lambda: {"ssh-ed25519": "new"}, live, True)
    assert action == "keep"


def check_decide_unverifiable(module) -> None:
    def unavailable():
        raise module.VerificationUnavailable("the Proxmox API token lacks VM.GuestAgent.FileRead")

    live = {"ssh-ed25519": "new"}
    action, reason = module.decide("changed", unavailable, live, False)
    assert action == "keep", (action, reason)
    assert "VM.GuestAgent.FileRead" in reason, reason

    action, reason = module.decide("changed", unavailable, live, True)
    assert action == "replace", (action, reason)
    assert "UNVERIFIED" in reason, reason

    action, reason = module.decide("changed", None, live, False)
    assert action == "keep", (action, reason)
    action, reason = module.decide("changed", None, live, True)
    assert action == "replace", (action, reason)


def check_decide_ignores_unchanged(module) -> None:
    assert module.decide("known", None, {"ssh-ed25519": "x"}, True) == ("keep", "")


def check_inventory_entries(module) -> None:
    inventory = {
        "_meta": {
            "hostvars": {
                "vm1": {"ansible_host": "10.0.0.9", "proxmox_node": "pve", "proxmox_vmid": 101},
                "box": {"ansible_host": "box.example.lan"},
            }
        }
    }
    entries = module.load_inventory_entries(inventory)
    assert [entry.name for entry in entries] == ["box", "vm1"], entries
    vm1 = entries[1]
    assert vm1.is_proxmox_vm and vm1.proxmox_vmid == "101" and vm1.proxmox_node == "pve"
    assert not entries[0].is_proxmox_vm
    assert vm1.label == "vm1 (10.0.0.9)"


def check_parse_guest_key_file(module) -> None:
    keys = module.parse_guest_key_file("ssh-ed25519 AAAAC3NEW root@vm1\n")
    assert keys == {"ssh-ed25519": "AAAAC3NEW"}, keys
    assert module.parse_guest_key_file("") == {}


def main() -> int:
    module = load_module()
    check_parse_key_lines(module)
    check_classify(module)
    check_decide_verified_match_replaces(module)
    check_decide_verified_mismatch_keeps(module)
    check_decide_unverifiable(module)
    check_decide_ignores_unchanged(module)
    check_inventory_entries(module)
    check_parse_guest_key_file(module)
    print("known_hosts checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
