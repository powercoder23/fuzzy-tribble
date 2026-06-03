#!/usr/bin/env python3
"""
Quick health check for the bhav / deals / vix collectors.

Reports, per table, whether it exists and how many rows were saved per date,
plus a couple of the most recent rows so you can eyeball the data.

Usage:
    # inside the running container (live DB lives in the shared-data volume)
    docker exec iv-collector python scripts/check_collectors.py

    # against an explicit DB file
    python scripts/check_collectors.py --db /app/data/iv_history.db

If --db is omitted it resolves the same path the service uses:
    $APP_BASE_DIR (or cwd) / data / iv_history.db
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# table -> (date column, sample columns to print)
_TABLES = {
    "deals":          ("date", ["symbol", "deal_type", "trade_type", "quantity", "value_cr"]),
    "delivery_daily": ("date", ["symbol", "close", "volume", "deliv_qty", "deliv_pct"]),
    "vix_daily":      ("date", ["open", "high", "low", "close", "pct_change"]),
}


def _default_db_path() -> Path:
    base = Path(os.getenv("APP_BASE_DIR", os.getcwd()))
    return base / "data" / "iv_history.db"


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    row = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _report_table(cur: sqlite3.Cursor, table: str, date_col: str, sample_cols: list) -> bool:
    print(f"\n=== {table} ===")
    if not _table_exists(cur, table):
        print("  [MISSING] table does not exist - collector has never saved a "
              "non-empty result (fetch likely failing/blocked).")
        return False

    total = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"  rows: {total}")
    if total == 0:
        print("  [EMPTY] table exists but has no rows.")
        return False

    print(f"  per {date_col}:")
    for d, c in cur.execute(
        f"SELECT {date_col}, COUNT(*) FROM {table} GROUP BY {date_col} "
        f"ORDER BY {date_col}"
    ).fetchall():
        print(f"    {d}: {c}")

    cols = ", ".join([date_col] + sample_cols)
    print(f"  latest rows ({cols}):")
    for row in cur.execute(
        f"SELECT {cols} FROM {table} ORDER BY {date_col} DESC LIMIT 3"
    ).fetchall():
        print("    " + " | ".join("" if v is None else str(v) for v in row))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Collector data health check")
    parser.add_argument("--db", default=None,
                        help="Path to iv_history.db (default: $APP_BASE_DIR/data/iv_history.db)")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else _default_db_path()
    print(f"DB: {db_path}")
    if not db_path.exists():
        print(f"[ERROR] DB file not found at {db_path}")
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        results = {
            table: _report_table(cur, table, date_col, sample_cols)
            for table, (date_col, sample_cols) in _TABLES.items()
        }
    finally:
        conn.close()

    print("\n=== summary ===")
    for table, ok in results.items():
        print(f"  {'OK  ' if ok else 'FAIL'}  {table}")

    # exit non-zero if any collector has no data, so this is CI/cron friendly
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
