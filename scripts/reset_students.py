#!/usr/bin/env python3
"""
Semester reset script – deletes all student keys and users from LiteLLM.
LiteLLM is the single source of truth; portal-db is cleaned up afterwards.

Usage:
    python scripts/reset_students.py --dry-run
    python scripts/reset_students.py --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000").rstrip("/")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

BATCH_SIZE = 50


async def litellm_list_student_users(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all users with role-prefix 'student:' from LiteLLM."""
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    resp = await client.get(f"{LITELLM_BASE_URL}/user/list", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    users = data if isinstance(data, list) else data.get("users", [])
    return [u for u in users if str(u.get("user_id", "")).startswith("student:")]


async def litellm_get_user_keys(client: httpx.AsyncClient, user_id: str) -> list[str]:
    """Return all key hashes for a given user."""
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    resp = await client.get(
        f"{LITELLM_BASE_URL}/user/info",
        params={"user_id": user_id},
        headers=headers,
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    user_info = data.get("user_info") or data
    keys = user_info.get("keys", [])
    return [k["token"] if isinstance(k, dict) else k for k in keys]


async def delete_keys(client: httpx.AsyncClient, keys: list[str], dry_run: bool) -> list[str]:
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    errors: list[str] = []
    for i in range(0, len(keys), BATCH_SIZE):
        batch = keys[i : i + BATCH_SIZE]
        if dry_run:
            print(f"  [dry-run] Would delete {len(batch)} key(s)")
            continue
        try:
            resp = await client.post(
                f"{LITELLM_BASE_URL}/key/delete",
                json={"keys": batch},
                headers=headers,
            )
            resp.raise_for_status()
            print(f"  Deleted {len(batch)} key(s).")
        except Exception as exc:
            print(f"  ERROR deleting keys: {exc}", file=sys.stderr)
            errors.extend(batch)
    return errors


async def delete_users(client: httpx.AsyncClient, user_ids: list[str], dry_run: bool) -> list[str]:
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    errors: list[str] = []
    for i in range(0, len(user_ids), BATCH_SIZE):
        batch = user_ids[i : i + BATCH_SIZE]
        if dry_run:
            print(f"  [dry-run] Would delete {len(batch)} user(s)")
            continue
        try:
            resp = await client.post(
                f"{LITELLM_BASE_URL}/user/delete",
                json={"user_ids": batch},
                headers=headers,
            )
            resp.raise_for_status()
            print(f"  Deleted {len(batch)} user(s).")
        except Exception as exc:
            print(f"  ERROR deleting users: {exc}", file=sys.stderr)
            errors.extend(batch)
    return errors


async def main(dry_run: bool, confirm: bool) -> int:
    if not dry_run and not confirm:
        print("ERROR: Pass --confirm to perform live deletion, or --dry-run to preview.", file=sys.stderr)
        return 1
    if not LITELLM_MASTER_KEY:
        print("ERROR: LITELLM_MASTER_KEY is not set.", file=sys.stderr)
        return 1
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        return 1

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"=== Student Reset ({mode}) ===")
    print(f"LiteLLM: {LITELLM_BASE_URL}")
    print()

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Step 1: Fetch all student users from LiteLLM
        print("Step 1: Fetching student users from LiteLLM ...")
        try:
            students = await litellm_list_student_users(client)
        except Exception as exc:
            print(f"ERROR fetching users: {exc}", file=sys.stderr)
            return 1

        if not students:
            print("No student users found in LiteLLM. Nothing to do.")
            return 0

        user_ids = [u["user_id"] for u in students]
        print(f"Found {len(user_ids)} student user(s):")
        for uid in user_ids[:10]:
            print(f"  - {uid}")
        if len(user_ids) > 10:
            print(f"  ... and {len(user_ids) - 10} more")
        print()

        # Step 2: Collect all keys for these users
        print("Step 2: Collecting API keys ...")
        all_keys: list[str] = []
        for user_id in user_ids:
            keys = await litellm_get_user_keys(client, user_id)
            all_keys.extend(keys)
        print(f"  Found {len(all_keys)} key(s) total.")

        # Step 3: Delete keys from LiteLLM
        print(f"Step 3: Deleting {len(all_keys)} key(s) from LiteLLM ...")
        has_errors = False
        key_errors = await delete_keys(client, all_keys, dry_run)
        if key_errors:
            print(f"  WARNING: {len(key_errors)} key(s) could not be deleted.", file=sys.stderr)
            has_errors = True

        # Step 4: Delete users from LiteLLM
        print(f"Step 4: Deleting {len(user_ids)} user(s) from LiteLLM ...")
        user_errors = await delete_users(client, user_ids, dry_run)
        if user_errors:
            print(f"  WARNING: {len(user_errors)} user(s) could not be deleted.", file=sys.stderr)
            has_errors = True

    # Step 5: Clean portal-db
    print("Step 5: Cleaning portal database ...")
    if dry_run:
        print("  [dry-run] Would delete student rows from portal_users and portal_verification_codes.")
    else:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            r1 = await conn.execute("DELETE FROM portal_users WHERE role = 'student'")
            r2 = await conn.execute("DELETE FROM portal_verification_codes WHERE role = 'student'")
            print(f"  Deleted {r1.split()[-1]} portal_users row(s), {r2.split()[-1]} portal_verification_codes row(s).")
        finally:
            await conn.close()

    print()
    if has_errors:
        print("Reset completed WITH ERRORS. Review output above.", file=sys.stderr)
        return 2
    print("Reset completed successfully.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Semester reset: delete all student accounts.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview without modifying anything.")
    group.add_argument("--confirm", action="store_true", help="Perform live deletion.")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(dry_run=args.dry_run, confirm=args.confirm)))
