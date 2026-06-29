# -*- coding: utf-8 -*-
"""Unit tests for the OI-contradiction auto-exit decision (pure, no DB/API)."""

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
