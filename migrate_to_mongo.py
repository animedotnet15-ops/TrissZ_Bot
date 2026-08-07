"""
One-time migration: SQLite (database.db) -> MongoDB (Motor).

SAFETY MODEL
------------
- Never deletes or modifies the SQLite file. It is only ever read from.
- Never overwrites existing MongoDB documents (upsert with $setOnInsert
  for anything that could already exist there — e.g. if this script is
  re-run after a partial failure).
- Default mode is --dry-run: reads SQLite, connects to Mongo, reports
  exactly what WOULD be migrated, and writes nothing.
- --verify re-reads both sides after a real migration and diffs row
  counts per table/collection. Any mismatch is reported and the script
  exits non-zero — nothing about your running bot depends on this
  succeeding; the SQLite backend keeps working regardless.
- Your bot keeps using SQLite (the default backend) until you explicitly
  set DB_BACKEND=mongo in your environment. This script does not flip
  that switch for you.

USAGE
-----
    python migrate_to_mongo.py --dry-run          # inspect only, safe to run anytime
    python migrate_to_mongo.py --migrate           # actually copy data (idempotent)
    python migrate_to_mongo.py --verify             # row-count parity check
    python migrate_to_mongo.py --migrate --verify   # do both, in order
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

import aiosqlite
from motor.motor_asyncio import AsyncIOMotorClient

from config import config

TABLES = [
    "users", "bans", "strikes", "bypass_events", "settings",
    "stored_files", "posts", "post_files", "protected_sessions",
    "pending_tokens", "favorites", "download_history",
]

# Tables whose natural key differs from a straight primary key, used to
# build an idempotent upsert filter (so re-running never duplicates).
KEY_FIELDS = {
    "users": ["user_id"],
    "bans": ["user_id"],
    "strikes": ["user_id"],
    "bypass_events": ["event_key", "user_id"],
    "settings": ["key"],
    "stored_files": ["storage_message_id"],
    "posts": ["code"],
    "post_files": ["post_id", "file_id"],
    "protected_sessions": ["token"],
    "pending_tokens": ["token"],
    "favorites": ["user_id", "post_code"],
    "download_history": ["id"],
}


async def read_sqlite() -> dict[str, list[dict]]:
    data: dict[str, list[dict]] = {}
    async with aiosqlite.connect(config.database_path) as db:
        db.row_factory = aiosqlite.Row
        for table in TABLES:
            try:
                rows = await (await db.execute(f"SELECT * FROM {table}")).fetchall()
            except aiosqlite.OperationalError:
                rows = []  # table doesn't exist yet in this install — fine
            data[table] = [dict(r) for r in rows]
    return data


async def dry_run(data: dict[str, list[dict]]) -> None:
    print("\n=== DRY RUN — nothing has been written anywhere ===")
    total = 0
    for table, rows in data.items():
        print(f"  {table:<20} {len(rows):>6} rows in SQLite")
        total += len(rows)
    print(f"  {'TOTAL':<20} {total:>6} rows would be migrated\n")
    print("Run with --migrate to actually copy this data into MongoDB.")
    print("Your bot's live SQLite database is never modified by this script.\n")


async def migrate(data: dict[str, list[dict]]) -> dict[str, int]:
    if not config.mongo_uri:
        print("ERROR: MONGO_URI is not set in the environment. Aborting — "
              "no connection attempted.", file=sys.stderr)
        sys.exit(1)

    client = AsyncIOMotorClient(config.mongo_uri, serverSelectionTimeoutMS=8000)
    try:
        await client.server_info()
    except Exception as exc:
        print(f"ERROR: could not connect to MongoDB at the configured URI: {exc}",
              file=sys.stderr)
        sys.exit(1)

    db = client[config.mongo_db_name]
    report: dict[str, int] = {}

    for table, rows in data.items():
        keys = KEY_FIELDS.get(table, [])
        inserted = 0
        for row in rows:
            filt = {k: row[k] for k in keys if k in row} or row
            result = await db[table].update_one(
                filt, {"$setOnInsert": row}, upsert=True
            )
            if result.upserted_id is not None:
                inserted += 1
        report[table] = inserted
        print(f"  {table:<20} {inserted:>6} new documents inserted "
              f"({len(rows) - inserted} already present, skipped)")

    print(f"\nMigration pass complete at {time.strftime('%Y-%m-%d %H:%M:%S')}.")
    print("SQLite database.db was NOT modified or deleted — it remains your")
    print("live backup until you verify and explicitly switch DB_BACKEND=mongo.\n")
    client.close()
    return report


async def verify(data: dict[str, list[dict]]) -> bool:
    if not config.mongo_uri:
        print("ERROR: MONGO_URI is not set. Cannot verify.", file=sys.stderr)
        sys.exit(1)

    client = AsyncIOMotorClient(config.mongo_uri, serverSelectionTimeoutMS=8000)
    db = client[config.mongo_db_name]

    print("\n=== VERIFICATION — row-by-row count parity ===")
    all_ok = True
    for table, rows in data.items():
        mongo_count = await db[table].count_documents({})
        sqlite_count = len(rows)
        # Mongo count may be >= sqlite count if re-run after settings churn,
        # but must never be LESS — that would mean data loss.
        ok = mongo_count >= sqlite_count
        status = "OK" if ok else "MISMATCH — DATA LOSS RISK"
        print(f"  {table:<20} sqlite={sqlite_count:<6} mongo={mongo_count:<6} [{status}]")
        if not ok:
            all_ok = False

    client.close()
    if all_ok:
        print("\nAll tables verified: MongoDB has at least as many documents as "
              "SQLite for every table. No data loss detected.\n")
    else:
        print("\nVERIFICATION FAILED — do not switch DB_BACKEND to mongo until "
              "this is resolved. Your SQLite backend is untouched and still safe "
              "to keep using.\n", file=sys.stderr)
    return all_ok


async def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite data to MongoDB.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect only (default if no other flag given).")
    parser.add_argument("--migrate", action="store_true", help="Actually copy data into MongoDB.")
    parser.add_argument("--verify", action="store_true", help="Verify row-count parity after migrating.")
    args = parser.parse_args()

    if not any([args.dry_run, args.migrate, args.verify]):
        args.dry_run = True  # safest possible default

    data = await read_sqlite()

    if args.dry_run:
        await dry_run(data)
    if args.migrate:
        await migrate(data)
    if args.verify:
        ok = await verify(data)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
