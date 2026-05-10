#!/usr/bin/env python3
"""Send an email summary for an Ansible update run."""

from __future__ import annotations

import argparse
import os
import platform
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional, Tuple


MAX_LOG_BYTES = 80_000
TASK_RE = re.compile(r"^TASK \[(?P<name>.+?)\]")
TASK_RESULT_RE = re.compile(r"^(?P<status>ok|changed|fatal|skipping|skipped|unreachable): \[(?P<host>[^\]]+)\]")
RECAP_RE = re.compile(
    r"^(?P<host>\S+)\s+:\s+"
    r"ok=(?P<ok>\d+)\s+"
    r"changed=(?P<changed>\d+)\s+"
    r"unreachable=(?P<unreachable>\d+)\s+"
    r"failed=(?P<failed>\d+)\s+"
    r"skipped=(?P<skipped>\d+)\s+"
    r"rescued=(?P<rescued>\d+)\s+"
    r"ignored=(?P<ignored>\d+)"
)


def tail_text(path: Path, max_bytes: int = MAX_LOG_BYTES) -> str:
    data = path.read_bytes()
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")
    return (
        f"[Log truncated to last {max_bytes} bytes]\n\n"
        + data[-max_bytes:].decode("utf-8", errors="replace")
    )


def full_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def result_host(raw_host: str) -> str:
    return raw_host.split(" -> ", 1)[0]


def parse_ansible_log(log_text: str) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, int]]]:
    task_results: Dict[str, Dict[str, str]] = {}
    recap: Dict[str, Dict[str, int]] = {}
    current_task = ""

    for line in log_text.splitlines():
        task_match = TASK_RE.match(line)
        if task_match:
            current_task = task_match.group("name")
            task_results.setdefault(current_task, {})
            continue

        recap_match = RECAP_RE.match(line)
        if recap_match:
            values = recap_match.groupdict()
            host = values.pop("host")
            recap[host] = {key: int(value) for key, value in values.items()}
            continue

        if not current_task:
            continue

        result_match = TASK_RESULT_RE.match(line)
        if result_match:
            status = result_match.group("status")
            if status == "fatal":
                status = "failed"
            task_results[current_task][result_host(result_match.group("host"))] = status

    return task_results, recap


def host_update_phrase(host: str, package_status: Optional[str], failed: bool, unreachable: bool, mode: str) -> str:
    if unreachable:
        return f"{host} was unreachable; no update was completed."

    if package_status == "changed":
        phrase = "would be updated" if mode == "dry-run" else "was updated"
    elif package_status == "ok":
        phrase = "would already be up to date" if mode == "dry-run" else "was already up to date"
    elif package_status in {"skipping", "skipped"}:
        phrase = "was skipped before package upgrades"
    elif failed:
        return f"{host} failed before package update status could be confirmed."
    else:
        phrase = "ran, but package update status was not found in the log"

    if failed:
        phrase += ", but the run failed during post-update verification"

    return f"{host} {phrase}."


def build_summary(log_text: str, mode: str) -> List[str]:
    task_results, recap = parse_ansible_log(log_text)
    package_results = task_results.get("Safely upgrade packages", {})
    reboot_results = task_results.get("Reboot after updates when Ubuntu requires it", {})
    suppressed_reboots = task_results.get("Report reboot suppressed by host configuration", {})
    rollback_results = task_results.get("Roll back Proxmox VM to pre-update snapshot", {})

    hosts = list(recap)
    for host in package_results:
        if host not in recap:
            hosts.append(host)

    if not hosts:
        return ["No per-host update summary could be parsed from the Ansible log."]

    summary = []
    for host in hosts:
        counts = recap.get(host, {})
        failed = counts.get("failed", 0) > 0
        unreachable = counts.get("unreachable", 0) > 0
        line = host_update_phrase(host, package_results.get(host), failed, unreachable, mode)

        if reboot_results.get(host) == "changed":
            line += " It was rebooted because the OS required it."
        elif suppressed_reboots.get(host) == "ok":
            line += " Reboot was required but suppressed by host configuration."

        if rollback_results.get(host) == "changed":
            line += " It was rolled back to the pre-update snapshot."

        summary.append(line)

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Ansible update result email")
    parser.add_argument("--status", type=int, required=True, help="Ansible exit status")
    parser.add_argument("--log-file", required=True, help="Path to the run log")
    parser.add_argument("--started-at", required=True, help="Run start timestamp")
    parser.add_argument("--finished-at", required=True, help="Run finish timestamp")
    parser.add_argument(
        "--mode",
        choices=["update", "dry-run"],
        default="update",
        help="Whether this was a real update run or an Ansible check-mode dry run",
    )
    args = parser.parse_args()

    recipient = os.getenv("ALERT_EMAIL_TO")
    if not recipient:
        print("Email alert skipped: ALERT_EMAIL_TO is not set")
        return 0

    smtp_host = os.getenv("SMTP_RELAY_HOST", "smtp.domain.com")
    smtp_port = int(os.getenv("SMTP_RELAY_PORT", "587"))
    sender = os.getenv("ALERT_EMAIL_FROM", f"ansible-updates@{platform.node() or 'localhost'}")
    starttls = os.getenv("SMTP_RELAY_STARTTLS", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    result = "SUCCESS" if args.status == 0 else "FAILED"
    mode_label = "DRY RUN" if args.mode == "dry-run" else "UPDATE"
    log_file = Path(args.log_file)
    log_tail = tail_text(log_file)
    summary = build_summary(full_text(log_file), args.mode)

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"[{result}] [{mode_label}] Ansible updates on {platform.node()}"
    message.set_content(
        "\n".join(
            [
                f"Result: {result}",
                f"Run mode: {mode_label}",
                f"Exit status: {args.status}",
                f"Host: {platform.node()}",
                f"Started: {args.started_at}",
                f"Finished: {args.finished_at}",
                f"Log file: {log_file}",
                "",
                "Run log:",
                "--------",
                log_tail,
                "",
                "Summary:",
                "--------",
                *summary,
            ]
        )
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            if starttls:
                smtp.starttls()
            smtp.send_message(message)
    except OSError as exc:
        print(f"Email alert failed: unable to connect to {smtp_host}:{smtp_port}: {exc}")
        return 1
    except smtplib.SMTPException as exc:
        print(f"Email alert failed: SMTP error from {smtp_host}:{smtp_port}: {exc}")
        return 1

    print(f"Email alert sent to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
