#!/usr/bin/env python3
"""Send an email summary for an Ansible update run."""

from __future__ import annotations

import argparse
import os
import platform
import smtplib
from email.message import EmailMessage
from pathlib import Path


MAX_LOG_BYTES = 80_000


def tail_text(path: Path, max_bytes: int = MAX_LOG_BYTES) -> str:
    data = path.read_bytes()
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")
    return (
        f"[Log truncated to last {max_bytes} bytes]\n\n"
        + data[-max_bytes:].decode("utf-8", errors="replace")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Ansible update result email")
    parser.add_argument("--status", type=int, required=True, help="Ansible exit status")
    parser.add_argument("--log-file", required=True, help="Path to the run log")
    parser.add_argument("--started-at", required=True, help="Run start timestamp")
    parser.add_argument("--finished-at", required=True, help="Run finish timestamp")
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
    log_file = Path(args.log_file)

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"[{result}] Homelab Ansible updates on {platform.node()}"
    message.set_content(
        "\n".join(
            [
                f"Result: {result}",
                f"Exit status: {args.status}",
                f"Host: {platform.node()}",
                f"Started: {args.started_at}",
                f"Finished: {args.finished_at}",
                f"Log file: {log_file}",
                "",
                "Run log:",
                "--------",
                tail_text(log_file),
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
