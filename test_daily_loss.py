# -*- coding: utf-8 -*-
"""Tests for RISK-1 — the book-level daily-loss lockout (review 2026-07-09 §3.1).

Covers the pure P&L aggregator (book_day_pnl_rupees) and the OrderManager gate
(_daily_loss_locked) in off / soft / hard modes, with realized and marked-open
P&L. The gate's mode/limit resolvers are monkeypatched so the tests don't depend
on the shared settings DB or environment.
"""

import sys
from datetime import datetime

import order_manager
from order_manager import book_day_pnl_rupees, OrderManager


# --- fixtures ---------------------------------------------------------------

class _FakeBook:
    def __init__(self, trades):
        self._t = trades

    def all_trades(self, date):
        return self._t

    def open_trades(self, date):
        return [t for t in self._t if str(t.get("status")) == "open"]


def _closed(rupees):
    return {"status": "closed", "realized_rupees": rupees}


def _open(entry, last, lot, qty_frac=1.0, booked=0.0):
    return {"status": "open", "entry": entry, "last_price": last,
            "lot_size": lot, "qty_frac": qty_frac, "booked_points": booked}


def _om(trades, mode, limit):
    """OrderManager over a fake book with mode/limit forced (no DB / no alert)."""
    order_manager._resolve_mode = lambda k, f: mode
    order_manager._resolve_limit = lambda k, f: limit
    om = OrderManager(book=_FakeBook(trades))
    om._alert_daily_loss = lambda *a, **k: None   # never touch Telegram in tests
    return om


# --- pure aggregator --------------------------------------------------------

def test_realized_only_sum():
    assert book_day_pnl_rupees([_closed(-1000), _closed(400)], include_open=False) == -600


def test_marked_open_included():
    # entry 100, last 90, lot 50, full qty -> (90-100)*1*50 = -500
    t = [_open(100, 90, 50)]
    assert book_day_pnl_rupees(t, include_open=True) == -500
    assert book_day_pnl_rupees(t, include_open=False) == 0.0


def test_partial_booked_open():
    # booked +5 pts, remainder 0.3 marked at 90 (entry 100):
    # (5 + (-10)*0.3) * 100 = (5 - 3) * 100 = 200
    t = [_open(100, 90, 100, qty_frac=0.3, booked=5.0)]
    assert abs(book_day_pnl_rupees(t) - 200.0) < 1e-6


def test_aggregator_robust_to_missing_fields():
    assert book_day_pnl_rupees([{"status": "closed"}, {"status": "open"}]) == 0.0


# --- gate: off / soft / hard ------------------------------------------------

def test_no_lock_when_off():
    om = _om([_closed(-5000)], "off", 1000)
    locked, _ = om._daily_loss_locked(om.book)
    assert locked is False


def test_soft_logs_but_does_not_lock():
    om = _om([_closed(-5000)], "soft", 1000)
    locked, pnl = om._daily_loss_locked(om.book)
    assert locked is False and pnl == -5000


def test_hard_locks_when_breached():
    om = _om([_closed(-1200)], "hard", 1000)
    locked, pnl = om._daily_loss_locked(om.book)
    assert locked is True and pnl == -1200


def test_within_limit_not_locked():
    om = _om([_closed(-500)], "hard", 1000)
    locked, _ = om._daily_loss_locked(om.book)
    assert locked is False


def test_zero_limit_disables_guard():
    om = _om([_closed(-99999)], "hard", 0)
    locked, _ = om._daily_loss_locked(om.book)
    assert locked is False


# --- gate blocks the submit paths ------------------------------------------

def test_submit_signals_returns_empty_when_locked():
    om = _om([_closed(-2000)], "hard", 1000)
    opened = om.submit_signals([{"symbol": "X", "strike": 100, "type": "CE",
                                 "entry": 10, "t1": 12, "security_id": "1"}],
                               now=datetime(2026, 7, 9, 10, 0, 0))
    assert opened == []


def test_submit_external_signal_returns_none_when_locked():
    om = _om([_closed(-2000)], "hard", 1000)
    booked = om.submit_external_signal({"symbol": "Y", "side": "CE", "strike": 50,
                                        "entry": 8, "t1": 10, "security_id": "2"},
                                       now=datetime(2026, 7, 9, 10, 0, 0))
    assert booked is None


# --- flatten-on-breach (extends daily-loss into a real circuit breaker) -----

import os
import tempfile

import paper_trader


def _cleanup(path):
    """Best-effort tempfile removal. On Windows, a just-closed sqlite
    connection (WAL mode leaves -wal/-shm sidecars) can keep the file
    handle briefly locked even after the `with` block exits — that's a
    Windows-only OS timing quirk, not a bug in the feature under test, so
    cleanup failure here must never fail the test."""
    try:
        os.unlink(path)
    except OSError:
        pass


