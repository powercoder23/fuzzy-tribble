# -*- coding: utf-8 -*-
"""Unit tests for the pure IV-rank math + zone classification.

Run:  python -m pytest test_iv_rank.py -q
"""

import iv_rank_config as cfg
from iv_rank_scanner import (
    iv_rank,
    iv_percentile,
    classify_zone,
    ZONE_CHEAP,
    ZONE_FAIR,
    ZONE_EXPENSIVE,
)


def test_iv_rank_basic():
    hist = [10, 20, 30, 40, 50]
    assert iv_rank(10, hist) == 0.0          # at the low
    assert iv_rank(50, hist) == 100.0        # at the high
    assert iv_rank(30, hist) == 50.0         # midpoint


def test_iv_rank_clips_out_of_range():
    hist = [10, 20, 30]
    assert iv_rank(5, hist) == 0.0           # below min -> clipped to 0
    assert iv_rank(40, hist) == 100.0        # above max -> clipped to 100


def test_iv_rank_flat_history():
    assert iv_rank(15, [15, 15, 15]) == 50.0  # max == min -> neutral 50


def test_iv_rank_empty():
    assert iv_rank(20, []) is None


def test_iv_percentile_basic():
    hist = [10, 20, 30, 40]
    assert iv_percentile(25, hist) == 50.0    # 2 of 4 below
    assert iv_percentile(5, hist) == 0.0      # none below
    assert iv_percentile(50, hist) == 100.0   # all below


def test_iv_percentile_empty():
    assert iv_percentile(20, []) is None


def test_classify_zone_thresholds():
    assert classify_zone(cfg.BUY_ZONE_MAX - 1) == ZONE_CHEAP
    assert classify_zone(cfg.BUY_ZONE_MAX) == ZONE_CHEAP
    assert classify_zone((cfg.BUY_ZONE_MAX + cfg.SELECTIVE_MAX) / 2) == ZONE_FAIR
    assert classify_zone(cfg.SELECTIVE_MAX + 1) == ZONE_EXPENSIVE


def test_classify_zone_none_is_fair():
    assert classify_zone(None) == ZONE_FAIR


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
