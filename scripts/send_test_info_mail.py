#!/usr/bin/env python3
"""
Send the current info mail template to the configured test recipient.

Usage:
    python scripts/send_test_info_mail.py --dry-run
    python scripts/send_test_info_mail.py --confirm
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
TEMPLATE_PATH = ROOT_DIR / "infomail.txt"


def _load_env() -> None:
    load_dotenv(ENV_PATH)


def _load_portal_module():
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    import portal

    return portal


def _validate_template() -> None:
    try:
        content = TEMPLATE_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"infomail.txt is not readable: {exc}") from exc
    if not content:
        raise ValueError("infomail.txt is empty.")


def run(dry_run: bool, confirm: bool) -> int:
    _load_env()
    test_email = os.environ.get("TEST_INFO_EMAIL", "").strip().lower()
    mode = "DRY RUN" if dry_run else "LIVE"

    if not dry_run and not confirm:
        print("ERROR: Pass --confirm to send, or --dry-run to preview.", file=sys.stderr)
        return 1
    if not test_email:
        print("ERROR: TEST_INFO_EMAIL is not set.", file=sys.stderr)
        return 1

    try:
        _validate_template()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"=== Test Info Mail ({mode}) ===")
    print(f"Recipient: {test_email}")
    print(f"Template: {TEMPLATE_PATH.name}")

    if dry_run:
        print("Would send 1 email.")
        return 0

    try:
        portal = _load_portal_module()
        sent_count = portal.send_inform_email([test_email])
    except Exception as exc:
        print(f"ERROR sending test info mail: {exc}", file=sys.stderr)
        return 1

    if sent_count != 1:
        print("ERROR: Expected exactly 1 sent email.", file=sys.stderr)
        return 1

    print("Sent 1 email.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Send the test info mail.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview without sending.")
    group.add_argument("--confirm", action="store_true", help="Send the email.")
    args = parser.parse_args()
    return run(dry_run=args.dry_run, confirm=args.confirm)


if __name__ == "__main__":
    sys.exit(main())
