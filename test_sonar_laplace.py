# -*- coding: utf-8 -*-
"""Unit tests for sonar_laplace pure DSP/classify logic. No broker/DB."""

import sonar_laplace_scanner as s


def test_smoother_preserves_length_and_reduces_noise():
    import random
    random.seed(1)
    base = [100 + i * 0.5 for i in range(60)]               # uptrend
    noisy = [b + random.uniform(-3, 3) for b in base]
    sm = s.super_smoother(noisy, 12)
    assert len(sm) == len(noisy)
    # smoothed series should vary less step-to-step than the raw noisy one
    def avg_step(x): return sum(abs(x[i] - x[i-1]) for i in range(1, len(x))) / (len(x)-1)
    assert avg_step(sm) < avg_step(noisy)


def test_bands_order():
    series = [100 + i for i in range(30)]
    sm = s.super_smoother(series, 10)
    up, lo, mid = s.dynamic_bands(series, sm, 1.6)
    assert lo <= mid <= up


def test_classify_breakout_up():
    r = s.classify(prev_price=101, last_price=110, upper=105, lower=95, slope_p=0.3, min_slope=0.05)
    assert r["signal"] == "BREAKOUT_UP" and r["bias"] == "CE"


def test_classify_breakdown():
    r = s.classify(prev_price=99, last_price=90, upper=105, lower=95, slope_p=-0.3, min_slope=0.05)
    assert r["signal"] == "BREAKDOWN" and r["bias"] == "PE"


def test_classify_reversal_up():
    # was below lower (94), now crossed back above lower (96, lower=95)
    r = s.classify(prev_price=94, last_price=96, upper=110, lower=95, slope_p=0.0, min_slope=0.05)
    assert r["signal"] == "REVERSAL_UP" and r["bias"] == "CE"


def test_classify_inband_uses_trend():
    r = s.classify(prev_price=100, last_price=101, upper=110, lower=90, slope_p=0.2, min_slope=0.05)
    assert r["signal"] == "NONE" and r["trend"] == "UP" and r["bias"] == "CE"


def test_flat_when_slope_small():
    r = s.classify(prev_price=100, last_price=100.1, upper=110, lower=90, slope_p=0.01, min_slope=0.05)
    assert r["trend"] == "FLAT" and r["bias"] is None


if __name__ == "__main__":
    import sys
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
