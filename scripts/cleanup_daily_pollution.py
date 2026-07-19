#!/usr/bin/env python3
"""
cleanup_daily_pollution.py — collapse legacy duplicate 'daily' rows in
iv_history.db to ONE row per (security_id, calendar day), keeping the LAST
row of each day (the near-close value — matches promote_daily_from_last_intraday
and iv_store.get_iv_history).

Background: an old collector bug wrote a 'daily' row on every intraday scan
(save_snapshot's data_type default is 'daily'), flooding the daily history with
20-55 rows/symbol/day in ~June-early July. The write path is already fixed; this
is a one-time cleanup of the residue so June-spanning lookbacks (52wk IVP,
backtests) and the IV-slope readers see one daily close per day.

SAFETY:
  * Stop the iv-collector container first so nothing writes mid-delete.
  * Takes a VACUUM INTO backup before touching anything; aborts if that fails.
  * Read-only dry run by default. Pass --apply to actually delete.

Usage:
    python scripts/cleanup_daily_pollution.py            # dry run (counts only)
    python scripts/cleanup_daily_pollution.py --apply    # backup + collapse + vacuum
"""
import os
import sys
import sqlite3
from datetime import datetime

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "iv_history.db")

DUPE_COUNT_SQL = """
    SELECT COUNT(*) AS total_daily,
           COUNT(*) - COUNT(DISTINCT security_id || '|' || DATE(timestamp)) AS to_delete
    FROM iv_history WHERE data_type='daily'
"""

DELETE_SQL = """
    DELETE FROM iv_history
    WHERE data_type='daily'
      AND rowid NOT IN (
          SELECT MAX(rowid) FROM iv_history
          WHERE data_type='daily'
          GROUP BY security_id, DATE(timestamp)
      )
"""

VERIFY_SQL = """
    SELECT COUNT(*) FROM iv_history AS h
    WHERE h.data_type='daily'
      AND EXISTS (SELECT 1 FROM iv_history i2
                  WHERE i2.data_type='daily' AND i2.security_id=h.security_id
                    AND DATE(i2.timestamp)=DATE(h.timestamp) AND i2.rowid<>h.rowid)
"""


def main():
    apply = "--apply" in sys.argv
    if not os.path.exists(DB):
        sys.exit(f"DB not found: {DB}")

    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")

    total, to_delete = conn.execute(DUPE_COUNT_SQL).fetchone()
    print(f"daily rows: {total:,}   duplicates to remove: {to_delete:,}")
    if to_delete == 0:
        print("Nothing to clean — already one daily row per symbol-day.")
        return
    if not apply:
        print("\nDRY RUN — re-run with --apply to back up and collapse.")
        return

    # 1) backup
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(os.path.dirname(DB), f"iv_history_backup_{stamp}.db")
    print(f"\nBacking up -> {backup}")
    try:
        conn.execute(f"VACUUM INTO '{backup}'")
    except sqlite3.Error as e:
        sys.exit(f"Backup failed, aborting without changes: {e}")

    # 2) collapse
    cur = conn.execute(DELETE_SQL)
    deleted = cur.rowcount
    conn.commit()
    print(f"Deleted {deleted:,} duplicate daily rows.")

    # 3) verify + reclaim
    remaining = conn.execute(VERIFY_SQL).fetchone()[0]
    print(f"Remaining symbol-days with >1 daily row: {remaining}  (expect 0)")
    if remaining == 0:
        conn.execute("VACUUM")
        print("VACUUM done. Cleanup complete.")
    else:
        print("WARNING: duplicates remain — DID NOT vacuum. Backup is intact.")


if __name__ == "__main__":
    main()
