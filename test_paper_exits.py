# -*- coding: utf-8 -*-
"""Regression tests for review 2026-07-09 BUG-1 / BUG-2 and the B&B paper
booking + auto-exit changes.

BUG-1: apply_tick left full-book plans (t1_book_fraction=1.0, t1==t2 — every
       B&B trade) as zero-quantity "open" rows until 15:20 square-off.
BUG-2: process_signals' sonar veto used the latest sonar row EVER, so a stale
       FLAT/BREAKDOWN from yesterday's close vetoed today's entries.
Plus:  per-signal min-premium override (B&B cheap large-lot names) and the
       relaxed auto-exit contradiction defaults.
"""

import os
import sys
import tempfile
from datetime import datetime

import paper_trader
from paper_trader import new_trade_runtime, apply_tick


# -- BUG-1: full-book single-target plans must close at T1 ------------------

def test_full_target_closes_immediately():
    t = new_trade_runtime(entry=100, sl=85, t1=125, t2=125,
                          t1_book_fraction=1.0, lot_size=500)
    ev = apply_tick(t, 126)
    assert ev == ["T1_FULL"], ev
    assert t["status"] == "closed", t["status"]
    assert t["exit_reason"] == "Target full", t["exit_reason"]
    assert t["qty_frac"] <= 1e-9
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
    t = new_trade_runtime(entry=100, sl=85, t1=125, t2=145,
                          t1_book_fraction=0.7, lot_size=500)
    ev = apply_tick(t, 150)
    assert ev == ["T1", "T2"], ev
    assert t["status"] == "closed"


def test_full_target_no_longer_lingers_to_square_off():
    t = new_trade_runtime(entry=100, sl=85, t1=125, t2=125,
                          t1_book_fraction=1.0, lot_size=500)
    apply_tick(t, 126)
    ev = apply_tick(t, 90, square_off=True)
    assert ev == [], ev
    assert t["exit_reason"] == "Target full"


# -- BUG-2: stale sonar rows must not veto entries ---------------------------

def _mk_row(symbol="CIPLA", sec_id="1234"):
    return {
        "symbol": symbol, "security_id": sec_id, "exchange_segment": "NSE_FNO",
        "type": "CALL", "strike": 1500.0, "expiry": "2026-07-30",
        "entry": 100.0, "stop_loss": 85.0, "t1": 125.0, "t2": 145.0,
        "score": 80.0,
    }


def _run_process_signals(monkey_sonar, now):
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


# -- B&B booking: per-signal min-premium override ----------------------------

def _mk_bb_signal(entry=1.8, min_premium=None):
    sig = {
        "symbol": "NHPC", "security_id": "17400", "exchange_segment": "NSE_FNO",
        "side": "CE", "strike": 80.0, "expiry": "2026-07-30",
        "entry": entry, "sl": round(entry * 0.7, 2),
        "t1": round(entry + entry * 0.3 * 2.5, 2),
        "t2": round(entry + entry * 0.3 * 2.5, 2),
        "t1_book_fraction": 1.0, "lot_size": 6950,
        "strategy": "Break & Bounce",
    }
    if min_premium is not None:
        sig["min_premium"] = min_premium
    return sig


def _run_book_signal(sig, now):
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    book = paper_trader.PaperTradeBook(db_path=p)
    orig_send = paper_trader.send_telegram
    paper_trader.send_telegram = lambda *a, **k: True
    try:
        booked = paper_trader.book_signal(book, sig, now=now)
        trades = book.all_trades(now.date().isoformat())
    finally:
        paper_trader.send_telegram = orig_send
        os.unlink(p)
    return booked, trades


def test_cheap_bb_premium_blocked_without_override():
    now = datetime(2026, 7, 9, 10, 0, 0)
    booked, trades = _run_book_signal(_mk_bb_signal(entry=1.8), now)
    assert booked is None and trades == [], "Rs 1.8 must hit the default Rs 5 floor"


def test_cheap_bb_premium_books_with_own_floor():
    now = datetime(2026, 7, 9, 10, 0, 0)
    booked, trades = _run_book_signal(_mk_bb_signal(entry=1.8, min_premium=0.5), now)
    assert booked is not None, "B&B floor 0.5 must allow a Rs 1.8 premium"
    assert len(trades) == 1 and trades[0]["strategy"] == "Break & Bounce"
    assert abs(trades[0]["entry"] - 1.8) < 1e-9


def test_bb_floor_still_filters_junk():
    now = datetime(2026, 7, 9, 10, 0, 0)
    booked, _ = _run_book_signal(_mk_bb_signal(entry=0.3, min_premium=0.5), now)
    assert booked is None, "sub-floor junk must still be rejected"


# -- Auto-exit: weak covering reads now contradict (config defaults) ---------

def test_weak_covering_contradiction_exits_with_new_defaults():
    import auto_exit_config as cfg
    from order_manager import oi_contradicts
    # JUBLFOOD sample: PE position, SHORT_COVERING -> CE bias, OI -1.0%, pnl -7.6%
    assert oi_contradicts(
        "PE", "CE", "weak", -1.0, -7.6,
        min_oi_chg_pct=cfg.MIN_OI_CHG_PCT,
        require_strong=cfg.REQUIRE_STRONG,
        max_profit_pct=cfg.MAX_PROFIT_PCT,
    ), "weak covering read must now trigger the auto-exit"
    assert not oi_contradicts(
        "PE", "CE", "weak", -1.0, 25.0,
        min_oi_chg_pct=cfg.MIN_OI_CHG_PCT,
        require_strong=cfg.REQUIRE_STRONG,
        max_profit_pct=cfg.MAX_PROFIT_PCT,
    ), "winner-skip (MAX_PROFIT_PCT) must survive the config change"


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
