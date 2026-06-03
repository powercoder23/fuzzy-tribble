#!/usr/bin/env python3
"""
Show the rows each collector saved for a given date (default: yesterday).

Handy for confirming the bhav / deals / vix collectors actually persisted data
for the last trading session.

Usage:
    docker exec iv-collector python scripts/find_saved_rows.py
    python scripts/find_saved_rows.py --date 2026-06-02
    python scripts/find_saved_rows.py --db /app/data/iv_history.db --limit 100

If --db is omitted it resolves $APP_BASE_DIR (or cwd) / data / iv_history.db.
delivery_daily and vix_daily store ISO dates (YYYY-MM-DD); the deals table
stores NSE's native date string, so several common formats are matched.
"""

import argparse
import datetime
import os
import sqlite3
import sys
from pathlib import Path

# table -> (date column, display columns)
_TABLES = {
    "deals":          ("date", ["symbol", "deal_type", "trade_type",
                                 "quantity", "price", "value_cr", "client"]),
    "delivery_daily": ("date", ["symbol", "close", "volume",
                                 "deliv_qty", "deliv_pct"]),
    "vix_daily":      ("date", ["open", "high", "low", "close",
                                 "prev_close", "change", "pct_change"]),
}


def _default_db_path() -> Path:
    base = Path(os.getenv("APP_BASE_DIR", os.getcwd()))
    return base / "data" / "iv_history.db"


def _date_candidates(d: datetime.date) -> list:
    """Common string forms NSE / our collectors use for one calendar date."""
    mon = d.strftime("%b")
    cands = {
        d.isoformat(),                          # 2026-06-02
        d.strftime("%d-%b-%Y"),                 # 02-Jun-2026
        d.strftime("%d-%b-%Y").upper(),         # 02-JUN-2026
        f"{d.day}-{mon}-{d.year}",              # 2-Jun-2026
        f"{d.day}-{mon.upper()}-{d.year}",      # 2-JUN-2026
        d.strftime("%d/%m/%Y"),                 # 02/06/2026
        d.strftime("%d-%m-%Y"),                 # 02-06-2026
    }
    return sorted(cands)


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    return cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _report(cur, table, date_col, disp_cols, candidates, limit) -> int:
    print(f"\n=== {table} ===")
    if not _table_exists(cur, table):
        print("  [MISSING] table does not exist")
        return 0

    placeholders = ",".join("?" * len(candidates))
    col_sql = ", ".join([date_col] + disp_cols)

    total = cur.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {date_col} IN ({placeholders})",
        candidates,
    ).fetchone()[0]
    print(f"  matched rows: {total}")

    if total == 0:
        avail = [r[0] for r in cur.execute(
            f"SELECT DISTINCT {date_col} FROM {table} "
            f"ORDER BY {date_col} DESC LIMIT 10"
        ).fetchall()]
        print("  no rows for that date. recent dates present:",
              ", ".join(avail) if avail else "(table empty)")
        return 0

    rows = cur.execute(
        f"SELECT {col_sql} FROM {table} WHERE {date_col} IN ({placeholders}) "
        f"ORDER BY {date_col} LIMIT ?",
        candidates + [limit],
    ).fetchall()
    print(f"  columns: {col_sql}")
    for row in rows:
        print("    " + " | ".join("" if v is None else str(v) for v in row))
    if total > len(rows):
        print(f"    ... ({total - len(rows)} more, raise --limit to see them)")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Show collector rows for a date")
    parser.add_argument("--date", default=None,
                        help="Target date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--db", default=None,
                        help="Path to iv_history.db (default: $APP_BASE_DIR/data/iv_history.db)")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max rows to print per table (default: 50)")
    args = parser.parse_args()

    if args.date:
        try:
            target = datetime.date.fromisoformat(args.date)
        except ValueError:
            print(f"[ERROR] --date must be YYYY-MM-DD, got {args.date!r}")
            return 2
    else:
        target = datetime.date.today() - datetime.timedelta(days=1)

    db_path = Path(args.db) if args.db else _default_db_path()
    candidates = _date_candidates(target)

    print(f"DB:   {db_path}")
    print(f"Date: {target.isoformat()}  (matching any of: {', '.join(candidates)})")
    if not db_path.exists():
        print(f"[ERROR] DB file not found at {db_path}")
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        counts = {
            table: _report(cur, table, date_col, disp_cols, candidates, args.limit)
            for table, (date_col, disp_cols) in _TABLES.items()
        }
    finally:
        conn.close()

    print("\n=== summary ===")
    for table, n in counts.items():
        print(f"  {n:>5} rows  {table}")
    return 0 if any(counts.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
