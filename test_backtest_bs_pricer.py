# -*- coding: utf-8 -*-
"""Regression tests for backtest/bs_pricer.py — the Black-Scholes premium
reconstruction the backtest engine uses in place of real historical option
quotes (see backtest/engine.py / project plan for why)."""

from backtest import bs_pricer


def test_atm_call_roughly_half_expected_move():
    # Well-known BS sanity check: an ATM call with modest IV/DTE prices to
    # roughly 0.4x the 1-sigma expected move (Brenner-Subrahmanyam), same
    # approximation engine/expected_move.py already uses live.
    spot, strike, t_years, sigma_pct = 1000.0, 1000.0, 7 / 365, 20.0
    premium = bs_pricer.price(spot, strike, t_years, sigma_pct, "CE")
    expected_move = spot * (sigma_pct / 100) * (t_years ** 0.5)
    assert 0.3 * expected_move < premium < 0.5 * expected_move


def test_put_call_parity_holds():
    spot, strike, t_years, sigma_pct, r = 1000.0, 980.0, 15 / 365, 25.0, 0.07
    call = bs_pricer.price(spot, strike, t_years, sigma_pct, "CE", r=r)
    put = bs_pricer.price(spot, strike, t_years, sigma_pct, "PE", r=r)
    import math
    lhs = call - put
    rhs = spot - strike * math.exp(-r * t_years)
    assert abs(lhs - rhs) < 0.05


def test_deep_itm_call_approaches_intrinsic():
    premium = bs_pricer.price(1200.0, 1000.0, 3 / 365, 15.0, "CE")
    intrinsic = 1200.0 - 1000.0
    assert premium >= intrinsic
    assert premium - intrinsic < 5.0  # little time value this close to expiry


def test_expired_option_prices_at_intrinsic():
    assert bs_pricer.price(1100.0, 1000.0, 0, 20.0, "CE") == 100.0
    assert bs_pricer.price(900.0, 1000.0, 0, 20.0, "PE") == 100.0
    assert bs_pricer.price(900.0, 1000.0, 0, 20.0, "CE") == 0.0


def test_degenerate_inputs_return_none_not_raise():
    assert bs_pricer.price(0, 100, 0.1, 20, "CE") is None
    assert bs_pricer.price(100, 0, 0.1, 20, "CE") is None
    assert bs_pricer.price(100, 100, 0.1, 0, "CE") is None
    assert bs_pricer.price(100, 100, 0.1, None, "CE") is None


def test_call_delta_between_zero_and_one():
    d = bs_pricer.delta(1000.0, 1000.0, 10 / 365, 20.0, "CE")
    assert 0.4 < d < 0.6  # ATM call delta is close to 0.5


def test_put_delta_between_minus_one_and_zero():
    d = bs_pricer.delta(1000.0, 1000.0, 10 / 365, 20.0, "PE")
    assert -0.6 < d < -0.4
