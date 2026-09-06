#!/usr/bin/env python3
"""Send an email summary for an Ansible update run."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import platform
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional, Tuple


MAX_ATTACHMENT_BYTES = 5_000_000
GZIP_THRESHOLD_BYTES = 256_000
MAX_FAILURE_DETAIL_LINES = 12
TASK_RE = re.compile(r"^TASK \[(?P<name>.+?)\]")
TASK_RESULT_RE = re.compile(r"^(?P<status>ok|changed|fatal|skipping|skipped|unreachable): \[(?P<host>[^\]]+)\]")
FAILURE_RE = re.compile(r"^(?P<status>fatal|unreachable): \[(?P<host>[^\]]+)\][^=]*=> (?P<payload>\{.*\})\s*$")
INVENTORY_ERROR_RE = re.compile(r"^Inventory (?:error|warning): (?P<message>.+)$")
SKIPPED_HOST_RE = re.compile(r"^Skipping unreachable host: (?P<host>\S+)")
ADHOC_UNREACHABLE_RE = re.compile(r"^(?P<host>\S+) \| UNREACHABLE!")
ADHOC_MSG_RE = re.compile(r'^\s*"msg":\s*"(?P<message>.*?)",?\s*$')
DPKG_ERROR_RE = re.compile(
    r"^(dpkg: error|dpkg: dependency problems|dpkg: warning|E: |Errors were encountered|"
    r"Sub-process |.*Permission denied|.*No such file or directory)"
)
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


def parse_skipped_hosts(log_text: str) -> List[str]:
    """Hosts run_updates.sh dropped from the run because SSH preflight failed."""
    skipped: List[str] = []
    for line in log_text.splitlines():
        match = SKIPPED_HOST_RE.match(line.strip())
        if match:
            skipped.append(match.group("host"))
    return dedupe(skipped)


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


def build_summary(log_text: str, mode: str, skipped_hosts: List[str]) -> List[str]:
    task_results, recap = parse_ansible_log(log_text)
    package_results = task_results.get("Safely upgrade packages", {})
    reboot_results = task_results.get("Reboot after updates when Ubuntu requires it", {})
    suppressed_reboots = task_results.get("Report reboot suppressed by host configuration", {})
    rollback_results = task_results.get("Roll back Proxmox VM to pre-update snapshot", {})

    hosts = list(recap)
    for host in package_results:
        if host not in recap:
            hosts.append(host)

    summary = [
        f"{host} was skipped: SSH preflight could not connect, so no update was attempted."
        for host in skipped_hosts
    ]

    if not hosts:
        if summary:
            return summary
        return ["No per-host update summary could be parsed from the Ansible log."]

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


def dedupe(items: List[str]) -> List[str]:
    seen = set()
    unique = []
    for item in items:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def interesting_output_lines(payload: Dict[str, object]) -> List[str]:
    """Pick the lines an operator actually needs out of a failed module's output."""
    candidates: List[str] = []
    for key in ("stdout_lines", "stderr_lines"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(str(item) for item in value)

    picked = [line.strip() for line in candidates if DPKG_ERROR_RE.match(line.strip())]
    return dedupe(picked)


def build_failure_details(log_text: str) -> List[str]:
    """Summarize each failed task instead of pasting the whole run log."""
    details: List[str] = []
    current_task = ""

    for line in log_text.splitlines():
        task_match = TASK_RE.match(line)
        if task_match:
            current_task = task_match.group("name")
            continue

        failure_match = FAILURE_RE.match(line)
        if not failure_match:
            continue

        host = result_host(failure_match.group("host"))
        try:
            payload = json.loads(failure_match.group("payload"))
        except ValueError:
            payload = {}

        header = f"{host} - TASK [{current_task or 'unknown task'}]"
        attempts = payload.get("attempts")
        if isinstance(attempts, int) and attempts > 1:
            header += f" (failed after {attempts} attempts)"
        details.append(header)

        message = str(payload.get("msg", "")).strip()
        for message_line in message.splitlines()[:4]:
            if message_line.strip():
                details.append(f"  {message_line.strip()}")

        output_lines = interesting_output_lines(payload)
        for output_line in output_lines[:MAX_FAILURE_DETAIL_LINES]:
            details.append(f"  {output_line}")
        if len(output_lines) > MAX_FAILURE_DETAIL_LINES:
            details.append(
                f"  ... {len(output_lines) - MAX_FAILURE_DETAIL_LINES} more lines in the attached log"
            )

        details.append("")

    while details and details[-1] == "":
        details.pop()

    return details


def build_preflight_failures(log_text: str) -> List[str]:
    """Explain ad-hoc SSH preflight failures, which never produce a play recap."""
    reasons: Dict[str, str] = {}
    lines = log_text.splitlines()

    for index, line in enumerate(lines):
        unreachable_match = ADHOC_UNREACHABLE_RE.match(line)
        if not unreachable_match:
            continue

        host = result_host(unreachable_match.group("host"))
        for follow_up in lines[index + 1 : index + 8]:
            message_match = ADHOC_MSG_RE.match(follow_up)
            if message_match:
                reasons.setdefault(host, message_match.group("message").strip())
                break
        else:
            reasons.setdefault(host, "SSH connection failed during preflight")

    details: List[str] = []
    for host, reason in reasons.items():
        details.append(f"{host} - SSH preflight")
        details.append(f"  {reason}")

    return details


def build_warnings(log_text: str) -> List[str]:
    """Collect inventory and Ansible warnings once, not once per inventory parse."""
    warnings: List[str] = []
    for line in log_text.splitlines():
        inventory_match = INVENTORY_ERROR_RE.match(line.strip())
        if inventory_match:
            warnings.append(f"Inventory: {inventory_match.group('message')}")
    return dedupe(warnings)


def format_duration(started_at: str, finished_at: str) -> str:
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(finished_at)
    except ValueError:
        return ""

    seconds = int((finished - started).total_seconds())
    if seconds < 0:
        return ""
    return f"{seconds // 60}m{seconds % 60:02d}s"


def prepare_log_attachment(log_file: Path) -> Tuple[Dict[str, object], str]:
    """Return add_attachment kwargs for the run log plus the note that describes it."""
    data = log_file.read_bytes()
    truncated = len(data) > MAX_ATTACHMENT_BYTES
    if truncated:
        data = data[-MAX_ATTACHMENT_BYTES:]

    if len(data) > GZIP_THRESHOLD_BYTES:
        payload = gzip.compress(data)
        kwargs: Dict[str, object] = {
            "maintype": "application",
            "subtype": "gzip",
            "filename": f"{log_file.name}.gz",
        }
    else:
        payload = data
        kwargs = {
            "maintype": "text",
            "subtype": "plain",
            "filename": log_file.name,
        }

    note = f"Full log attached as {kwargs['filename']} ({max(len(payload) // 1024, 1)} KB)"
    if truncated:
        note += ", truncated to the last portion of the run"

    return {"data": payload, **kwargs}, note + "."


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

    mode_label = "DRY RUN" if args.mode == "dry-run" else "UPDATE"
    log_file = Path(args.log_file)
    log_text = full_text(log_file)
    runner = platform.node()

    skipped_hosts = parse_skipped_hosts(log_text)
    if args.status != 0:
        result = "FAILED"
    elif skipped_hosts:
        result = "PARTIAL"
    else:
        result = "SUCCESS"

    duration = format_duration(args.started_at, args.finished_at)
    run_line = f"Started {args.started_at}, finished {args.finished_at}"
    if duration:
        run_line += f" ({duration})"

    body: List[str] = [
        f"{result}: {mode_label} run from {runner}",
        "",
        *build_summary(log_text, args.mode, skipped_hosts),
        "",
        run_line,
        f"Exit status: {args.status}",
        f"Log file: {log_file}",
    ]

    failure_details = build_failure_details(log_text) + build_preflight_failures(log_text)
    if failure_details:
        body += ["", "Failures:", "---------", *failure_details]

    warnings = build_warnings(log_text)
    if warnings:
        body += ["", "Warnings:", "---------", *warnings]

    attachment, attachment_note = prepare_log_attachment(log_file)
    body += ["", attachment_note]

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"[{result}] [{mode_label}] Ansible updates on {runner}"
    message.set_content("\n".join(body) + "\n")
    message.add_attachment(
        attachment.pop("data"),
        **attachment,
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
