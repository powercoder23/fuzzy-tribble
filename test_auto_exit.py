# -*- coding: utf-8 -*-
"""Unit tests for the OI-contradiction auto-exit decision (pure, no DB/API),
plus the Sonar-reversal auto-exit (pure decision + OrderManager integration,
including the combo-partner-safety path shared with OI-contradiction)."""

import sys

from order_manager import oi_contradicts

# Default-ish thresholds mirroring auto_exit_config defaults.
KW = dict(min_oi_chg_pct=50, require_strong=True, max_profit_pct=10)


def test_short_buildup_against_ce_exits():
    # CE held; fresh PE-bias short buildup with big OI → exit.
    assert oi_contradicts("CE", "PE", "strong", 60, -8, **KW) is True


def test_long_buildup_against_pe_exits():
    assert oi_contradicts("PUT", "CE", "strong", 75, -5, **KW) is True


def test_agreeing_bias_holds():
    # CE held, OI also CE-biased → never exit.
    assert oi_contradicts("CE", "CE", "strong", 90, -8, **KW) is False


def test_below_oi_threshold_holds():
    assert oi_contradicts("CE", "PE", "strong", 30, -8, **KW) is False


def test_weak_buildup_holds_when_strong_required():
    # SHORT_COVERING / LONG_UNWINDING come through as strength="weak".
    assert oi_contradicts("CE", "PE", "weak", 80, -8, **KW) is False


def test_weak_allowed_when_strong_not_required():
    kw = dict(KW, require_strong=False)
    assert oi_contradicts("CE", "PE", "weak", 80, -8, **kw) is True


def test_clear_winner_is_not_dumped():
    # Up +25% — past max_profit_pct, so hold despite contradiction.
    assert oi_contradicts("CE", "PE", "strong", 80, 25, **KW) is False


def test_winner_dumped_when_guard_disabled():
    kw = dict(KW, max_profit_pct=1000)
    assert oi_contradicts("CE", "PE", "strong", 80, 25, **kw) is True


def test_flat_or_missing_bias_holds():
    assert oi_contradicts("CE", "-", "flat", 80, -8, **KW) is False
    assert oi_contradicts("CE", "", "strong", 80, -8, **KW) is False


def test_none_pnl_ignores_profit_guard():
    assert oi_contradicts("CE", "PE", "strong", 60, None, **KW) is True


def test_bad_oi_value_holds():
    assert oi_contradicts("CE", "PE", "strong", None, -8, **KW) is False


# --- Sonar-reversal: pure decision -------------------------------------------

from order_manager import sonar_reversal_contradicts


def test_breakdown_contradicts_ce():
    assert sonar_reversal_contradicts("CE", "BREAKDOWN") is True
    assert sonar_reversal_contradicts("CALL", "REVERSAL_DOWN") is True


def test_breakout_contradicts_pe():
    assert sonar_reversal_contradicts("PE", "BREAKOUT_UP") is True
    assert sonar_reversal_contradicts("PUT", "REVERSAL_UP") is True


def test_agreeing_signal_holds():
    assert sonar_reversal_contradicts("CE", "BREAKOUT_UP") is False
    assert sonar_reversal_contradicts("PE", "BREAKDOWN") is False


def test_neutral_or_unknown_signal_holds():
    assert sonar_reversal_contradicts("CE", "FLAT") is False
    assert sonar_reversal_contradicts("CE", "") is False


# --- Sonar-reversal: OrderManager integration (real book + fake sonar) ------

import os
import tempfile
from datetime import datetime

import order_manager
import paper_trader
from order_manager import OrderManager


def _cleanup(path):
    """Best-effort tempfile removal — see test_daily_loss.py's _cleanup for
    why this must never fail the test (Windows sqlite WAL handle timing)."""
    try:
        os.unlink(path)
    except OSError:
        pass


class _FakeScanner:
    def get_current_option_premium(self, *a, **k):
        return {"last": None}  # close_position falls back to trade.last_price


def _sonar_sig(symbol, entry, side="CE", combo_id=None, direction="long",
              security_id=None):
    return {"symbol": symbol, "security_id": security_id or symbol,
            "exchange_segment": "NSE_FNO", "side": side, "strike": 100.0,
            "expiry": "2026-07-30", "entry": entry, "sl": 1.0, "t1": 999.0,
            "t2": 999.0, "t1_book_fraction": 1.0, "lot_size": 500,
            "direction": direction, "combo_id": combo_id}


def _sonar_om(mode):
    order_manager._resolve_mode = lambda k, f: mode
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    book = paper_trader.PaperTradeBook(db_path=p)
    om = OrderManager(book=book)
    paper_trader.send_telegram = lambda *a, **k: True
    return om, p


def _patch_sonar(monkey):
    import sonar_laplace_scanner
    orig = sonar_laplace_scanner.get_latest_sonar
    sonar_laplace_scanner.get_latest_sonar = monkey
    return orig


def _unpatch_sonar(orig):
    import sonar_laplace_scanner
    sonar_laplace_scanner.get_latest_sonar = orig


