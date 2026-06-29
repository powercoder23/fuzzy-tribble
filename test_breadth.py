# -*- coding: utf-8 -*-
"""Tests for breadth.py — synthetic iv_history + the real sector_mapping.db."""

import os
import sqlite3
import sys
import tempfile

import breadth
import breadth_config as cfg

# Real symbols present in data/sector_mapping.db (Healthcare / Financials / IT).
PHARMA = ["ABBOTINDIA", "ALKEM", "APOLLOHOSP", "AUROPHARMA", "BIOCON", "CIPLA"]
BANKS  = ["AUBANK", "AXISBANK", "BANKBARODA", "CANBK", "FEDERALBNK", "HDFCBANK"]


def _make_iv_db(moves):
    """moves: {symbol: pct_move}. Writes a 2-snapshot intraday iv_history.db.
    Returns the db path."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE iv_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT, security_id TEXT, symbol TEXT,
        timestamp DATETIME, spot_price REAL, data_type TEXT)""")
    for i, (sym, pct) in enumerate(moves.items()):
        open_px = 100.0
        now_px = open_px * (1 + pct / 100.0)
        conn.execute("INSERT INTO iv_history(security_id,symbol,timestamp,spot_price,data_type)"
                     " VALUES(?,?,?,?,?)", (str(i), sym, "2026-06-29 09:15:00", open_px, "intraday"))
        conn.execute("INSERT INTO iv_history(security_id,symbol,timestamp,spot_price,data_type)"
                     " VALUES(?,?,?,?,?)", (str(i), sym, "2026-06-29 09:30:00", now_px, "intraday"))
    conn.commit(); conn.close()
    return path


def _scenario():
    """Pharma broadly up, banks broadly down — today's real tape."""
    moves = {}
    for s in PHARMA:
        moves[s] = 1.5
    for s in BANKS:
        moves[s] = -1.5
    # pad with neutral names so MIN_TOTAL_NAMES is met
    for i in range(20):
        moves[f"NEUTRAL{i}"] = -0.8 if i % 2 else 0.05
    return moves


def test_market_breadth_computed():
    p = _make_iv_db(_scenario())
    snap = breadth.compute(db_path=p)
    os.unlink(p)
    assert snap.market_pct is not None
    assert snap.adv >= len(PHARMA) and snap.dec >= len(BANKS)


def test_sector_split_real_db():
    p = _make_iv_db(_scenario())
    snap = breadth.compute(db_path=p)   # sector_db_path defaults to real DB
    os.unlink(p)
    heal = snap.sectors.get("Healthcare")
    fin  = snap.sectors.get("Financial Services")
    assert heal and heal["pct"] == 100.0      # all pharma up
    assert fin and fin["pct"] == 0.0          # all banks down


def test_ce_blocked_in_bearish_sector():
    p = _make_iv_db(_scenario())
    snap = breadth.compute(db_path=p)
    os.unlink(p)
    # A bank CE while the bank sector is 0% breadth → blocked.
    block, reason = breadth.breadth_blocks("CE", "AXISBANK", snap)
    assert block is True, reason


def test_pe_blocked_in_bullish_sector():
    p = _make_iv_db(_scenario())
    snap = breadth.compute(db_path=p)
    os.unlink(p)
    block, reason = breadth.breadth_blocks("PE", "CIPLA", snap)
    assert block is True, reason


def test_aligned_trade_allowed():
    p = _make_iv_db(_scenario())
    snap = breadth.compute(db_path=p)
    os.unlink(p)
    # Pharma CE (with sector bullish) should pass the sector check; market may
    # still gate, so force a balanced market for this assertion.
    snap.market_pct = 50.0
    block, reason = breadth.breadth_blocks("CE", "CIPLA", snap)
    assert block is False, reason


def test_fail_open_when_no_data():
    snap = breadth.BreadthSnapshot()   # market_pct None
    block, reason = breadth.breadth_blocks("CE", "CIPLA", snap)
    assert block is False


def test_heatmap_renders():
    p = _make_iv_db(_scenario())
    snap = breadth.compute(db_path=p)
    os.unlink(p)
    txt = breadth.format_sector_heatmap(snap)
    assert "Healthcare" in txt and "Sector heatmap" in txt


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
