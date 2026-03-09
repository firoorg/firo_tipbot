"""
One-time migration: convert Balance and Locked fields from float to Decimal128.

This script safely converts all monetary fields in the database from IEEE 754
float to BSON Decimal128, eliminating floating-point precision drift.

Safety features:
  - Dry-run mode by default (pass --apply to actually write)
  - Logs every change before and after
  - Only updates documents where Balance/Locked are not already Decimal128
  - Handles int, float, and string values gracefully
  - Does NOT delete or modify any other fields

Usage:
  python3 migrate_to_decimal128.py          # dry run, shows what would change
  python3 migrate_to_decimal128.py --apply  # actually perform the migration

Prerequisites:
  - MongoDB must be running
  - services.json must be configured
  - Back up your database before running with --apply
"""
import json
import sys
from decimal import Decimal, ROUND_DOWN
from bson.decimal128 import Decimal128
from pymongo import MongoClient

QUANTIZE_EXP = Decimal('0.00000001')


def to_decimal128(value):
    """Convert any numeric value to Decimal128 with 8 decimal places."""
    if isinstance(value, Decimal128):
        return value  # already migrated
    d = Decimal(str(value)).quantize(QUANTIZE_EXP, rounding=ROUND_DOWN)
    return Decimal128(d)


def needs_migration(value):
    """Check if a value needs to be migrated (is not already Decimal128)."""
    return not isinstance(value, Decimal128)


def migrate_collection(col, fields, dry_run=True):
    """Migrate specified fields in a collection from float to Decimal128."""
    migrated = 0
    skipped = 0
    errors = 0

    for doc in col.find():
        update = {}
        for field in fields:
            if field in doc and needs_migration(doc[field]):
                try:
                    new_val = to_decimal128(doc[field])
                    update[field] = new_val
                except Exception as e:
                    print(f"  ERROR: _id={doc['_id']} field={field} value={doc[field]!r}: {e}")
                    errors += 1

        if update:
            old_vals = {f: doc.get(f) for f in update}
            print(f"  _id={doc['_id']}: {old_vals} -> {update}")
            if not dry_run:
                col.update_one({"_id": doc["_id"]}, {"$set": update})
            migrated += 1
        else:
            skipped += 1

    return migrated, skipped, errors


def main():
    dry_run = "--apply" not in sys.argv

    if dry_run:
        print("=== DRY RUN MODE (pass --apply to write changes) ===\n")
    else:
        print("=== APPLYING MIGRATION ===\n")

    with open('services.json') as f:
        conf = json.load(f)

    client = MongoClient(conf['mongo']['connectionString'])
    db = client.get_default_database()

    # Migrate users collection: Balance and Locked
    print("--- Migrating 'users' collection (Balance, Locked) ---")
    m, s, e = migrate_collection(db['users'], ['Balance', 'Locked'], dry_run)
    print(f"  Result: {m} migrated, {s} already ok, {e} errors\n")

    # Migrate envelopes collection: amount and remains
    print("--- Migrating 'envelopes' collection (amount, remains) ---")
    m, s, e = migrate_collection(db['envelopes'], ['amount', 'remains'], dry_run)
    print(f"  Result: {m} migrated, {s} already ok, {e} errors\n")

    # Migrate tip_logs collection: amount
    print("--- Migrating 'tip_logs' collection (amount) ---")
    m, s, e = migrate_collection(db['tip_logs'], ['amount'], dry_run)
    print(f"  Result: {m} migrated, {s} already ok, {e} errors\n")

    if dry_run:
        print("No changes written. Run with --apply to perform migration.")
    else:
        print("Migration complete.")


if __name__ == '__main__':
    main()
