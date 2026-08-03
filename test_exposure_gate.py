# -*- coding: utf-8 -*-
"""Tests for the portfolio-wide open-exposure gate (_apply_exposure_gate),
mirroring the existing daily-loss gate test style: mode/limit resolvers are
monkeypatched so tests don't depend on the shared settings DB or env vars.
scan_log.record_decision is stubbed so a "hard" block doesn't write into the
real shared paper_trades.db during tests."""

import sys

import order_manager
import scan_log
from order_manager import OrderManager


class _FakeBook:
    def __init__(self, trades):
        self._t = trades

    def open_trades(self, date):
        return [t for t in self._t if str(t.get("status")) == "open"]


def _open(symbol, entry, lot_size=500):
    return {"status": "open", "symbol": symbol, "side": "CE",
            "entry": entry, "lot_size": lot_size}


def _cand(symbol, entry, lot_size=500):
    return {"symbol": symbol, "side": "CE", "strike": 100.0,
            "entry": entry, "lot_size": lot_size, "strategy": "Test"}


def _om(open_trades, mode, max_positions=0, max_premium=0.0):
    order_manager._resolve_mode = lambda k, f: mode
    limits = {"EXPOSURE_MAX_OPEN_POSITIONS": max_positions,
              "EXPOSURE_MAX_OPEN_PREMIUM_RUPEES": max_premium}
    order_manager._resolve_limit = lambda k, f: limits.get(k, f)
    scan_log.record_decision = lambda *a, **k: None
    return OrderManager(book=_FakeBook(open_trades))


def test_off_mode_passthrough_regardless_of_caps():
    om = _om([_open("X", 100)], "off", max_positions=1, max_premium=1.0)
    kept = om._apply_exposure_gate([_cand("Y", 50)], om.book)
    assert len(kept) == 1


def test_zero_caps_disable_the_gate():
    om = _om([], "hard", max_positions=0, max_premium=0.0)
    kept = om._apply_exposure_gate([_cand("Y", 50)], om.book)
    assert len(kept) == 1


def test_hard_blocks_when_position_cap_hit():
    om = _om([_open("A", 100), _open("B", 100)], "hard", max_positions=2)
    kept = om._apply_exposure_gate([_cand("C", 50)], om.book)
    assert kept == []


def test_hard_allows_under_position_cap():
    om = _om([_open("A", 100)], "hard", max_positions=2)
    kept = om._apply_exposure_gate([_cand("C", 50)], om.book)
    assert len(kept) == 1


def test_soft_logs_but_keeps():
    om = _om([_open("A", 100), _open("B", 100)], "soft", max_positions=2)
    kept = om._apply_exposure_gate([_cand("C", 50)], om.book)
    assert len(kept) == 1


def test_hard_blocks_when_premium_cap_hit():
    # 2 open @ entry=100, lot=500 -> Rs 100,000 deployed already.
    om = _om([_open("A", 100), _open("B", 100)], "hard", max_premium=150000.0)
    # candidate entry=200, lot=500 -> Rs 100,000 more -> total 200,000 > 150,000
    kept = om._apply_exposure_gate([_cand("C", 200)], om.book)
    assert kept == []


def test_hard_allows_under_premium_cap():
    om = _om([_open("A", 100)], "hard", max_premium=150000.0)
    kept = om._apply_exposure_gate([_cand("C", 50)], om.book)  # +25,000 -> 75,000 total
    assert len(kept) == 1


def test_running_totals_accumulate_within_one_batch():
    # No open positions yet, cap = 1 -> first candidate fills the only slot,
    # second candidate in the SAME batch must see that slot as taken.
    om = _om([], "hard", max_positions=1)
    kept = om._apply_exposure_gate([_cand("A", 50), _cand("B", 50)], om.book)
    assert len(kept) == 1 and kept[0]["symbol"] == "A"


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
