# -*- coding: utf-8 -*-
"""Unit tests for Delivery-Surge and Smart-Money pure logic.

Run:  python -m pytest test_data_scanners.py -q
"""

import delivery_surge_config as ds_cfg
from delivery_surge_scanner import surge_ratio, qualifies
from smart_money_scanner import _parse_deal_date, net_bias


# ---- Delivery surge ---------------------------------------------------------
def test_surge_ratio():
    assert surge_ratio(60, 40) == 1.5
    assert surge_ratio(40, 40) == 1.0
    assert surge_ratio(50, 0) is None
    assert surge_ratio(50, None) is None


def test_qualifies_true():
    # 60% deliv vs 40% avg = 1.5x surge, price +2% -> qualifies (defaults: 1.5x, 45%, 1%)
    assert qualifies(60, 40, 2.0) is True


def test_qualifies_below_surge():
    assert qualifies(48, 45, 3.0) is False  # 1.07x < 1.5x


def test_qualifies_below_floor():
    # big surge ratio but absolute deliv% under the floor
    assert qualifies(30, 10, 5.0) is (30 >= ds_cfg.MIN_DELIV_PCT)  # floor blocks it


def test_qualifies_small_price_move():
    assert qualifies(70, 40, 0.2) is False  # price move under MIN_PRICE_CHANGE_PCT


# ---- Smart money ------------------------------------------------------------
def test_parse_deal_date():
    d = _parse_deal_date("01-Jun-2026")
    assert d is not None and d.year == 2026 and d.month == 6 and d.day == 1
    assert _parse_deal_date("2026-06-01") is not None
    assert _parse_deal_date("garbage") is None


def test_net_bias():
    assert net_bias(10.0) == "CE"
    assert net_bias(-10.0) == "PE"


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
