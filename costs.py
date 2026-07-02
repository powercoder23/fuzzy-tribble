# -*- coding: utf-8 -*-
"""
costs.py — NSE options transaction-cost model (STRATEGY_REVIEW_P1.md §6.1).

Pure functions, no I/O. Every rate is env-overridable so a fee change is a
config bump, not a code edit. Defaults reflect the NSE/discount-broker
schedule as of mid-2026 — VERIFY against your broker's contract note and
update the env vars if they differ.

A typical ₹8-10k premium round trip costs ₹70-110 (~0.8-1.2% of premium)
before spread crossing. Paper P&L that ignores this overstates edge; the
paper trader calls `option_trade_costs` at trade finalization so realized
P&L is NET of costs.
"""

from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Rates (fractions of premium turnover unless noted)
# --------------------------------------------------------------------------- #
BROKERAGE_PER_ORDER = _f("COST_BROKERAGE_PER_ORDER", 20.0)   # ₹ flat per executed order
STT_SELL_PCT        = _f("COST_STT_SELL_PCT",        0.001)    # 0.1%  of SELL premium
EXCH_TXN_PCT        = _f("COST_EXCH_TXN_PCT",        0.0003503)  # 0.03503% both sides (NSE options)
SEBI_PCT            = _f("COST_SEBI_PCT",            0.000001)   # ₹10/crore
STAMP_BUY_PCT       = _f("COST_STAMP_BUY_PCT",       0.00003)    # 0.003% of BUY premium
IPFT_PCT            = _f("COST_IPFT_PCT",            0.000005)   # 0.0005% both sides
GST_PCT             = _f("COST_GST_PCT",             0.18)       # on brokerage + txn + SEBI + IPFT


def option_trade_costs(buy_premium: float, sell_premium: float, qty: int,
                       n_orders: int = 2) -> dict:
    """All-in cost (₹) for one long-option round trip.

    buy_premium / sell_premium : per-share premium actually paid / received
    qty                        : total shares (lots × lot_size)
    n_orders                   : executed orders (2 = single entry+exit;
                                 3 when T1 books a partial and the runner
                                 exits separately)

    Returns a breakdown dict; "total" is the number to subtract from gross P&L.
    """
    qty = max(int(qty or 0), 0)
    if qty == 0:
        return {"brokerage": 0.0, "stt": 0.0, "exchange_txn": 0.0, "sebi": 0.0,
                "stamp_duty": 0.0, "ipft": 0.0, "gst": 0.0, "total": 0.0,
                "buy_turnover": 0.0, "sell_turnover": 0.0}
    buy_turnover = max(float(buy_premium or 0), 0.0) * qty
    sell_turnover = max(float(sell_premium or 0), 0.0) * qty
    both = buy_turnover + sell_turnover

    brokerage = BROKERAGE_PER_ORDER * max(int(n_orders or 0), 0)
    stt = STT_SELL_PCT * sell_turnover
    exch = EXCH_TXN_PCT * both
    sebi = SEBI_PCT * both
    stamp = STAMP_BUY_PCT * buy_turnover
    ipft = IPFT_PCT * both
    gst = GST_PCT * (brokerage + exch + sebi + ipft)
    total = brokerage + stt + exch + sebi + stamp + ipft + gst

    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn": round(exch, 2),
        "sebi": round(sebi, 4),
        "stamp_duty": round(stamp, 4),
        "ipft": round(ipft, 4),
        "gst": round(gst, 2),
        "total": round(total, 2),
        "buy_turnover": round(buy_turnover, 2),
        "sell_turnover": round(sell_turnover, 2),
    }


def cost_in_points(buy_premium: float, sell_premium: float, qty: int,
                   n_orders: int = 2) -> float:
    """Total cost expressed in premium points per share (for point-based P&L)."""
    if not qty:
        return 0.0
    return option_trade_costs(buy_premium, sell_premium, qty, n_orders)["total"] / qty
