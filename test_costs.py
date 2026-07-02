# -*- coding: utf-8 -*-
"""Unit tests for costs.py — run with `python -m pytest test_costs.py -q`."""

import costs


def test_typical_round_trip_magnitude():
    # ₹50 entry, ₹55 exit, 1 lot of 500 shares -> ₹25k/₹27.5k turnover
    c = costs.option_trade_costs(buy_premium=50, sell_premium=55, qty=500, n_orders=2)
    assert c["brokerage"] == 40.0
    assert abs(c["stt"] - 27.5) < 0.01            # 0.1% of 27,500
    assert abs(c["exchange_txn"] - 18.39) < 0.05  # 0.03503% of 52,500
    assert 85 < c["total"] < 110                  # sanity band for the schedule
    # Costs must be a visible fraction of a +10% winner (2,500 gross)
    assert c["total"] / 2500 > 0.03


def test_partial_exit_adds_an_order():
    c2 = costs.option_trade_costs(50, 55, 500, n_orders=2)
    c3 = costs.option_trade_costs(50, 55, 500, n_orders=3)
    assert abs((c3["total"] - c2["total"]) - 20.0 * 1.18) < 0.01  # brokerage + GST


def test_zero_qty_is_free():
    assert costs.option_trade_costs(50, 55, 0)["total"] == 0.0
    assert costs.cost_in_points(50, 55, 0) == 0.0


def test_cost_in_points_consistency():
    c = costs.option_trade_costs(50, 55, 500)
    assert abs(costs.cost_in_points(50, 55, 500) * 500 - c["total"]) < 1e-6


def test_loser_still_pays_stt_on_sell():
    c = costs.option_trade_costs(buy_premium=50, sell_premium=42.5, qty=500)
    assert c["stt"] > 0
    assert c["total"] > 40  # never below brokerage
