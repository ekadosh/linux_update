#!/usr/bin/env python3
"""Checks for the run-email summary parsing in scripts/send_run_email.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def load_email_module():
    spec = importlib.util.spec_from_file_location(
        "send_run_email", ROOT_DIR / "scripts" / "send_run_email.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREFLIGHT_FAILURE_LOG = """Checking SSH connectivity with default SSH options
SSH preflight [default]
vm1 | UNREACHABLE! => {
    "changed": false,
    "msg": "Task failed: Failed to connect to the host via ssh: ansible@10.0.0.9: Permission denied (publickey).",
    "unreachable": true
}
vm2 | CHANGED | rc=0 >>
Shared connection to 10.0.0.10 closed.

Skipping unreachable host: vm1 (SSH preflight [default] could not connect)
Continuing with 1 reachable host(s): --limit all:!vm1
Running Ansible playbook

TASK [Safely upgrade packages] *************************************************
changed: [vm2]

PLAY RECAP *********************************************************************
vm2                        : ok=9    changed=3    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
"""


HOST_KEY_CHANGE_LOG = """Refreshing SSH known_hosts from current inventory
known: 10.0.0.10
Host key changed: vm1 (10.0.0.9): NOT updated - could not be verified: the Proxmox API token lacks VM.GuestAgent.FileRead; left unchanged.
    new ssh-ed25519 fingerprint: SHA256:abc
Host key changed: vm3 (10.0.0.11): verified through the Proxmox guest agent; known_hosts updated.
unreachable during key scan: vm4 (10.0.0.12) (known_hosts left unchanged)
Known hosts updated: /home/runner/.ssh/known_hosts
Checking SSH connectivity with default SSH options
SSH preflight [default]
vm1 | UNREACHABLE! => {
    "changed": false,
    "msg": "Task failed: Failed to connect to the host via ssh: @@@@@@@@\\r\\n@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\\r\\nHost key verification failed.",
    "unreachable": true
}
vm2 | CHANGED | rc=0 >>
Shared connection to 10.0.0.10 closed.

Skipping unreachable host: vm1 (SSH preflight [default] could not connect)
"""


def check_host_key_change_explained(module) -> None:
    details = module.build_preflight_failures(HOST_KEY_CHANGE_LOG)
    assert details[0] == "vm1 - SSH preflight", details
    assert details[1].startswith("  SSH host key changed and was not accepted:"), details
    assert "VM.GuestAgent.FileRead" in details[1], details
    assert "@@@@" not in details[1], details

    # Without a refresh report, the raw ssh banner is still condensed.
    log = "\n".join(
        line for line in HOST_KEY_CHANGE_LOG.splitlines() if not line.startswith("Host key changed")
    )
    details = module.build_preflight_failures(log)
    assert "SSH host key changed (REMOTE HOST IDENTIFICATION HAS CHANGED)" in details[1], details


def check_host_key_warnings(module) -> None:
    warnings = module.build_warnings(HOST_KEY_CHANGE_LOG)
    assert any("vm3 changed; verified through the Proxmox guest agent" in w for w in warnings), warnings
    assert any("vm4 did not answer the known_hosts key scan" in w for w in warnings), warnings
    assert not any("vm1" in w for w in warnings), warnings


def check_skipped_hosts(module) -> None:
    skipped = module.parse_skipped_hosts(PREFLIGHT_FAILURE_LOG)
    assert skipped == ["vm1"], skipped


def check_summary_reports_skipped_and_updated_hosts(module) -> None:
    skipped = module.parse_skipped_hosts(PREFLIGHT_FAILURE_LOG)
    summary = module.build_summary(PREFLIGHT_FAILURE_LOG, "update", skipped)
    assert summary[0] == (
        "vm1 was skipped: SSH preflight could not connect, so no update was attempted."
    ), summary
    assert "vm2 was updated." in summary, summary


def check_preflight_failure_details(module) -> None:
    details = module.build_preflight_failures(PREFLIGHT_FAILURE_LOG)
    assert details[0] == "vm1 - SSH preflight", details
    assert "Permission denied (publickey)" in details[1], details


def check_preflight_failure_reported_without_a_recap(module) -> None:
    """A run that dies in preflight still has to explain itself."""
    log = PREFLIGHT_FAILURE_LOG.split("Skipping unreachable host", 1)[0]
    assert module.parse_skipped_hosts(log) == []
    assert module.build_preflight_failures(log), "preflight failure was not reported"


def main() -> int:
    module = load_email_module()
    check_skipped_hosts(module)
    check_summary_reports_skipped_and_updated_hosts(module)
    check_preflight_failure_details(module)
    check_preflight_failure_reported_without_a_recap(module)
    check_host_key_change_explained(module)
    check_host_key_warnings(module)
    print("email summary checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
