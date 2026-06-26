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
from datetime import datetime

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

    def __init__(self, book: "paper_trader.PaperTradeBook | None" = None,
                 bot_token: str | None = None, chat_id: str | None = None):
        self.book = book or paper_trader.PaperTradeBook()
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._warned: set = set()          # (trade_id, risk_type) — dedup intraday alerts
        self._warned_date: str | None = None

    # ---- intake: a scanner hands booked signals to the manager ------------- #
    def submit_signals(self, opportunities, now=None, lot_size_fn=None):
        """Book the top qualifying signals (caps / dedup / cutoff enforced by
        paper_trader.process_signals). Applies the pre-market quality gate
        (IVR / IV-HV / OTM% / PCR / position-cap) and the composite entry
        gate before reaching paper_trader. Returns the list of opened signals."""
        opportunities = self._apply_pre_market_gate(opportunities, self.book)
        opportunities = self._apply_entry_gate(opportunities)
        opened = paper_trader.process_signals(
            self.book, opportunities, now=now, lot_size_fn=lot_size_fn
        )
        if opened:
            logger.info("OrderManager: accepted %d new position(s)", len(opened))
        return opened

    def _apply_pre_market_gate(self, opportunities, book=None):
        """
        Apply the 5-gate pre-market quality filter before booking.

        Gates: IVR cap | IV/HV ratio | OTM% cap | PCR direction | position cap.
        Mode is config-driven (PMG_GATE_MODE env var):
          off  → always pass through unchanged
          soft → evaluate and log failures, never block
          hard → drop candidates that fail any gate

        Fail-open: any exception passes candidates through unchanged.
        Safe with DataFrame, list-of-dicts, or None.
        """
        try:
            import pre_market_gate
            import pre_market_gate_config as pmg_cfg

            if pmg_cfg.GATE_MODE == "off" or opportunities is None:
                return opportunities

            rows = (
                opportunities.to_dict("records")
                if hasattr(opportunities, "to_dict")
                else list(opportunities)
            )

            # Current open positions — gate 5 uses this as the base count.
            today = datetime.now().date().isoformat()
            open_count = len(book.open_trades(today)) if book else 0

            kept = []
            accepted_this_batch = 0   # running tally within this submit call

            for r in rows:
                result = pre_market_gate.evaluate(
                    security_id   = r.get("security_id"),
                    symbol        = r.get("symbol", ""),
                    side          = r.get("side") or r.get("type", ""),
                    spot          = r.get("spot"),
                    strike        = r.get("strike"),
                    iv            = r.get("iv"),
                    hv            = r.get("hv"),
                    iv_rank       = r.get("iv_rank"),
                    open_positions= open_count + accepted_this_batch,
                )
                if result["allow"]:
                    kept.append(r)
                    accepted_this_batch += 1
                else:
                    logger.info(
                        "OrderManager: pre_market_gate blocked %s %s — %s",
                        r.get("symbol"), r.get("side") or r.get("type"),
                        result["reason"],
                    )

            dropped = len(rows) - len(kept)
            if dropped:
                logger.info(
                    "OrderManager: pre_market_gate dropped %d / %d candidate(s)",
                    dropped, len(rows),
                )
            return kept

        except Exception:
            logger.exception("pre_market_gate failed; passing candidates through unchanged")
            return opportunities

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
        closed = paper_trader.monitor(
            self.book, scanner, now=now, square_off=square_off,
            bot_token=self.bot_token, chat_id=self.chat_id,
        )
        if closed:
            logger.info("OrderManager: closed %d position(s) this tick", len(closed))
        # Risk alerts on surviving open positions (OI contradiction + Sonar reversal).
        if not square_off:
            today = (now or datetime.now()).date().isoformat()
            self._check_position_risks(self.book.open_trades(today))
        return closed

    def _check_position_risks(self, open_trades: list) -> None:
        """
        Check each open position for OI contradiction and Sonar reversal.

        Fires a Telegram warning the FIRST time each risk is detected per trade.
        Deduplicates within the trading day via self._warned so the same risk
        does not spam on every 5-min tick.

        Signals checked (zero broker calls — reads iv_history.db only):
          • OI buildup contradiction: position is CE but OI building PE (or vice-versa)
          • Sonar reversal: Sonar shows BREAKDOWN/REVERSAL_DOWN on a CE position
            or BREAKOUT_UP/REVERSAL_UP on a PE position
        """
        if not open_trades:
            return

        today = datetime.now().date().isoformat()
        if self._warned_date != today:
            self._warned = set()
            self._warned_date = today

        try:
            import sqlite3
            import notifications
            from collectors import iv_store

            for trade in open_trades:
                tid    = trade.get("id")
                symbol = trade.get("symbol", "")
                side   = "CE" if str(trade.get("side", "")).upper() in ("CE", "CALL") else "PE"
                sec_id = str(trade.get("security_id") or "")
                entry  = float(trade.get("entry") or 0)
                _lp    = trade.get("last_price")
                last   = float(_lp if _lp is not None else entry)
                strike = trade.get("strike", 0)
                risk_lines = []

                # ── OI contradiction ─────────────────────────────────────── #
                try:
                    with sqlite3.connect(iv_store.DB_PATH) as conn:
                        row = conn.execute(
                            """SELECT bias, classification, price_chg_pct, oi_chg_pct
                               FROM oi_buildup_history
                               WHERE symbol = ?
                                 AND date(timestamp) = date('now', 'localtime')
                                 AND bias NOT IN ('-', 'FLAT', '')
                               ORDER BY timestamp DESC LIMIT 1""",
                            (symbol,),
                        ).fetchone()
                    if row:
                        oi_bias, oi_class, px_chg, oi_chg = row
                        if oi_bias and oi_bias != side:
                            key = (tid, f"oi_{oi_bias}")
                            if key not in self._warned:
                                risk_lines.append(
                                    f"📊 OI {oi_class} (px {float(px_chg or 0):+.1f}%"
                                    f" | OI {float(oi_chg or 0):+.1f}%)"
                                    f" → {oi_bias} bias vs your {side}"
                                )
                                self._warned.add(key)
                except Exception:
                    logger.debug("OI risk check failed for %s", symbol)

                # ── Sonar reversal ───────────────────────────────────────── #
                try:
                    from sonar_laplace_scanner import get_latest_sonar
                    sonar    = get_latest_sonar(sec_id) if sec_id else {}
                    # Discard stale signals from previous sessions
                    if sonar.get("timestamp", "")[:10] != today:
                        sonar = {}
                    s_bias   = sonar.get("bias")
                    s_signal = sonar.get("signal", "")
                    bearish  = {"BREAKDOWN", "REVERSAL_DOWN"}
                    bullish  = {"BREAKOUT_UP", "REVERSAL_UP"}
                    contra   = (side == "CE" and s_signal in bearish) or \
                               (side == "PE" and s_signal in bullish)
                    if contra:
                        key = (tid, f"sonar_{s_signal}")
                        if key not in self._warned:
                            risk_lines.append(
                                f"📡 Sonar {s_signal} → {s_bias}"
                                f" | last {sonar.get('last') or 0:.1f}"
                                f" (S {sonar.get('support') or 0}"
                                f" / R {sonar.get('resistance') or 0})"
                            )
                            self._warned.add(key)
                except Exception:
                    logger.debug("Sonar risk check failed for %s", symbol)

                if risk_lines:
                    pnl_pct  = ((last - entry) / entry * 100) if entry else 0
                    pnl_sign = "+" if pnl_pct >= 0 else ""
                    msg = (
                        f"🚨 <b>POSITION RISK</b> • <b>{symbol}</b> {side} {int(strike)}\n"
                        + "\n".join(risk_lines)
                        + f"\nEntry ₹{entry:.2f} | Now ₹{last:.2f}"
                        f" ({pnl_sign}{pnl_pct:.1f}%) — consider early exit"
                    )
                    notifications.notify(msg, bot_token=self.bot_token, chat_id=self.chat_id)
                    logger.info(
                        "Position risk alert: %s %s — %s",
                        symbol, side, "; ".join(risk_lines),
                    )

        except Exception:
            logger.exception("_check_position_risks failed (non-fatal)")

    def square_off_all(self, scanner, now=None):
        """Force-close all remaining open positions (square-off time)."""
        return paper_trader.monitor(self.book, scanner, now=now, square_off=True)

    def eod(self, scanner=None, now=None):
        """Square off stragglers and send the realized-P&L summary."""
        return paper_trader.run_eod(self.book, scanner, now=now)
