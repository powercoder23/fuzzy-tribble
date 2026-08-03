# -*- coding: utf-8 -*-
"""Unit tests for the trailing stop-loss ratchet (_apply_trailing, exercised
via apply_tick — pure, no DB/API). trailing_config is monkeypatched per test
so nothing depends on env vars / the shared settings DB."""

import sys
from contextlib import contextmanager

import trailing_config
from paper_trader import new_trade_runtime, apply_tick


@contextmanager
def _trailing(enabled, activation_pct=0.20, giveback_pct=0.15):
    orig = (trailing_config.ENABLED, trailing_config.ACTIVATION_PCT, trailing_config.GIVEBACK_PCT)
    trailing_config.ENABLED = enabled
    trailing_config.ACTIVATION_PCT = activation_pct
    trailing_config.GIVEBACK_PCT = giveback_pct
    try:
        yield
    finally:
        trailing_config.ENABLED, trailing_config.ACTIVATION_PCT, trailing_config.GIVEBACK_PCT = orig


def test_disabled_by_default_is_a_no_op():
    with _trailing(enabled=False):
        t = new_trade_runtime(entry=100, sl=85, t1=130, t2=130, t1_book_fraction=1.0, lot_size=500)
        apply_tick(t, 125)
        assert t["sl"] == 85, "SL must stay at the fixed level when trailing is off"
        assert t["status"] == "open"


def test_below_activation_leaves_sl_unchanged():
    with _trailing(enabled=True, activation_pct=0.20, giveback_pct=0.15):
        t = new_trade_runtime(entry=100, sl=85, t1=130, t2=130, t1_book_fraction=1.0, lot_size=500)
        apply_tick(t, 110)  # +10%, below the 20% activation
        assert t["sl"] == 85
        assert t["status"] == "open"


def test_activates_and_ratchets_sl_up_long():
    with _trailing(enabled=True, activation_pct=0.20, giveback_pct=0.15):
        t = new_trade_runtime(entry=100, sl=85, t1=130, t2=130, t1_book_fraction=1.0, lot_size=500)
        apply_tick(t, 125)  # +25%, past activation
        # peak=125, giveback=(125-100)*0.15=3.75 -> candidate=121.25
        assert abs(t["sl"] - 121.25) < 1e-9, t["sl"]
        assert t["status"] == "open"


def test_ratcheted_sl_locks_in_gain_on_pullback():
    with _trailing(enabled=True, activation_pct=0.20, giveback_pct=0.15):
        t = new_trade_runtime(entry=100, sl=85, t1=130, t2=130, t1_book_fraction=1.0, lot_size=500)
        apply_tick(t, 125)                    # ratchets sl to 121.25
        ev = apply_tick(t, 110)               # well above the ORIGINAL sl (85)...
        # ...but below the ratcheted sl (121.25), so it must still stop out.
        assert ev == ["SL"], ev
        assert t["status"] == "closed"
        assert t["exit_reason"] == "SL"
        # gap-aware fill: min(sl, observed) = min(121.25, 110) = 110
        assert abs(t["gross_points"] - 10.0) < 1e-6, t["gross_points"]


def test_sl_only_tightens_never_loosens():
    with _trailing(enabled=True, activation_pct=0.20, giveback_pct=0.15):
        t = new_trade_runtime(entry=100, sl=85, t1=200, t2=200, t1_book_fraction=1.0, lot_size=500)
        apply_tick(t, 140)                    # ratchets sl up to 134 (peak=140, giveback=6)
        sl_after_first = t["sl"]
        apply_tick(t, 136)                    # pulls back, but stays above the ratcheted sl (134)
        assert t["status"] == "open", "136 must not hit an sl ratcheted to 134"
        assert t["sl"] == sl_after_first, "a pullback that doesn't hit SL must not loosen it"


def test_clips_at_target_instead_of_overshooting():
    with _trailing(enabled=True, activation_pct=0.01, giveback_pct=0.0):
        # A same-tick gap straight past a near target: without clipping the
        # candidate SL (105 - 0 giveback = 105) would sit ABOVE the target
        # (104), which would be nonsensical (SL > target for a long). It
        # must clip to the target instead, and the tick still resolves as a
        # TARGET hit (not a same-tick SL mislabel).
        t = new_trade_runtime(entry=100, sl=85, t1=104, t2=104, t1_book_fraction=1.0, lot_size=500)
        ev = apply_tick(t, 105)
        assert ev == ["TARGET"], ev
        assert t["status"] == "closed"
        assert t["exit_reason"] == "Target"


def test_short_side_mirrors_long():
    with _trailing(enabled=True, activation_pct=0.20, giveback_pct=0.15):
        t = new_trade_runtime(entry=100, sl=115, t1=70, t2=70, t1_book_fraction=1.0,
                              lot_size=500, direction="short")
        apply_tick(t, 80)   # premium decayed 20% in the seller's favor
        # peak(min)=80, giveback=|80-100|*0.15=3 -> candidate=max(83,70)=83
        assert abs(t["sl"] - 83) < 1e-9, t["sl"]
        assert t["status"] == "open"


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
