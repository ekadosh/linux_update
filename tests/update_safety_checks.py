#!/usr/bin/env python3
"""Exercise production Ansible retry/rescue rules with local, harmless commands.

Only the relevant tasks are retained, in production order. Package, SSH, and
Proxmox operations are replaced with recording stubs; no real hosts are used.
Run with the same Python environment as Ansible.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml


ROOT = Path(__file__).resolve().parent.parent


def stub(events_path: str, scenario: str, stage: str) -> int:
    path = Path(events_path)
    events = path.read_text().splitlines() if path.exists() else []
    with path.open("a") as output:
        output.write(stage + "\n")
    if stage == "clean":
        if scenario == "lock_exhausted" or (scenario == "lock_once" and stage not in events):
            print("E: Could not get lock /var/lib/apt/lists/lock", file=sys.stderr)
            return 100
        if scenario == "clean_error":
            print("E: Permission denied", file=sys.stderr)
            return 100
    if stage == "hold" and scenario == "hold_error":
        return 1
    if stage == "upgrade" and scenario in {"upgrade_error", "static_error", "rollback_error"}:
        return 1
    if stage == "health" and scenario == "health_error":
        return 1
    if stage == "rollback" and scenario == "rollback_error":
        print("403 Forbidden: Permission check failed (/vms/403, VM.PowerMgmt)", file=sys.stderr)
        return 1
    return 0


def recording_task(task: dict, stage: str, scenario: str, events: Path) -> dict:
    task = copy.deepcopy(task)
    for key in list(task):
        if key.startswith("ansible.builtin."):
            del task[key]
    task["ansible.builtin.command"] = {
        "argv": [sys.executable, str(Path(__file__).resolve()), "--stub", str(events), scenario, stage]
    }
    return task


def run_case(production: dict, defaults: dict, scenario: str) -> None:
    with tempfile.TemporaryDirectory(prefix="update-safety-") as directory:
        temp = Path(directory)
        events_path = temp / "events"
        update = copy.deepcopy(next(t for t in production["tasks"] if "block" in t))
        stages = {
            "Clean apt package archive cache before upgrade": "clean",
            "Hold packages that have not yet met the minimum age requirement": "hold",
            "Safely upgrade packages": "upgrade",
            "Verify apt package database health": "health",
        }
        retained = {"Fail early when apt archive filesystem is too full"}
        block = []
        for task in update["block"]:
            if task["name"] in stages:
                task = recording_task(task, stages[task["name"]], scenario, events_path)
                if scenario == "check_error" and task["name"] == "Safely upgrade packages":
                    # Simulate apt failing during check mode without executing a command.
                    del task["ansible.builtin.command"]
                    task["ansible.builtin.fail"] = {"msg": "simulated check-mode failure"}
                block.append(task)
            elif task["name"] in retained or "ansible.builtin.set_fact" in task and (
                "update_package_changes_started" in task["ansible.builtin.set_fact"]
            ):
                block.append(task)
        update["block"] = block
        for index, task in enumerate(update["rescue"]):
            if task["name"] == "Roll back Proxmox VM to pre-update snapshot":
                update["rescue"][index] = recording_task(task, "rollback", scenario, events_path)
            elif task["name"] == "Wait for SSH after rollback":
                update["rescue"][index] = recording_task(task, "wait", scenario, events_path)
            elif "ansible.builtin.meta" in task:
                update["rescue"][index] = {"ansible.builtin.debug": {"msg": "simulated SSH reset"}}
        update["always"] = [
            recording_task(task, "unhold", scenario, events_path) for task in update["always"]
        ]
        variables = {
            **defaults,
            "ansible_python_interpreter": sys.executable,
            "ansible_remote_tmp": str(temp / "remote-tmp"),
            "update_package_changes_started": True,
            "apt_retries": 2,
            "apt_retry_delay": 0,
            "apt_archive_free_mb": 0 if scenario == "disk_error" else 4096,
            "age_held_packages": ["test-package"],
            "proxmox_node": "test-node",
            "update_snapshot_name": "test-snapshot",
        }
        if scenario != "static_error":
            variables["proxmox_vmid"] = "403"
        # Include production initialization to ensure state is reset per run.
        tasks = [copy.deepcopy(t) for t in production["tasks"] if "block" not in t] + [update]
        playbook = temp / "playbook.yml"
        playbook.write_text(yaml.safe_dump([{
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "become": False, "vars": variables, "tasks": tasks,
        }], sort_keys=False))
        env = {
            **os.environ,
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_STDOUT_CALLBACK": "default",
            "ANSIBLE_INVENTORY_ENABLED": "host_list",
        }
        command = [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,", str(playbook)]
        if scenario == "check_error":
            command.append("--check")
        result = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, timeout=90)
        output = result.stdout + result.stderr
        events = events_path.read_text().splitlines() if events_path.exists() else []
        expected = {
            "lock_once": ["clean", "clean", "hold", "upgrade", "health", "unhold"],
            "lock_exhausted": ["clean"] * 3 + ["unhold"],
            "clean_error": ["clean", "unhold"],
            "disk_error": ["clean", "unhold"],
            "hold_error": ["clean", "hold", "unhold"],
            "upgrade_error": ["clean", "hold", "upgrade", "rollback", "wait", "unhold"],
            "health_error": ["clean", "hold", "upgrade", "health", "rollback", "wait", "unhold"],
            "static_error": ["clean", "hold", "upgrade", "unhold"],
            "rollback_error": ["clean", "hold", "upgrade", "rollback", "unhold"],
            "check_error": [],
        }[scenario]
        assert events == expected, f"{scenario}: {events} != {expected}\n{output}"
        assert result.returncode == (0 if scenario == "lock_once" else 2), output
        if scenario in {"lock_exhausted", "clean_error", "disk_error", "hold_error", "check_error"}:
            assert "No package upgrade or removal was attempted" in output, output
        if scenario == "rollback_error":
            assert "VM.PowerMgmt" in output, output
            assert "Rolled back Proxmox VM" not in output, output
        print(f"passed: {scenario}", flush=True)


def main() -> None:
    production = yaml.safe_load((ROOT / "playbooks/update_ubuntu.yml").read_text())[0]
    defaults = yaml.safe_load((ROOT / "group_vars/all.yml").read_text())
    for scenario in (
        "lock_once", "lock_exhausted", "clean_error", "disk_error", "hold_error",
        "upgrade_error", "health_error", "static_error", "rollback_error", "check_error",
    ):
        run_case(production, defaults, scenario)
    print("update safety checks passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--stub":
        sys.exit(stub(*sys.argv[2:]))
    main()
