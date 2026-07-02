#!/usr/bin/env python3
"""
reset_paper_experiment.py — archive the pre-fix paper book and start a clean
measurement sample (STRATEGY_REVIEW_P1.md §5, "Reset the paper experiment").

Why: trades booked before 2026-07-02 are contaminated — the Sonar side-flip
booked wrong price plans, SL fills ignored gaps, IV-rank gates used a polluted
daily history, and no trade carried costs/spread. Mixing them with post-fix
trades makes every statistic uninterpretable.

What it does (safe by design):
  1. VACUUM INTO a timestamped archive copy (consistent even if a process has
     the DB open) — nothing is deleted until the archive verifies.
  2. PRAGMA quick_check on the archive; abort if it fails.
  3. Move still-open trades' rows? No — refuses to run while any trade is
     'open' today (square off first).
  4. Deletes all rows from paper_trades in the live file (schema, WAL settings
     and new columns remain).

Usage (run WHERE THE LIVE DB LIVES — inside the discount container or on the
host against the Docker volume):
    python scripts/reset_paper_experiment.py            # dry run: show counts
    python scripts/reset_paper_experiment.py --yes      # actually archive+wipe
    python scripts/reset_paper_experiment.py --db path/to/paper_trades.db --yes
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "paper_trades.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--yes", action="store_true", help="actually archive + wipe")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"DB not found: {args.db}")

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")

    total = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    open_now = conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status='open'"
    ).fetchone()[0]
    by_strat = conn.execute(
        "SELECT COALESCE(strategy,'?'), COUNT(*), ROUND(SUM(COALESCE(realized_rupees,0)))"
        " FROM paper_trades GROUP BY strategy"
    ).fetchall()

    print(f"DB: {args.db}")
    print(f"Total trades: {total} | open now: {open_now}")
    for s, n, r in by_strat:
        print(f"  {s}: {n} trades, ₹{r or 0:,.0f} realized (pre-fix figures — biased)")

    if open_now:
        sys.exit("Refusing to reset while trades are OPEN. Square off first (15:20 job) and re-run.")
    if not args.yes:
        print("\nDry run. Re-run with --yes to archive + wipe.")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    archive = args.db.replace(".db", f"_archive_prefix_{stamp}.db")
    conn.execute(f"VACUUM INTO '{archive}'")

    check = sqlite3.connect(archive).execute("PRAGMA quick_check").fetchone()[0]
    if check != "ok":
        sys.exit(f"Archive failed quick_check ({check}) — NOT wiping. Live DB untouched.")

    conn.execute("DELETE FROM paper_trades")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"Archived {total} trades to {archive} (verified ok).")
    print("Live paper_trades is empty — clean sample starts with the next scan.")
    print("Reminder: judge nothing before ~100 closed trades per strategy tag.")


if __name__ == "__main__":
    main()
