# -*- coding: utf-8 -*-
"""
Break and Bounce Strategy — Strategy 4.

Three-step process:
  Step 1 (Daily):  Mark yesterday's candle high/low as key levels (the "blueprint").
  Step 2 (15-min): Wait for a 15-min candle CLOSE above/below the level.
                   Must occur within 9:15–11:45 (first 2.5 hours). Otherwise void.
  Step 3 (5-min):  After breakout, wait for price to retest the breakout level.
                   Enter on:
                     - Hammer / Inverted Hammer → at candle close, SL at wick extreme
                     - Bullish / Bearish Engulfing → at prev candle high/low,
                       SL beyond engulfing candle extreme
                   Target: 2.5× SL distance. Force close at 15:15.
"""

import csv
import logging
import os
import time
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path

import pandas as pd
import numpy as np
import pytz
import requests

from discount import DiscountedPremiumScanner, unwrap_dhan_payload, get_trading_days_to_expiry
from token_manager import TokenManager
from config import Config
import iv_store
from momentum_strategy import (
    ScripMasterLotSizer,
    MomentumRegimeFilter,
    MomentumScanner,
    AffordabilityFilter,
    MomentumTradeJournal,
)
from break_bounce_config import (
    CAPITAL, BB_RISK, BB_BREAKOUT, BB_LIQUIDITY, BB_STRIKE,
    SCRIP_MASTER_DB, TRADE_LOG_PATH, LOT_SIZE_FALLBACK,
)

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLASS 1: BreakBounceRiskManager
# ---------------------------------------------------------------------------

class BreakBounceRiskManager:

    def __init__(self, capital: float = CAPITAL):
        self.capital        = capital
        self.daily_pnl      = 0.0
        self.trades_today   = 0
        self.open_positions: list = []

    def max_risk(self) -> float:
        return self.capital * BB_RISK["max_risk_pct"]

    def can_trade(self) -> tuple:
        daily_loss_limit = -(self.capital * BB_RISK["daily_loss_limit_pct"])
        if self.daily_pnl <= daily_loss_limit:
            return False, f"daily_loss_limit_hit(pnl={self.daily_pnl:.0f})"
        if self.trades_today >= BB_RISK["max_trades_per_day"]:
            return False, f"max_trades_reached({self.trades_today})"
        if len(self.open_positions) >= BB_RISK["max_open_positions"]:
            return False, f"max_positions_open({len(self.open_positions)})"
        return True, "OK"

    def calculate_lots(self, atm_premium: float, lot_size: int) -> int:
        if atm_premium <= 0 or lot_size <= 0:
            return 0
        risk_per_lot = atm_premium * BB_RISK["sl_pct"] * lot_size
        return max(0, int(self.max_risk() / risk_per_lot))

    def is_affordable(self, atm_premium: float, lot_size: int) -> bool:
        return self.calculate_lots(atm_premium, lot_size) >= 1

    def sl_price(self, premium: float) -> float:
        return round(premium * (1 - BB_RISK["sl_pct"]), 1)

    def target_price(self, premium: float) -> float:
        sl_amt = premium * BB_RISK["sl_pct"]
        return round(premium + sl_amt * BB_RISK["target_ratio"], 1)

    def record_trade(self, pnl: float = 0.0) -> None:
        self.daily_pnl    += pnl
        self.trades_today += 1

    def add_position(self, position: dict) -> None:
        self.open_positions.append(position)

    def remove_position(self, symbol: str) -> None:
        self.open_positions = [p for p in self.open_positions
                               if p.get("symbol") != symbol]

    def reset_daily(self) -> None:
        self.daily_pnl    = 0.0
        self.trades_today = 0

    def summary(self) -> dict:
        return {
            "capital":        self.capital,
            "daily_pnl":      round(self.daily_pnl, 2),
            "daily_pnl_pct":  round(self.daily_pnl / self.capital * 100, 2),
            "trades_today":   self.trades_today,
            "open_positions": len(self.open_positions),
            "daily_limit":    round(self.capital * BB_RISK["daily_loss_limit_pct"], 2),
            "max_trades":     BB_RISK["max_trades_per_day"],
        }


# ---------------------------------------------------------------------------
# CLASS 2: BreakBounceScanner
# ---------------------------------------------------------------------------

