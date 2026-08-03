# -*- coding: utf-8 -*-
"""
bs_pricer.py — pure Black-Scholes pricer used to reconstruct option premium
paths for the backtest engine, since this system has no historical option
quote data (see backtest/README design note / project plan). Inputs are
real historical spot (candles) and real historical IV (iv_lookup.py); this
module just does the math.

No I/O, no config imports — a plain, independently-testable pricer.
"""

from __future__ import annotations

import math

RISK_FREE_RATE = 0.07  # flat assumption; not sourced from anywhere in-repo


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(spot: float, strike: float, t_years: float, r: float, sigma: float) -> tuple[float, float]:
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / (sigma * math.sqrt(t_years))
    d2 = d1 - sigma * math.sqrt(t_years)
    return d1, d2


def price(spot: float, strike: float, t_years: float, sigma_pct: float,
         opt_type: str, r: float = RISK_FREE_RATE) -> float | None:
    """Black-Scholes premium. sigma_pct is annualized IV in percent (e.g. 22.5
    for 22.5%), matching how this system stores atm_iv everywhere else.
    Returns None for degenerate inputs (expired, non-positive spot/strike/IV)
    rather than raising — callers treat None as 'can't price this'."""
    if spot is None or strike is None or sigma_pct is None:
        return None
    if spot <= 0 or strike <= 0 or sigma_pct <= 0 or t_years is None:
        return None
    if t_years <= 0:
        # At/after expiry: intrinsic value only.
        intrinsic = max(spot - strike, 0.0) if opt_type == "CE" else max(strike - spot, 0.0)
        return round(intrinsic, 2)

    sigma = sigma_pct / 100.0
    d1, d2 = _d1_d2(spot, strike, t_years, r, sigma)
    if opt_type == "CE":
        val = spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    else:
        val = strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return round(max(val, 0.0), 2)


def delta(spot: float, strike: float, t_years: float, sigma_pct: float,
         opt_type: str, r: float = RISK_FREE_RATE) -> float | None:
    """Black-Scholes delta, same input conventions as price()."""
    if spot is None or strike is None or sigma_pct is None:
        return None
    if spot <= 0 or strike <= 0 or sigma_pct <= 0 or t_years is None or t_years <= 0:
        return None
    sigma = sigma_pct / 100.0
    d1, _ = _d1_d2(spot, strike, t_years, r, sigma)
    return round(_norm_cdf(d1) if opt_type == "CE" else _norm_cdf(d1) - 1.0, 4)