def test_sonar_off_mode_never_closes():
    now = datetime(2026, 7, 9, 10, 0, 0)
    om, p = _sonar_om("off")
    orig = _patch_sonar(lambda sid: {"signal": "BREAKDOWN",
                                     "timestamp": "2026-07-09 09:55:00"})
    try:
        om.book.open_trade(_sonar_sig("AAA", 100), now=now)
        open_trades = om.book.open_trades(now.date().isoformat())
        closed = om._auto_exit_on_sonar_reversal(open_trades, _FakeScanner(), now)
        assert closed == []
        assert len(om.book.open_trades(now.date().isoformat())) == 1
    finally:
        _unpatch_sonar(orig)
        _cleanup(p)


def test_sonar_soft_mode_logs_but_does_not_close():
    now = datetime(2026, 7, 9, 10, 0, 0)
    om, p = _sonar_om("soft")
    orig = _patch_sonar(lambda sid: {"signal": "BREAKDOWN",
                                     "timestamp": "2026-07-09 09:55:00"})
    try:
        om.book.open_trade(_sonar_sig("AAA", 100), now=now)
        open_trades = om.book.open_trades(now.date().isoformat())
        closed = om._auto_exit_on_sonar_reversal(open_trades, _FakeScanner(), now)
        assert closed == []
        assert len(om.book.open_trades(now.date().isoformat())) == 1
    finally:
        _unpatch_sonar(orig)
        _cleanup(p)


def test_sonar_hard_mode_closes_contradicting_position():
    now = datetime(2026, 7, 9, 10, 0, 0)
    om, p = _sonar_om("hard")
    orig = _patch_sonar(lambda sid: {"signal": "BREAKDOWN",
                                     "timestamp": "2026-07-09 09:55:00"})
    try:
        om.book.open_trade(_sonar_sig("AAA", 100), now=now)
        open_trades = om.book.open_trades(now.date().isoformat())
        closed = om._auto_exit_on_sonar_reversal(open_trades, _FakeScanner(), now)
        assert len(closed) == 1
        assert closed[0]["exit_reason"] == "Sonar reversal (BREAKDOWN)"
        assert om.book.open_trades(now.date().isoformat()) == []
    finally:
        _unpatch_sonar(orig)
        _cleanup(p)


def test_sonar_hard_mode_skips_a_clear_winner():
    now = datetime(2026, 7, 9, 10, 0, 0)
    om, p = _sonar_om("hard")
    orig = _patch_sonar(lambda sid: {"signal": "BREAKDOWN",
                                     "timestamp": "2026-07-09 09:55:00"})
    try:
        om.book.open_trade(_sonar_sig("AAA", 100), now=now)
        trade = om.book.open_trades(now.date().isoformat())[0]
        trade["last_price"] = 200.0  # +100% — well past MAX_PROFIT_PCT (20)
        om.book.save_runtime(trade, now)
        open_trades = om.book.open_trades(now.date().isoformat())
        closed = om._auto_exit_on_sonar_reversal(open_trades, _FakeScanner(), now)
        assert closed == [], "a clear winner must not be dumped on a reversal alone"
        assert len(om.book.open_trades(now.date().isoformat())) == 1
    finally:
        _unpatch_sonar(orig)
        _cleanup(p)


def test_sonar_stale_signal_from_prior_session_is_ignored():
    now = datetime(2026, 7, 9, 10, 0, 0)
    om, p = _sonar_om("hard")
    orig = _patch_sonar(lambda sid: {"signal": "BREAKDOWN",
                                     "timestamp": "2026-07-08 15:25:00"})  # yesterday
    try:
        om.book.open_trade(_sonar_sig("AAA", 100), now=now)
        open_trades = om.book.open_trades(now.date().isoformat())
        closed = om._auto_exit_on_sonar_reversal(open_trades, _FakeScanner(), now)
        assert closed == []
        assert len(om.book.open_trades(now.date().isoformat())) == 1
    finally:
        _unpatch_sonar(orig)
        _cleanup(p)


def test_sonar_hard_mode_closes_combo_partner_together():
    # A hedge combo: long CE + short CE (same side, same combo_id) must exit
    # TOGETHER — this is the same combo-safety path OI-contradiction uses.
    now = datetime(2026, 7, 9, 10, 0, 0)
    om, p = _sonar_om("hard")
    orig = _patch_sonar(lambda sid: {"signal": "BREAKDOWN",
                                     "timestamp": "2026-07-09 09:55:00"})
    try:
        om.book.open_trade(_sonar_sig("AAA", 100, combo_id="C1", direction="long",
                                      security_id="AAA_L"), now=now)
        om.book.open_trade(_sonar_sig("AAA", 20, combo_id="C1", direction="short",
                                      security_id="AAA_S"), now=now)
        open_trades = om.book.open_trades(now.date().isoformat())
        assert len(open_trades) == 2
        closed = om._auto_exit_on_sonar_reversal(open_trades, _FakeScanner(), now)
        assert len(closed) == 2
        assert om.book.open_trades(now.date().isoformat()) == []
    finally:
        _unpatch_sonar(orig)
        _cleanup(p)


if __name__ == "__main__":
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