class BreakBounceScanner:
    """
    Core logic for Break and Bounce.

    - get_yesterday_levels: fetch yesterday's daily high/low (Step 1)
    - check_15min_breakout: detect 15-min close above/below level (Step 2)
    - check_5min_entry:     detect reversal pattern at retest (Step 3)
    """

    def __init__(self, scanner: DiscountedPremiumScanner):
        self.scanner           = scanner
        self._daily_fetcher    = MomentumRegimeFilter(scanner)
        self._intraday_fetcher = MomentumScanner(scanner)

    # ---- Step 1: Daily levels --------------------------------------------------

    def get_yesterday_levels(self, security_id, exchange_segment) -> dict:
        """Return yesterday's daily candle high and low."""
        empty = {"yesterday_high": 0.0, "yesterday_low": 0.0, "date": ""}
        df = self._daily_fetcher.get_daily_candles(security_id, exchange_segment, days=5)
        if df is None or df.empty:
            return empty
        # df sorted ascending; iloc[-1] = most recent complete daily candle = yesterday
        yesterday = df.iloc[-1]
        return {
            "yesterday_high": float(yesterday["high"]),
            "yesterday_low":  float(yesterday["low"]),
            "date":           str(yesterday["date"]),
        }

    # ---- Step 2: 15-min breakout -----------------------------------------------

    def check_15min_breakout(self, security_id, exchange_segment, symbol,
                             yesterday_levels: dict) -> dict:
        """
        Scan today's completed 15-min candles for a close above yesterday_high
        or below yesterday_low. Must be within 9:15–11:45 window.
        """
        no_breakout = {"direction": "NONE", "breakout_level": 0.0, "reason": ""}
        try:
            now = datetime.now(IST)
            window_end = now.replace(
                hour=BB_BREAKOUT["window_end_hour"],
                minute=BB_BREAKOUT["window_end_min"],
                second=0, microsecond=0,
            )
            if now > window_end:
                return {**no_breakout, "reason": "window_expired"}

            yh = yesterday_levels.get("yesterday_high", 0.0)
            yl = yesterday_levels.get("yesterday_low", 0.0)
            if not yh or not yl:
                return {**no_breakout, "reason": "no_levels"}

            df = self._intraday_fetcher.get_intraday_candles(
                security_id, exchange_segment, interval_minutes=15)
            if df is None or df.empty or len(df) < 2:
                return {**no_breakout, "reason": "insufficient_candles"}

            today_str = date.today().isoformat()
            df["_date"] = pd.to_datetime(df["datetime"]).dt.date.astype(str)
            today_df = df[df["_date"] == today_str]
            if today_df.empty:
                return {**no_breakout, "reason": "no_today_candles"}

            market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)

            # Exclude the live in-progress candle (last row)
            completed = today_df.iloc[:-1] if len(today_df) > 1 else pd.DataFrame()
            if completed.empty:
                return {**no_breakout, "reason": "no_completed_candles"}

            for _, candle in completed.iterrows():
                candle_dt = pd.to_datetime(candle["datetime"])
                if candle_dt.tzinfo is None:
                    candle_dt = IST.localize(candle_dt)
                if candle_dt < market_open or candle_dt > window_end:
                    continue

                close = float(candle["close"])
                if close > yh:
                    return {
                        "direction":      "BULLISH",
                        "breakout_level": yh,
                        "candle_close":   round(close, 2),
                        "candle_time":    str(candle["datetime"]),
                        "reason":         "closed_above_daily_high",
                    }
                if close < yl:
                    return {
                        "direction":      "BEARISH",
                        "breakout_level": yl,
                        "candle_close":   round(close, 2),
                        "candle_time":    str(candle["datetime"]),
                        "reason":         "closed_below_daily_low",
                    }

            return {**no_breakout, "reason": "no_breakout_yet"}

        except Exception:
            logger.exception("check_15min_breakout failed | %s", symbol)
            return {**no_breakout, "reason": "exception"}

    # ---- Step 3: 5-min entry at retest ----------------------------------------

    def check_5min_entry(self, security_id, exchange_segment, symbol,
                         breakout_direction: str, breakout_level: float) -> dict:
        """
        After breakout confirmed, look for reversal pattern at retest on 5-min.

        BULLISH breakout → expect price to pull back to breakout level (support).
                           Look for Hammer or Bullish Engulfing → signal CE.

        BEARISH breakout → expect price to bounce back to breakout level (resistance).
                           Look for Inverted Hammer or Bearish Engulfing → signal PE.
        """
        no_signal = {
            "signal": "NONE", "pattern": "", "symbol": symbol,
            "security_id": security_id, "sl_level": 0.0, "reason": "",
        }
        try:
            df = self._intraday_fetcher.get_intraday_candles(
                security_id, exchange_segment, interval_minutes=5)
            if df is None or df.empty or len(df) < 3:
                return {**no_signal, "reason": "insufficient_candles"}

            today_str = date.today().isoformat()
            df["_date"] = pd.to_datetime(df["datetime"]).dt.date.astype(str)
            today_df = df[df["_date"] == today_str]
            if len(today_df) < 3:
                return {**no_signal, "reason": "insufficient_today_candles"}

            last = today_df.iloc[-2]   # most recent completed 5-min candle
            prev = today_df.iloc[-3]   # candle before that

            tol   = BB_BREAKOUT["retest_tol_pct"]
            level = breakout_level

            if breakout_direction == "BULLISH":
                # Price retests level from above — low of candle touches level
                at_level = abs(float(last["low"]) - level) / max(level, 1e-6) <= tol
                if at_level:
                    if self._is_hammer(last):
                        return {
                            "signal":      "CE",
                            "pattern":     "HAMMER",
                            "symbol":      symbol,
                            "security_id": security_id,
                            "entry_price": round(float(last["close"]), 2),
                            "sl_level":    round(float(last["low"]), 2),
                            "candle_time": str(last["datetime"]),
                            "reason":      "hammer_at_retest",
                        }
                    if self._is_bullish_engulfing(prev, last):
                        return {
                            "signal":      "CE",
                            "pattern":     "BULLISH_ENGULFING",
                            "symbol":      symbol,
                            "security_id": security_id,
                            # Enter at high of previous (bearish) candle
                            "entry_price": round(float(prev["high"]), 2),
                            "sl_level":    round(float(last["low"]), 2),
                            "candle_time": str(last["datetime"]),
                            "reason":      "bullish_engulfing_at_retest",
                        }

            elif breakout_direction == "BEARISH":
                # Price retests level from below — high of candle touches level
                at_level = abs(float(last["high"]) - level) / max(level, 1e-6) <= tol
                if at_level:
                    if self._is_inverted_hammer(last):
                        return {
                            "signal":      "PE",
                            "pattern":     "INVERTED_HAMMER",
                            "symbol":      symbol,
                            "security_id": security_id,
                            "entry_price": round(float(last["close"]), 2),
                            "sl_level":    round(float(last["high"]), 2),
                            "candle_time": str(last["datetime"]),
                            "reason":      "inverted_hammer_at_retest",
                        }
                    if self._is_bearish_engulfing(prev, last):
                        return {
                            "signal":      "PE",
                            "pattern":     "BEARISH_ENGULFING",
                            "symbol":      symbol,
                            "security_id": security_id,
                            # Enter at low of previous (bullish) candle
                            "entry_price": round(float(prev["low"]), 2),
                            "sl_level":    round(float(last["high"]), 2),
                            "candle_time": str(last["datetime"]),
                            "reason":      "bearish_engulfing_at_retest",
                        }

            return {**no_signal, "reason": "no_pattern_at_level"}

        except Exception:
            logger.exception("check_5min_entry failed | %s", symbol)
            return {**no_signal, "reason": "exception"}

    # ---- Candle pattern helpers ------------------------------------------------

    @staticmethod
    def _is_hammer(candle: pd.Series) -> bool:
        """Long lower wick, small body. Bullish reversal at support."""
        open_ = float(candle["open"])
        close = float(candle["close"])
        high  = float(candle["high"])
        low   = float(candle["low"])
        body  = abs(close - open_)
        if body < 1e-6:
            return False
        lower_wick = min(open_, close) - low
        upper_wick = high - max(open_, close)
        wr = BB_BREAKOUT["hammer_wick_ratio"]
        mc = BB_BREAKOUT["max_counter_wick"]
        return lower_wick >= wr * body and upper_wick <= mc * body

    @staticmethod
    def _is_inverted_hammer(candle: pd.Series) -> bool:
        """Long upper wick, small body. Bearish reversal at resistance (shooting star)."""
        open_ = float(candle["open"])
        close = float(candle["close"])
        high  = float(candle["high"])
        low   = float(candle["low"])
        body  = abs(close - open_)
        if body < 1e-6:
            return False
        lower_wick = min(open_, close) - low
        upper_wick = high - max(open_, close)
        wr = BB_BREAKOUT["hammer_wick_ratio"]
        mc = BB_BREAKOUT["max_counter_wick"]
        return upper_wick >= wr * body and lower_wick <= mc * body

    @staticmethod
    def _is_bullish_engulfing(prev: pd.Series, curr: pd.Series) -> bool:
        """curr bullish candle fully engulfs prev bearish candle body."""
        prev_open  = float(prev["open"])
        prev_close = float(prev["close"])
        curr_open  = float(curr["open"])
        curr_close = float(curr["close"])
        if prev_close >= prev_open:   # prev must be bearish
            return False
        if curr_close <= curr_open:   # curr must be bullish
            return False
        return curr_open <= prev_close and curr_close >= prev_open

    @staticmethod
    def _is_bearish_engulfing(prev: pd.Series, curr: pd.Series) -> bool:
        """curr bearish candle fully engulfs prev bullish candle body."""
        prev_open  = float(prev["open"])
        prev_close = float(prev["close"])
        curr_open  = float(curr["open"])
        curr_close = float(curr["close"])
        if prev_close <= prev_open:   # prev must be bullish
            return False
        if curr_close >= curr_open:   # curr must be bearish
            return False
        return curr_open >= prev_close and curr_close <= prev_open


