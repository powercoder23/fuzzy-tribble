#!/usr/bin/env python3
"""
recover_iv_history.py — salvage rows from a corrupted iv_history.db.

Reads every table in chunked rowid ranges, bisecting around unreadable pages,
and writes the surviving rows into a fresh WAL-mode database. Then collapses
duplicate 'daily' rows (keeps the LAST row per security per calendar day) —
see ARCHITECTURE_REVIEW_P0.md §2.1.

Usage:
    python scripts/recover_iv_history.py <corrupt.db> <recovered.db> [schema.db]

If the corrupt file's sqlite_master is itself unreadable, pass a known-good
sibling DB (e.g. an older snapshot) as third argument to source the schema.
The corrupt source is opened read-only and never modified.
"""

import sqlite3
import sys


def salvage_table(src, dst, table, cols):
    col_list = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join("?" * len(cols))
    ins = f'INSERT OR IGNORE INTO "{table}" ({col_list}) VALUES ({placeholders})'

    try:
        max_rowid = src.execute(f'SELECT MAX(rowid) FROM "{table}"').fetchone()[0] or 0
    except sqlite3.DatabaseError:
        max_rowid = 10_000_000  # unknown — probe blindly

    saved = lost_ranges = 0

    def copy_range(lo, hi):
        nonlocal saved, lost_ranges
        if lo > hi:
            return
        try:
            rows = src.execute(
                f'SELECT {col_list} FROM "{table}" WHERE rowid BETWEEN ? AND ?',
                (lo, hi),
            ).fetchall()
            if rows:
                dst.executemany(ins, rows)
                saved += len(rows)
        except sqlite3.DatabaseError:
            if lo == hi:
                lost_ranges += 1
                return
            mid = (lo + hi) // 2
            copy_range(lo, mid)
            copy_range(mid + 1, hi)

    chunk = 2000
    lo = 1
    while lo <= max_rowid:
        copy_range(lo, min(lo + chunk - 1, max_rowid))
        lo += chunk
    dst.commit()
    return saved, lost_ranges


def main():
    src_path, dst_path = sys.argv[1], sys.argv[2]
    schema_path = sys.argv[3] if len(sys.argv) > 3 else src_path
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    dst.execute("PRAGMA journal_mode=WAL")

    # Schema — from the source itself, or a known-good sibling if damaged.
    try:
        schema_conn = sqlite3.connect(f"file:{schema_path}?mode=ro", uri=True)
        schema = schema_conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
        schema_conn.close()
    except sqlite3.DatabaseError:
        print(f"sqlite_master unreadable in {schema_path}; "
              "pass a known-good schema DB as third argument")
        raise
    tables = []
    for typ, name, sql in schema:
        if name.startswith("sqlite_"):
            continue
        dst.execute(sql)
        if typ == "table":
            tables.append(name)
    dst.commit()

    total_saved = 0
    for t in tables:
        cols = [r[1] for r in src.execute(f'PRAGMA table_info("{t}")').fetchall()]
        saved, lost = salvage_table(src, dst, t, cols)
        total_saved += saved
        print(f"{t:<24} salvaged={saved:>8}  unreadable_rows~={lost}")

    # Collapse duplicate 'daily' rows: keep last per (security_id, calendar day)
    if "iv_history" in tables:
        before = dst.execute(
            "SELECT COUNT(*) FROM iv_history WHERE data_type='daily'"
        ).fetchone()[0]
        dst.execute("""
            DELETE FROM iv_history
            WHERE data_type = 'daily'
              AND id NOT IN (
                  SELECT MAX(id) FROM iv_history
                  WHERE data_type = 'daily'
                  GROUP BY security_id, DATE(timestamp)
              )
        """)
        after = dst.execute(
            "SELECT COUNT(*) FROM iv_history WHERE data_type='daily'"
        ).fetchone()[0]
        dst.commit()
        print(f"daily rows: {before} -> {after} (collapsed to one per symbol-day)")

    dst.execute("VACUUM")
    check = dst.execute("PRAGMA quick_check").fetchone()[0]
    print(f"recovered db quick_check: {check}")
    print(f"total salvaged rows: {total_saved}")
    src.close()
    dst.close()


if __name__ == "__main__":
    main()
