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
            order_type=dhan.SL_M, product_type=dhan.INTRA, price=0,
            trigger_price=sl_price,
        )
        if sl_response.get("status") != "success":
            logger.error("%sSL order failed — placing emergency market sell: %s", label, sl_response)
            dhan.place_order(
                security_id=option_sec_id, exchange_segment=dhan.NSE_FNO,
                transaction_type=dhan.SELL, quantity=qty,
                order_type=dhan.MARKET, product_type=dhan.INTRA, price=0,
            )
            if notify:
                try:
                    notify(f"⚠️ {label}SL order failed for {strike_data.get('side')} "
                           f"{strike_data.get('strike')} — emergency exit placed")
                except Exception:
                    logger.exception("notify failed")
            return {"status": "sl_failed_emergency_exit"}

        return {
            "status": "ok",
            "buy_order_id": response.get("orderId", ""),
            "sl_order_id": sl_response.get("orderId", ""),
        }
    except Exception:
        logger.exception("%splace_bracket_order exception", label)
        return {"status": "exception"}


class OrderManager:
    """Owns the trade book and the open-position lifecycle (paper backend)."""

    def __init__(self, book: "paper_trader.PaperTradeBook | None" = None):
        self.book = book or paper_trader.PaperTradeBook()

    # ---- intake: a scanner hands booked signals to the manager ------------- #
    def submit_signals(self, opportunities, now=None, lot_size_fn=None):
        """Book the top qualifying signals (caps / dedup / cutoff enforced by
        paper_trader.process_signals). Applies the composite entry gate first
        (no-op unless GATE_MODE=hard). Returns the list of opened signals."""
        opportunities = self._apply_entry_gate(opportunities)
        opened = paper_trader.process_signals(
            self.book, opportunities, now=now, lot_size_fn=lot_size_fn
        )
        if opened:
            logger.info("OrderManager: accepted %d new position(s)", len(opened))
        return opened

    @staticmethod
    def _apply_entry_gate(opportunities):
        """In GATE_MODE=hard, drop candidates the composite gate rejects.
        off/soft -> unchanged (fail-open). Safe with DataFrame, list, or None."""
        try:
            import entry_gate, entry_gate_config
            if entry_gate_config.GATE_MODE != "hard" or opportunities is None:
                return opportunities
            rows = opportunities.to_dict("records") if hasattr(opportunities, "to_dict") else list(opportunities)
            kept = [r for r in rows
                    if entry_gate.passes(r.get("security_id"), r.get("type") or r.get("side"))]
            dropped = len(rows) - len(kept)
            if dropped:
                logger.info("OrderManager: entry gate dropped %d candidate(s)", dropped)
            return kept
        except Exception:
            logger.exception("entry gate failed; passing candidates through")
            return opportunities

    # ---- lifecycle: manager re-prices + exits ALL open positions ----------- #
    def track(self, scanner, now=None, square_off=False):
        """Re-price every open position and advance its exit state machine.
        Called on the OrderManager's own (faster) cadence."""
        closed = paper_trader.monitor(self.book, scanner, now=now, square_off=square_off)
        if closed:
            logger.info("OrderManager: closed %d position(s) this tick", len(closed))
        return closed

    def square_off_all(self, scanner, now=None):
        """Force-close all remaining open positions (square-off time)."""
        return paper_trader.monitor(self.book, scanner, now=now, square_off=True)

    def eod(self, scanner=None, now=None):
        """Square off stragglers and send the realized-P&L summary."""
        return paper_trader.run_eod(self.book, scanner, now=now)