# ---------------------------------------------------------------------------
# CLASS 3: BreakBounceTelegramNotifier
# ---------------------------------------------------------------------------

class BreakBounceTelegramNotifier:

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token or ""
        self.chat_id   = chat_id or ""
        self._url      = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def send(self, text: str) -> bool:
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram not configured — skipping")
            return False
        try:
            resp = requests.post(
                self._url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if not resp.ok:
                logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
            return resp.ok
        except Exception:
            logger.warning("Telegram send exception")
            return False

    def send_premarket_report(self, levels_count: int, risk_summary: dict) -> None:
        day_str  = datetime.now(IST).strftime("%d %b %Y")
        capital  = risk_summary.get("capital", CAPITAL)
        dlimit   = risk_summary.get("daily_limit", capital * BB_RISK["daily_loss_limit_pct"])
        msg = (
            f"🎯 <b>BREAK &amp; BOUNCE SCANNER</b> | {day_str}\n"
            "Strategy: Yesterday's H/L breakout + 5-min retest entry\n"
            "──────────────────────────────\n"
            f"Stocks with daily levels: {levels_count}\n"
            "Breakout window: 09:15 – 11:45 IST\n"
            "Patterns: Hammer | Inverted Hammer | Engulfing\n"
            "──────────────────────────────\n"
            f"Capital: ₹{capital:,.0f} | Daily limit: ₹{dlimit:,.0f}\n"
            f"Max trades: {risk_summary.get('max_trades', BB_RISK['max_trades_per_day'])}"
        )
        self.send(msg)

    def send_breakout_alert(self, symbol: str, direction: str,
                            level: float, candle_close: float) -> None:
        arrow = "🟢 BULLISH" if direction == "BULLISH" else "🔴 BEARISH"
        level_label = "yesterday HIGH" if direction == "BULLISH" else "yesterday LOW"
        msg = (
            f"⚡ <b>BREAKOUT CONFIRMED: {symbol}</b>\n"
            f"Direction: {arrow}\n"
            f"15-min close: ₹{candle_close:.2f} vs {level_label}: ₹{level:.2f}\n"
            "Watching 5-min chart for retest entry..."
        )
        self.send(msg)

    def send_signal_alert(self, signal: dict, strike_data: dict,
                          lots: int, risk_data: dict) -> None:
        symbol   = signal.get("symbol", "")
        side     = signal.get("signal", "")
        pattern  = signal.get("pattern", "")
        sl_level = signal.get("sl_level", 0.0)

        ltp      = strike_data.get("ltp", 0.0)
        qty      = risk_data.get("qty", 0)
        sl       = risk_data.get("sl", 0.0)
        target   = risk_data.get("target", 0.0)
        max_risk = risk_data.get("max_risk", 0.0)
        strike   = strike_data.get("strike", "")

        auto_exec = os.getenv("AUTO_EXECUTE", "false").strip().lower() == "true"
        footer = "" if auto_exec else "\n⚠️ <i>Alert only — place order manually on Dhan app</i>"

        msg = (
            f"🚨 <b>B&amp;B SIGNAL: {symbol} {side} {strike}</b>\n"
            f"Pattern: {pattern}\n"
            f"Underlying SL level: ₹{sl_level:.2f}\n"
            "──────────────────────────────\n"
            f"Entry: ₹{ltp:.1f} | Lots: {lots} | Qty: {qty:,}\n"
            f"Option SL: ₹{sl:.1f} (-30%) | Target: ₹{target:.1f} (2.5x)\n"
            f"Max risk: ₹{max_risk:,.0f}"
            f"{footer}"
        )
        self.send(msg)

    def send_daily_summary(self, stats: dict, risk_summary: dict) -> None:
        trades    = stats.get("trades", 0)
        wins      = stats.get("wins", 0)
        losses    = stats.get("losses", 0)
        win_rate  = stats.get("win_rate", 0.0)
        total_pnl = stats.get("total_pnl", 0.0)
        capital   = risk_summary.get("capital", CAPITAL)
        pnl_pct   = risk_summary.get("daily_pnl_pct", 0.0)
        msg = (
            "📊 <b>BREAK &amp; BOUNCE DAILY SUMMARY</b>\n"
            "──────────────────────────────\n"
            f"Signals: {trades} | Wins: {wins} | Losses: {losses}\n"
            f"Win rate: {win_rate}% | P&amp;L: ₹{total_pnl:+,.0f}\n"
            "──────────────────────────────\n"
            f"Capital: ₹{capital:,} | Day: {pnl_pct:+.2f}%"
        )
        self.send(msg)

    def send_no_setup_alert(self, reason: str) -> None:
        self.send(f"ℹ️ <b>B&amp;B: No valid setups today</b>\nReason: {reason}")


# ---------------------------------------------------------------------------
# CLASS 4: BreakBounceStrategyRunner
# ---------------------------------------------------------------------------

class BreakBounceStrategyRunner:
    """Top-level orchestrator for the Break and Bounce strategy."""

    INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}

    def __init__(self, capital: float = CAPITAL):
        self.risk_manager  = BreakBounceRiskManager(capital)
        self.token_manager = TokenManager()
        self.lot_sizer     = ScripMasterLotSizer()
        self._scanner_obj  = None
        self._bb_scanner   = None
        self._affordability = None
        self._notifier     = None
        self._journal      = MomentumTradeJournal(filepath=TRADE_LOG_PATH)
        # {security_id: {symbol, breakout_direction, breakout_level, trade_placed, setup_voided}}
        self._stock_states: dict = {}
        # {security_id: {yesterday_high, yesterday_low, date, symbol, segment}}
        self._daily_levels: dict = {}

    def _build_scanner(self) -> DiscountedPremiumScanner:
        token = self.token_manager.refresh_if_needed()
        if not token:
            raise RuntimeError("Failed to get valid Dhan token")
        return DiscountedPremiumScanner(hardtoken=token, client_id=Config.DHAN_CLIENT_ID)

    def _ensure_components(self) -> None:
        if self._scanner_obj is not None:
            return
        self._scanner_obj   = self._build_scanner()
        self._bb_scanner    = BreakBounceScanner(self._scanner_obj)
        # AffordabilityFilter accepts any object with is_affordable(); BB risk manager qualifies
        self._affordability = AffordabilityFilter(self.lot_sizer, self.risk_manager)
        self._notifier      = BreakBounceTelegramNotifier(
            self._scanner_obj.telegram_bot_token,
            self._scanner_obj.telegram_chat_id,
        )
        logger.info("BreakBounceStrategyRunner components initialised")

    def _exchange_segment(self, symbol: str) -> str:
        return "IDX_I" if symbol in self.INDEX_SYMBOLS else "NSE_FNO"

    def _select_strike(self, chain: dict, spot: float, side: str,
                       symbol: str, expiry: str) -> dict:
        """ATM strike selection (mirrors MomentumStrategyRunner._select_strike)."""
        try:
            if not chain or spot <= 0:
                return {}
            strikes = sorted([float(k) for k in chain.keys()])
            if not strikes:
                return {}
            gaps = [strikes[i + 1] - strikes[i] for i in range(min(5, len(strikes) - 1))]
            strike_gap = max(set(gaps), key=gaps.count) if gaps else 50
            atm    = round(spot / strike_gap) * strike_gap
            offset = BB_STRIKE["otm_offset"]
            target = atm + offset * strike_gap if side == "CE" else atm - offset * strike_gap
            closest = min(strikes, key=lambda s: abs(s - target))
            actual_key = next((k for k in chain.keys() if float(k) == closest), None)
            if actual_key is None:
                return {}
            entry = chain[actual_key]
            sub   = entry.get("call" if side == "CE" else "put", {})
            ltp    = float(sub.get("ltp", 0))
            bid    = float(sub.get("bid", 0))
            ask    = float(sub.get("ask", 0))
            oi     = int(sub.get("oi", 0))
            volume = int(sub.get("volume", 0))
            mid        = (bid + ask) / 2 if (bid + ask) > 0 else ltp
            spread_pct = (ask - bid) / mid if mid > 0 else 1.0
            option_sec_id = self.lot_sizer.get_option_security_id(symbol, expiry, closest, side) or ""
            return {
                "strike":             closest,
                "ltp":                round(ltp, 2),
                "bid":                round(bid, 2),
                "ask":                round(ask, 2),
                "oi":                 oi,
                "volume":             volume,
                "spread_pct":         round(spread_pct, 4),
                "side":               side,
                "option_security_id": option_sec_id,
            }
        except Exception:
            logger.exception("_select_strike failed | %s", symbol)
            return {}

    def _check_liquidity(self, strike_data: dict) -> tuple:
        if strike_data.get("oi", 0) < BB_LIQUIDITY["min_oi"]:
            return False, f"low_oi({strike_data.get('oi', 0)})"
        if strike_data.get("volume", 0) < BB_LIQUIDITY["min_volume"]:
            return False, f"low_volume({strike_data.get('volume', 0)})"
        if strike_data.get("spread_pct", 1.0) > BB_LIQUIDITY["max_spread_pct"]:
            return False, f"wide_spread({strike_data.get('spread_pct', 1.0):.2%})"
        return True, "OK"

    def _place_order(self, strike_data: dict, lots: int,
                     lot_size: int, sl_price: float) -> dict:
        try:
            option_sec_id = strike_data.get("option_security_id", "")
            if not option_sec_id:
                logger.error("_place_order: no option_security_id in strike_data")
                return {"status": "no_option_security_id"}
            qty = lots * lot_size
            response = self._scanner_obj.dhan.place_order(
                security_id      = option_sec_id,
                exchange_segment  = self._scanner_obj.dhan.NSE_FNO,
                transaction_type  = self._scanner_obj.dhan.BUY,
                quantity          = qty,
                order_type        = self._scanner_obj.dhan.MARKET,
                product_type      = self._scanner_obj.dhan.INTRA,
                price             = 0,
            )
            logger.info("B&B buy order response: %s", response)
            if response.get("status") != "success":
                return {"status": "buy_failed", "response": response}
            sl_response = self._scanner_obj.dhan.place_order(
                security_id      = option_sec_id,
                exchange_segment  = self._scanner_obj.dhan.NSE_FNO,
                transaction_type  = self._scanner_obj.dhan.SELL,
                quantity          = qty,
                order_type        = self._scanner_obj.dhan.SL_M,
                product_type      = self._scanner_obj.dhan.INTRA,
                price             = 0,
                trigger_price     = sl_price,
            )
            if sl_response.get("status") != "success":
                logger.error("B&B SL order failed — placing emergency exit: %s", sl_response)
                self._scanner_obj.dhan.place_order(
                    security_id      = option_sec_id,
                    exchange_segment  = self._scanner_obj.dhan.NSE_FNO,
                    transaction_type  = self._scanner_obj.dhan.SELL,
                    quantity          = qty,
                    order_type        = self._scanner_obj.dhan.MARKET,
                    product_type      = self._scanner_obj.dhan.INTRA,
                    price             = 0,
                )
                self._notifier.send(
                    f"⚠️ B&amp;B SL order failed for {strike_data.get('side')} "
                    f"{strike_data.get('strike')} — emergency exit placed"
                )
                return {"status": "sl_failed_emergency_exit"}
            return {
                "status":       "ok",
                "buy_order_id": response.get("orderId", ""),
                "sl_order_id":  sl_response.get("orderId", ""),
            }
        except Exception:
            logger.exception("_place_order exception")
            return {"status": "exception"}

    # ---- Public run methods ----------------------------------------------------

    def run_premarket(self) -> dict:
        """
        Called at 9:00 AM. Fetches yesterday's daily levels for all affordable
        stocks and initialises per-stock state for the day.
        """
        try:
            self._ensure_components()
            affordable = self._affordability.get_affordable_universe(
                self._scanner_obj.fno_stocks)

            self._daily_levels = {}
            self._stock_states = {}

            for sec_id, symbol in affordable.items():
                seg    = self._exchange_segment(symbol)
                levels = self._bb_scanner.get_yesterday_levels(sec_id, seg)
                if levels.get("yesterday_high", 0) > 0:
                    self._daily_levels[sec_id] = {
                        **levels, "symbol": symbol, "segment": seg}
                    self._stock_states[sec_id] = {
                        "symbol":             symbol,
                        "breakout_direction": None,
                        "breakout_level":     0.0,
                        "trade_placed":       False,
                        "setup_voided":       False,
                    }
                time.sleep(0.3)

            count = len(self._daily_levels)
            logger.info("B&B premarket: %d stocks with valid daily levels", count)
            self._notifier.send_premarket_report(count, self.risk_manager.summary())
            return {"levels_loaded": count}

        except Exception:
            logger.exception("run_premarket failed")
            return {"error": "premarket_failed"}

    def run_intraday_scan(self) -> list:
        """
        Called every 5 min from 9:15–11:45.

        For each tracked stock:
          - If breakout not yet confirmed: run Step 2 (15-min breakout check)
          - If breakout confirmed, no trade yet: run Step 3 (5-min entry check)
        """
        try:
            self._ensure_components()

            now = datetime.now(IST)
            window_end = now.replace(
                hour=BB_BREAKOUT["window_end_hour"],
                minute=BB_BREAKOUT["window_end_min"],
                second=0, microsecond=0,
            )
            if now > window_end:
                logger.debug("B&B: past 11:45 breakout window — skipping scan")
                return []

            if not self._daily_levels:
                logger.warning("B&B: no daily levels — run_premarket() may not have run")
                return []

            signals_placed = []

            for sec_id, state in list(self._stock_states.items()):
                if state.get("trade_placed") or state.get("setup_voided"):
                    continue

                levels = self._daily_levels.get(sec_id, {})
                symbol = state["symbol"]
                seg    = levels.get("segment", "NSE_FNO")

                # ── Step 2: check 15-min breakout ────────────────────────────
                if state["breakout_direction"] is None:
                    result = self._bb_scanner.check_15min_breakout(
                        sec_id, seg, symbol, levels)
                    direction = result.get("direction", "NONE")

                    if direction in ("BULLISH", "BEARISH"):
                        state["breakout_direction"] = direction
                        state["breakout_level"]     = result.get("breakout_level", 0.0)
                        logger.info(
                            "B&B BREAKOUT CONFIRMED: %s %s level=%.2f close=%.2f",
                            symbol, direction, state["breakout_level"],
                            result.get("candle_close", 0.0))
                        self._notifier.send_breakout_alert(
                            symbol, direction,
                            state["breakout_level"],
                            result.get("candle_close", 0.0))
                    elif result.get("reason") == "window_expired":
                        state["setup_voided"] = True
                        logger.debug("B&B %s: window expired, voiding setup", symbol)

                    time.sleep(0.3)
                    continue   # check entry on the next 5-min scan cycle

                # ── Step 3: breakout confirmed — check 5-min entry ───────────
                can_trade, reason = self.risk_manager.can_trade()
                if not can_trade:
                    logger.info("B&B: cannot trade — %s", reason)
                    break

                entry_signal = self._bb_scanner.check_5min_entry(
                    sec_id, seg, symbol,
                    state["breakout_direction"], state["breakout_level"])

                if entry_signal.get("signal") == "NONE":
                    time.sleep(0.2)
                    continue

                # ── Signal found — fetch option chain ────────────────────────
                side = entry_signal["signal"]
                try:
                    expiries = self._scanner_obj.get_expiry_list(sec_id, seg)
                    expiries = [e for e in expiries if get_trading_days_to_expiry(e) >= 4]
                    if not expiries:
                        logger.info("B&B: no valid expiry for %s", symbol)
                        time.sleep(0.2)
                        continue
                    expiry     = expiries[0]
                    chain_resp = self._scanner_obj.get_option_chain(sec_id, seg, expiry)
                    if not (isinstance(chain_resp, dict) and
                            chain_resp.get("status") == "success"):
                        time.sleep(0.2)
                        continue
                    chain_data = unwrap_dhan_payload(chain_resp.get("data") or {})
                    spot  = chain_data.get("last_price", 0)
                    chain = chain_data.get("oc", {})
                    if not chain or not spot:
                        time.sleep(0.2)
                        continue
                except Exception:
                    logger.exception("B&B chain fetch failed for %s", symbol)
                    time.sleep(0.2)
                    continue

                strike_data = self._select_strike(chain, spot, side, symbol, expiry)
                if not strike_data:
                    time.sleep(0.2)
                    continue

                liq_ok, liq_reason = self._check_liquidity(strike_data)
                if not liq_ok:
                    logger.info("B&B liquidity fail %s: %s", symbol, liq_reason)
                    time.sleep(0.2)
                    continue

                lot_size = self.lot_sizer.get(symbol)
                premium  = strike_data.get("ltp", 0)
                if premium <= 0:
                    time.sleep(0.2)
                    continue

                lots = self.risk_manager.calculate_lots(premium, lot_size)
                if lots < 1:
                    logger.info("B&B unaffordable %s prem=%.1f lot=%d",
                                symbol, premium, lot_size)
                    time.sleep(0.2)
                    continue

                sl     = self.risk_manager.sl_price(premium)
                target = self.risk_manager.target_price(premium)

                now_ist = datetime.now(IST)
                trade = {
                    "date":            now_ist.strftime("%Y-%m-%d"),
                    "time":            now_ist.strftime("%H:%M:%S"),
                    "symbol":          symbol,
                    "security_id":     sec_id,
                    "option_type":     side,
                    "strike":          strike_data.get("strike"),
                    "expiry":          expiry,
                    "lots":            lots,
                    "qty":             lots * lot_size,
                    "entry_premium":   premium,
                    "sl_price":        sl,
                    "t1":              target,
                    "t2":              "",
                    "exit_price":      "",
                    "exit_reason":     "",
                    "pnl":             "",
                    "pnl_pct":         "",
                    "holding_minutes": "",
                    "regime":          entry_signal.get("pattern", ""),
                    "strength":        state.get("breakout_direction", ""),
                    "adx":             "",
                    "signal_type":     "BREAK_AND_BOUNCE",
                    "trigger":         entry_signal.get("pattern", ""),
                    "volume_ratio":    "",
                    "composite_score": "",
                    "notes": (
                        f"breakout_level={state['breakout_level']:.2f} "
                        f"sl_underlying={entry_signal.get('sl_level', 0):.2f}"
                    ),
                }
                self._journal.log_entry(trade)

                risk_data = {
                    "qty":      lots * lot_size,
                    "sl":       sl,
                    "target":   target,
                    "max_risk": round(premium * BB_RISK["sl_pct"] * lots * lot_size, 2),
                }
                self._notifier.send_signal_alert(entry_signal, strike_data, lots, risk_data)

                if os.getenv("AUTO_EXECUTE", "false").strip().lower() == "true":
                    order = self._place_order(strike_data, lots, lot_size, sl)
                    trade["order"] = order

                self.risk_manager.record_trade()
                self.risk_manager.add_position({
                    "symbol":   symbol,
                    "side":     side,
                    "strike":   strike_data.get("strike"),
                    "expiry":   expiry,
                    "entry":    premium,
                    "lots":     lots,
                    "lot_size": lot_size,
                    "sl":       sl,
                    "target":   target,
                })
                state["trade_placed"] = True
                signals_placed.append(trade)
                logger.info("B&B trade logged: %s %s %s lots=%d",
                            symbol, side, strike_data.get("strike"), lots)
                time.sleep(0.3)

            return signals_placed

        except Exception:
            logger.exception("run_intraday_scan failed")
            return []

    def run_eod(self) -> None:
        """Called at 15:15. Send daily summary and reset all state."""
        try:
            self._ensure_components()
            stats = self._journal.get_today_stats()
            self._notifier.send_daily_summary(stats, self.risk_manager.summary())
            self.risk_manager.reset_daily()
            self._stock_states = {}
            self._daily_levels = {}
            logger.info("B&B EOD summary sent | stats=%s", stats)
        except Exception:
            logger.exception("run_eod failed")
