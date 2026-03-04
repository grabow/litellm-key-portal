#!/usr/bin/env python3
"""
Send the current roundmail template to all registered portal users.

Usage:
    python scripts/send_info_mail.py --dry-run
    python scripts/send_info_mail.py --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
TEMPLATE_PATH = ROOT_DIR / "rundmail.txt"


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
        raise RuntimeError(f"rundmail.txt nicht lesbar: {exc}") from exc
    if not content:
        raise ValueError("rundmail.txt ist leer.")


async def _get_recipients(database_url: str) -> list[str]:
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(
            "SELECT DISTINCT email FROM portal_users WHERE email <> '' ORDER BY email"
        )
    finally:
        await conn.close()
    return [row["email"] for row in rows]


async def run(dry_run: bool, confirm: bool) -> int:
    _load_env()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    mode = "DRY RUN" if dry_run else "LIVE"

    if not dry_run and not confirm:
        print("ERROR: Pass --confirm to send, or --dry-run to preview.", file=sys.stderr)
        return 1
    if not database_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        return 1

    try:
        _validate_template()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"=== Info Mail ({mode}) ===")

    try:
        recipients = await _get_recipients(database_url)
    except Exception as exc:
        print(f"ERROR loading recipients: {exc}", file=sys.stderr)
        return 1

    if not recipients:
        print("No recipients found. Nothing to do.")
        return 0

    print(f"Found {len(recipients)} recipient(s).")
    for recipient in recipients[:10]:
        print(f"  - {recipient}")
    if len(recipients) > 10:
        print(f"  ... and {len(recipients) - 10} more")

    if dry_run:
        print(f"Would send {len(recipients)} email(s).")
        return 0

    try:
        portal = _load_portal_module()
        sent_count = await asyncio.to_thread(portal.send_inform_email, recipients)
    except Exception as exc:
        print(f"ERROR sending info mail: {exc}", file=sys.stderr)
        return 1

    if sent_count != len(recipients):
        print(
            f"ERROR: Expected {len(recipients)} sent email(s), got {sent_count}.",
            file=sys.stderr,
        )
        return 2

    print(f"Sent {sent_count} email(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Send the info mail to all participants.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview without sending.")
    group.add_argument("--confirm", action="store_true", help="Send the emails.")
    args = parser.parse_args()
    return asyncio.run(run(dry_run=args.dry_run, confirm=args.confirm))


if __name__ == "__main__":
    sys.exit(main())
