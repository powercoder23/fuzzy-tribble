# -*- coding: utf-8 -*-
"""Regression tests for review 2026-07-09 BUG-1 / BUG-2.

BUG-1: apply_tick left full-book plans (t1_book_fraction=1.0, t1==t2 — every
       B&B trade) as zero-quantity "open" rows until 15:20 square-off.
BUG-2: process_signals' sonar veto used the latest sonar row EVER, so a stale
       FLAT/BREAKDOWN from yesterday's close vetoed today's entries.
"""

import os
import sys
import tempfile
from datetime import datetime

import paper_trader
from paper_trader import new_trade_runtime, apply_tick


# ── BUG-1: full-book single-target plans must close at T1 ────────────────── #

def test_full_target_closes_immediately():
    t = new_trade_runtime(entry=100, sl=85, t1=125, t2=125,
                          t1_book_fraction=1.0, lot_size=500)
    ev = apply_tick(t, 126)
    assert ev == ["T1_FULL"], ev
    assert t["status"] == "closed", t["status"]
    assert t["exit_reason"] == "Target full", t["exit_reason"]
    assert t["qty_frac"] <= 1e-9
    # gross 25 pts on 100 entry (net after slippage model; no half_spread here)
    assert abs(t["gross_points"] - 25.0) < 1e-6, t["gross_points"]


def test_partial_t1_keeps_runner_open():
    t = new_trade_runtime(entry=100, sl=85, t1=125, t2=145,
                          t1_book_fraction=0.7, lot_size=500)
    ev = apply_tick(t, 126)
    assert ev == ["T1"], ev
    assert t["status"] == "open"
    assert abs(t["qty_frac"] - 0.3) < 1e-9
    ev2 = apply_tick(t, 146)
    assert ev2 == ["T2"], ev2
    assert t["status"] == "closed"
    assert t["exit_reason"] == "T2"


def test_gap_through_t2_books_t1_and_t2_same_tick():
    # Existing behaviour for partial plans must be unchanged.
    t = new_trade_runtime(entry=100, sl=85, t1=125, t2=145,
                          t1_book_fraction=0.7, lot_size=500)
    ev = apply_tick(t, 150)
    assert ev == ["T1", "T2"], ev
    assert t["status"] == "closed"


def test_full_target_no_longer_lingers_to_square_off():
    t = new_trade_runtime(entry=100, sl=85, t1=125, t2=125,
                          t1_book_fraction=1.0, lot_size=500)
    apply_tick(t, 126)
    # A later square-off tick must be a no-op on an already-closed trade.
    ev = apply_tick(t, 90, square_off=True)
    assert ev == [], ev
    assert t["exit_reason"] == "Target full"


# ── BUG-2: stale sonar rows must not veto entries ─────────────────────────── #

def _mk_row(symbol="CIPLA", sec_id="1234"):
    return {
        "symbol": symbol, "security_id": sec_id, "exchange_segment": "NSE_FNO",
        "type": "CALL", "strike": 1500.0, "expiry": "2026-07-30",
        "entry": 100.0, "stop_loss": 85.0, "t1": 125.0, "t2": 145.0,
        "score": 80.0,
    }


def _run_process_signals(monkey_sonar, now):
    """Run process_signals against a temp book with a patched sonar read and
    a muted telegram. Returns the list of opened signals."""
    import sonar_laplace_scanner
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    book = paper_trader.PaperTradeBook(db_path=p)
    orig_sonar = sonar_laplace_scanner.get_latest_sonar
    orig_send = paper_trader.send_telegram
    sonar_laplace_scanner.get_latest_sonar = monkey_sonar
    paper_trader.send_telegram = lambda *a, **k: True
    try:
        opened = paper_trader.process_signals(book, [_mk_row()], now=now)
    finally:
        sonar_laplace_scanner.get_latest_sonar = orig_sonar
        paper_trader.send_telegram = orig_send
        os.unlink(p)
    return opened


def test_stale_flat_sonar_does_not_veto():
    now = datetime(2026, 7, 9, 10, 0, 0)
    stale = {"signal": "FLAT", "timestamp": "2026-07-08 15:25:00"}
    opened = _run_process_signals(lambda sid: stale, now)
    assert len(opened) == 1, "yesterday's FLAT must not veto today's entry"


def test_same_day_flat_sonar_still_vetoes():
    now = datetime(2026, 7, 9, 10, 0, 0)
    fresh = {"signal": "FLAT", "timestamp": "2026-07-09 09:55:00"}
    opened = _run_process_signals(lambda sid: fresh, now)
    assert opened == [], "same-day FLAT must still veto"


def test_same_day_contradiction_still_vetoes():
    now = datetime(2026, 7, 9, 10, 0, 0)
    fresh = {"signal": "BREAKDOWN", "timestamp": "2026-07-09 09:55:00"}
    opened = _run_process_signals(lambda sid: fresh, now)  # row is a CALL
    assert opened == [], "same-day BREAKDOWN must veto a CALL"


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
