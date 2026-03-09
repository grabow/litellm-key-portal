#!/usr/bin/env python3
"""
Seed deterministic student test users for the admin overview.

Usage:
    python scripts/seed_dummy_students.py --dry-run
    python scripts/seed_dummy_students.py --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from httpx import HTTPStatusError

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"


def _load_env() -> None:
    load_dotenv(ENV_PATH)


def _load_portal_module():
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    import portal

    return portal


def _dummy_email(index: int, domain: str) -> str:
    return f"dummy.student{index:02d}@{domain}"


async def run_seed(dry_run: bool, confirm: bool, count: int = 20) -> int:
    if not dry_run and not confirm:
        print("ERROR: Pass --confirm to perform live seeding, or --dry-run to preview.", file=sys.stderr)
        return 1

    _load_env()
    portal = _load_portal_module()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    allowed_domain = os.environ.get("ALLOWED_DOMAIN", "").strip().lower()
    budget = portal.ROLE_BUDGETS["student"]

    if not database_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        return 1
    if not allowed_domain:
        print("ERROR: ALLOWED_DOMAIN is not set.", file=sys.stderr)
        return 1

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"=== Dummy Student Seed ({mode}) ===")
    print(f"Count: {count}")
    print(f"Domain: {allowed_domain}")
    print(f"Budget: {budget:.2f} EUR")
    print()

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(portal.SCHEMA_SQL)

        for index in range(1, count + 1):
            email = _dummy_email(index, allowed_domain)
            user_id = f"student:{email}"
            print(f"[{index:02d}/{count:02d}] {email}")

            if dry_run:
                print("  [dry-run] Would create or update LiteLLM user, rotate key, and upsert portal rows.")
                continue

            try:
                await portal.litellm_create_user(user_id, budget)
                print("  Created LiteLLM user.")
            except HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
                await portal.litellm_update_budget(user_id, budget)
                print("  LiteLLM user already exists; updated budget.")

            existing_tokens = await portal.litellm_get_user_key_tokens(user_id)
            if existing_tokens:
                await portal.litellm_delete_keys(existing_tokens)
                print(f"  Rotated {len(existing_tokens)} existing key(s).")

            await portal.litellm_generate_key(user_id, budget)
            print("  Generated fresh API key.")

            created_at = datetime.now(timezone.utc) - timedelta(minutes=index)
            await conn.execute(
                """
                INSERT INTO portal_users (email, role, created_at)
                VALUES ($1, 'student', $2)
                ON CONFLICT (email, role) DO NOTHING
                """,
                email,
                created_at,
            )

            if index % 4 == 0:
                expires_at = datetime.now(timezone.utc) + timedelta(minutes=15 - (index % 5))
                await conn.execute(
                    """
                    INSERT INTO portal_verification_codes (email, role, hashed_code, expires_at, used, created_at)
                    VALUES ($1, 'student', $2, $3, FALSE, NOW())
                    """,
                    email,
                    portal.hash_code(f"{index:06d}"),
                    expires_at,
                )
                print("  Added active verification code.")
    finally:
        await conn.close()

    print()
    print(f"Seeded {count} dummy student user(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed dummy student users for the admin overview.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview without modifying anything.")
    group.add_argument("--confirm", action="store_true", help="Perform live seeding.")
    parser.add_argument("--count", type=int, default=20, help="Number of dummy student users to seed.")
    args = parser.parse_args()
    if args.count <= 0:
        print("ERROR: --count must be greater than 0.", file=sys.stderr)
        return 1
    return asyncio.run(run_seed(dry_run=args.dry_run, confirm=args.confirm, count=args.count))


if __name__ == "__main__":
    sys.exit(main())
