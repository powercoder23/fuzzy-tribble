# -*- coding: utf-8 -*-
"""Unit tests for the pure logic of the OI Buildup and Gap scanners.

Run:  python -m pytest test_new_scanners.py -q
"""

import oi_config
import oi_buildup_config as oib_cfg
import gap_scanner_config as gap_cfg
from oi_buildup_scanner import buyer_bias, is_flat
from gap_scanner import gap_pct, classify_gap


# ---- OI Buildup -------------------------------------------------------------
def test_buyer_bias_mapping():
    assert buyer_bias(oi_config.LONG_BUILDUP) == ("CE", "strong")
    assert buyer_bias(oi_config.SHORT_BUILDUP) == ("PE", "strong")
    assert buyer_bias(oi_config.SHORT_COVERING) == ("CE", "weak")
    assert buyer_bias(oi_config.LONG_UNWINDING) == ("PE", "weak")
    assert buyer_bias("FLAT") == ("-", "flat")


def test_is_flat_deadband():
    # both inside dead-band -> flat
    assert is_flat(oib_cfg.MIN_PRICE_CHANGE_PCT - 0.01, oib_cfg.MIN_OI_CHANGE_PCT - 0.01)
    # one clears it -> not flat
    assert not is_flat(oib_cfg.MIN_PRICE_CHANGE_PCT + 0.01, 0)
    assert not is_flat(0, oib_cfg.MIN_OI_CHANGE_PCT + 0.01)


# ---- Gap / Extreme Opening --------------------------------------------------
def test_gap_pct_basic():
    assert gap_pct(110, 100) == 10.0
    assert gap_pct(90, 100) == -10.0
    assert gap_pct(100, 0) is None
    assert gap_pct(100, None) is None


def test_classify_gap_up_breaks_range():
    # open 112 vs prev_close 100 = +12% gap, prev_high 105 -> breaks high
    direction, extreme = classify_gap(112, 100, 105, 95, require_range_break=True)
    assert direction == "GAP_UP"
    assert extreme is True


def test_classify_gap_up_within_range_not_extreme():
    # +2% gap but opens below prev_high 120 -> not extreme when range required
    direction, extreme = classify_gap(102, 100, 120, 90, require_range_break=True)
    if gap_cfg.GAP_PCT <= 2.0:
        assert direction == "GAP_UP"
        assert extreme is False
    else:
        assert direction == "NONE"


def test_classify_gap_down_breaks_low():
    direction, extreme = classify_gap(85, 100, 110, 90, require_range_break=True)
    assert direction == "GAP_DOWN"
    assert extreme is True


def test_classify_no_gap():
    direction, extreme = classify_gap(100.2, 100, 105, 95, require_range_break=True)
    assert direction == "NONE"
    assert extreme is False


def test_classify_gap_only_mode_ignores_range():
    direction, extreme = classify_gap(103, 100, 120, 90, require_range_break=False)
    if gap_cfg.GAP_PCT <= 3.0:
        assert direction == "GAP_UP"
        assert extreme is True


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failed else 0)