class _FakeScanner:
    """get_current_option_premium returns no usable quote -> close_position
    falls back to the trade's own last_price, so tests control P&L exactly
    by setting last_price directly instead of depending on quote plumbing."""
    def get_current_option_premium(self, *a, **k):
        return {"last": None}


def _sig(symbol, entry, sl=1.0, t1=999.0, lot_size=500):
    return {"symbol": symbol, "security_id": symbol, "exchange_segment": "NSE_FNO",
            "side": "CE", "strike": 100.0, "expiry": "2026-07-30",
            "entry": entry, "sl": sl, "t1": t1, "t2": t1,
            "t1_book_fraction": 1.0, "lot_size": lot_size}


def _open_marked_loss(book, symbol, entry, last_price, now):
    """Open a real trade then mark it to `last_price` (simulating a big
    unrealized move) via save_runtime, same path a real monitor tick uses."""
    book.open_trade(_sig(symbol, entry), now=now)
    today = now.date().isoformat()
    trade = [t for t in book.open_trades(today) if t["symbol"] == symbol][0]
    trade["last_price"] = last_price
    book.save_runtime(trade, now)


def _real_om(mode, flatten_on_breach, limit=1000.0):
    """OrderManager over a REAL (tempfile) PaperTradeBook — needed because
    the flatten path calls paper_trader.close_position(), which persists via
    book.save_runtime() (the _FakeBook above doesn't implement that)."""
    order_manager._resolve_mode = lambda k, f: mode
    order_manager._resolve_limit = lambda k, f: limit
    order_manager._resolve_bool = lambda k, f: flatten_on_breach
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    book = paper_trader.PaperTradeBook(db_path=p)
    om = OrderManager(book=book)
    om._alert_daily_loss = lambda *a, **k: None
    om._alert_flatten = lambda *a, **k: None
    paper_trader.send_telegram = lambda *a, **k: True
    return om, p


def test_flatten_noop_when_flag_off():
    now = datetime(2026, 7, 9, 10, 0, 0)
    om, p = _real_om("hard", flatten_on_breach=False)
    try:
        _open_marked_loss(om.book, "AAA", entry=100, last_price=50, now=now)
        closed = om._maybe_flatten_daily_loss(_FakeScanner(), now)
        assert closed == []
        assert len(om.book.open_trades(now.date().isoformat())) == 1
    finally:
        _cleanup(p)


def test_flatten_noop_in_soft_mode_even_with_flag_on():
    now = datetime(2026, 7, 9, 10, 0, 0)
    om, p = _real_om("soft", flatten_on_breach=True)
    try:
        _open_marked_loss(om.book, "AAA", entry=100, last_price=50, now=now)
        closed = om._maybe_flatten_daily_loss(_FakeScanner(), now)
        assert closed == []
        assert len(om.book.open_trades(now.date().isoformat())) == 1
    finally:
        _cleanup(p)


def test_flatten_noop_when_not_actually_breached():
    now = datetime(2026, 7, 9, 10, 0, 0)
    om, p = _real_om("hard", flatten_on_breach=True, limit=999999.0)
    try:
        _open_marked_loss(om.book, "AAA", entry=100, last_price=95, now=now)
        closed = om._maybe_flatten_daily_loss(_FakeScanner(), now)
        assert closed == []
        assert len(om.book.open_trades(now.date().isoformat())) == 1
    finally:
        _cleanup(p)


def test_flatten_closes_every_open_position_once():
    now = datetime(2026, 7, 9, 10, 0, 0)
    om, p = _real_om("hard", flatten_on_breach=True, limit=1000.0)
    try:
        _open_marked_loss(om.book, "AAA", entry=100, last_price=50, now=now)
        _open_marked_loss(om.book, "BBB", entry=100, last_price=60, now=now)
        closed = om._maybe_flatten_daily_loss(_FakeScanner(), now)
        assert len(closed) == 2
        assert om.book.open_trades(now.date().isoformat()) == []
        assert all(t["exit_reason"].startswith("Daily-loss breaker") for t in closed)

        # Dedup: a second tick the same day must not re-flatten (nothing left
        # open anyway, but this also guards a same-day re-entry scenario).
        _open_marked_loss(om.book, "CCC", entry=100, last_price=50, now=now)
        closed_again = om._maybe_flatten_daily_loss(_FakeScanner(), now)
        assert closed_again == []
        assert len(om.book.open_trades(now.date().isoformat())) == 1
    finally:
        _cleanup(p)


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
