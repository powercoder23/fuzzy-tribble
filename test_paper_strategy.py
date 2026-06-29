# -*- coding: utf-8 -*-
"""Schema/migration + EOD-aggregation checks for the paper_trader strategy
column. Self-contained (faithful copy of the SQL in paper_trader) so it runs
without importing the package."""

import os
import sqlite3
import sys
import tempfile

VOL = "Volatility Expansion Play"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, opened_at TEXT, closed_at TEXT,
    symbol TEXT, security_id TEXT, exchange_segment TEXT,
    side TEXT, strike REAL, expiry TEXT,
    entry REAL, sl REAL, t1 REAL, t2 REAL,
    t1_book_fraction REAL, lot_size INTEGER,
    score REAL, iv REAL, hv REAL, iv_rank REAL, dte INTEGER,
    strategy TEXT,
    status TEXT, t1_done INTEGER DEFAULT 0, qty_frac REAL DEFAULT 1.0,
    booked_points REAL DEFAULT 0.0, runner_stop REAL, last_price REAL,
    exit_reason TEXT, realized_points REAL, realized_pct REAL, realized_rupees REAL
);
"""

_OLD_SCHEMA = _SCHEMA.replace("    strategy TEXT,\n", "")   # pre-migration table

_INSERT = """INSERT INTO paper_trades
   (date, opened_at, symbol, security_id, exchange_segment, side,
    strike, expiry, entry, sl, t1, t2, t1_book_fraction, lot_size,
    score, iv, hv, iv_rank, dte, strategy, status, t1_done, qty_frac,
    booked_points, runner_stop, last_price)
   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def _insert(conn, symbol, side, strike, strategy):
    conn.execute(_INSERT, (
        "2026-06-29", "2026-06-29 09:30:00", symbol, "1", "NSE_FNO", side,
        float(strike), "2026-07-03", 10.0, 8.5, 12.5, 12.5, 1.0, 50,
        None, None, None, None, None, strategy,
        "open", 0, 1.0, 0.0, 8.5, 10.0,
    ))


def test_insert_with_strategy_roundtrips():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    c = sqlite3.connect(p); c.execute(_SCHEMA)
    _insert(c, "CIPLA", "CE", 1500, VOL)
    _insert(c, "TATAPOWER", "PE", 400, "Break & Bounce")
    c.commit()
    rows = c.execute("SELECT symbol, strategy FROM paper_trades ORDER BY symbol").fetchall()
    c.close(); os.unlink(p)
    assert dict(rows) == {"CIPLA": VOL, "TATAPOWER": "Break & Bounce"}, rows


def test_migration_adds_column():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    c = sqlite3.connect(p); c.execute(_OLD_SCHEMA)   # legacy DB, no strategy col
    cols = {r[1] for r in c.execute("PRAGMA table_info(paper_trades)")}
    assert "strategy" not in cols
    # migration (idempotent)
    if "strategy" not in cols:
        c.execute("ALTER TABLE paper_trades ADD COLUMN strategy TEXT")
    cols2 = {r[1] for r in c.execute("PRAGMA table_info(paper_trades)")}
    assert "strategy" in cols2
    _insert(c, "CIPLA", "CE", 1500, VOL); c.commit()
    c.close(); os.unlink(p)


def test_eod_per_strategy_aggregation():
    # Mirrors the per-strategy breakdown loop in format_eod_summary.
    trades = [
        {"strategy": VOL, "realized_rupees": -1000},
        {"strategy": VOL, "realized_rupees": -940},
        {"strategy": "Break & Bounce", "realized_rupees": 1200},
    ]
    by = {}
    for t in trades:
        s = t.get("strategy") or VOL
        agg = by.setdefault(s, {"n": 0, "rupees": 0.0})
        agg["n"] += 1
        agg["rupees"] += (t.get("realized_rupees") or 0)
    assert by[VOL] == {"n": 2, "rupees": -1940}
    assert by["Break & Bounce"] == {"n": 1, "rupees": 1200}
    assert len(by) > 1   # breakdown only shown when >1 strategy


if __name__ == "__main__":
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1; print(f"ERROR {fn.__name__}: {e!r}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
