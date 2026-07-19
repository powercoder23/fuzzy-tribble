#!/usr/bin/env python3
"""
recover_iv_history_verbose.py — same salvage + daily-collapse as
recover_iv_history.py, but with LIVE PROGRESS output so you can see it working:
per-table percentage, running salvaged/lost counts, and periodic WAL
checkpoints so the recovered .db file visibly grows.

Source is opened READ-ONLY and never modified. Safe to Ctrl-C and rerun.

Usage:
    python scripts/recover_iv_history_verbose.py data/iv_history.db data/iv_history_recovered.db
    # optional 3rd arg = known-good sibling DB for schema if source master is damaged
"""
import sqlite3, sys, time

CHUNK = 2000
PROGRESS_EVERY = 20_000   # print a progress line every N rowids scanned


def salvage_table(src, dst, table, cols, max_rowid):
    col_list = ", ".join(f'"{c}"' for c in cols)
    ins = f'INSERT OR IGNORE INTO "{table}" ({col_list}) VALUES ({", ".join("?"*len(cols))})'
    saved = lost = 0
    last_print = 0
    t0 = time.time()

    def copy_range(lo, hi):
        nonlocal saved, lost
        if lo > hi:
            return
        try:
            rows = src.execute(
                f'SELECT {col_list} FROM "{table}" WHERE rowid BETWEEN ? AND ?',
                (lo, hi)).fetchall()
            if rows:
                dst.executemany(ins, rows)
                saved += len(rows)
        except sqlite3.DatabaseError:
            if lo == hi:
                lost += 1
                return
            mid = (lo + hi) // 2
            copy_range(lo, mid)
            copy_range(mid + 1, hi)

    lo = 1
    while lo <= max_rowid:
        copy_range(lo, min(lo + CHUNK - 1, max_rowid))
        lo += CHUNK
        if lo - last_print >= PROGRESS_EVERY:
            dst.commit()
            try:
                dst.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
            pct = min(100.0, lo / max(max_rowid, 1) * 100)
            rate = saved / max(time.time() - t0, 0.001)
            print(f"  {table:<20} {pct:5.1f}%  saved={saved:,}  lost~={lost}  "
                  f"({rate:,.0f} rows/s)", flush=True)
            last_print = lo
    dst.commit()
    print(f"  {table:<20} DONE  saved={saved:,}  lost~={lost}", flush=True)
    return saved, lost


def main():
    src_path, dst_path = sys.argv[1], sys.argv[2]
    schema_path = sys.argv[3] if len(sys.argv) > 3 else src_path
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("PRAGMA synchronous=OFF")   # speed; it's a throwaway rebuild

    schema_conn = sqlite3.connect(f"file:{schema_path}?mode=ro", uri=True)
    schema = schema_conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    schema_conn.close()

    tables = []
    for typ, name, sql in schema:
        if name.startswith("sqlite_"):
            continue
        dst.execute(sql)
        if typ == "table":
            tables.append(name)
    dst.commit()
    print(f"tables to salvage: {tables}\n", flush=True)

    total = 0
    for t in tables:
        cols = [r[1] for r in src.execute(f'PRAGMA table_info("{t}")').fetchall()]
        try:
            max_rowid = src.execute(f'SELECT MAX(rowid) FROM "{t}"').fetchone()[0] or 0
        except sqlite3.DatabaseError:
            max_rowid = 10_000_000
        print(f"[{t}] scanning up to rowid {max_rowid:,} ...", flush=True)
        saved, _ = salvage_table(src, dst, t, cols, max_rowid)
        total += saved

    if "iv_history" in tables:
        before = dst.execute("SELECT COUNT(*) FROM iv_history WHERE data_type='daily'").fetchone()[0]
        dst.execute("""
            DELETE FROM iv_history WHERE data_type='daily'
              AND rowid NOT IN (
                  SELECT MAX(rowid) FROM iv_history WHERE data_type='daily'
                  GROUP BY security_id, DATE(timestamp))""")
        after = dst.execute("SELECT COUNT(*) FROM iv_history WHERE data_type='daily'").fetchone()[0]
        dst.commit()
        print(f"\ndaily rows: {before:,} -> {after:,} (collapsed to one per symbol-day)", flush=True)

    print("vacuuming (folds WAL into the .db file) ...", flush=True)
    dst.execute("VACUUM")
    check = dst.execute("PRAGMA quick_check").fetchone()[0]
    print(f"recovered db quick_check: {check}")
    print(f"total salvaged rows: {total:,}")
    src.close(); dst.close()


if __name__ == "__main__":
    main()
