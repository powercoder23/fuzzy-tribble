# -*- coding: utf-8 -*-
"""
order_manager.py — single owner of the open-position lifecycle.

Decouples *finding* a trade from *managing* it. A scanner/strategy SUBMITS a
booked signal here (`submit_signals`); the OrderManager then TRACKS and exits
every open position on its OWN cadence (`track`), independent of how often the
scanner runs. The discount scanner scans every 15 min, but the OrderManager
re-prices and exit-manages open trades every 5 min.

This is the thin first cut of the L4 OrderManager in ARCHITECTURE_REFACTOR_PLAN.md.
It currently delegates to the (already unit-tested) paper_trader engine, so no
exit logic is duplicated or changed. A live-broker backend can later implement
the same submit/track/eod surface without touching strategy code.
"""

from __future__ import annotations

import logging

import paper_trader

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Unified LIVE order path — single copy of the BUY -> SL_M -> emergency-exit
# sequence that momentum_strategy and break_bounce_strategy each duplicated.
# --------------------------------------------------------------------------- #
def place_bracket_order(dhan, strike_data: dict, lots: int, lot_size: int,
                        sl_price: float, notify=None, label: str = "") -> dict:
    """Market BUY an option, then immediately place an SL_M SELL. If the SL leg
    fails, fire an emergency market SELL and alert. Returns a status dict.

    `dhan`  : the broker adapter (scanner.dhan).
    `notify`: optional callable(str) for failure alerts (e.g. notifier.send).
    """
    try:
        option_sec_id = strike_data.get("option_security_id", "")
        if not option_sec_id:
            logger.error("%splace_bracket_order: no option_security_id", label)
            return {"status": "no_option_security_id"}

        qty = lots * lot_size

        response = dhan.place_order(
            security_id=option_sec_id, exchange_segment=dhan.NSE_FNO,
            transaction_type=dhan.BUY, quantity=qty,
            order_type=dhan.MARKET, product_type=dhan.INTRA, price=0,
        )
        logger.info("%sbuy order response: %s", label, response)
        if response.get("status") != "success":
            return {"status": "buy_failed", "response": response}

        sl_response = dhan.place_order(
            security_id=option_sec_id, exchange_segment=dhan.NSE_FNO,
            transaction_type=dhan.SELL, quantity=qty,
            order_type=dhan.SL_M, product_type=dhan.INTRA,