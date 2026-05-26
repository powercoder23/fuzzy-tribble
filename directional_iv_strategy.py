import logging
import math
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from discount import DiscountedPremiumScanner, unwrap_dhan_payload
from config import Config
import iv_store
from directional_iv_config import (
    CAPITAL,
    DTE_FILTER,
    IV_FILTER,
    LIQUIDITY,
    MIN_SCORE,
    RISK_CONFIG,
    TELEGRAM_ALERT_THRESHOLD,
    TREND_FILTER,
    DEFAULT_UNIVERSE_SIZE,
)

logger = logging.getLogger(__name__)


class DirectionalIVScanner:
    """Directional option buying scanner using Dhan API and IV history."""

    def __init__(self, hardtoken=None, client_id=None, universe=None,
                 upstox_adapter=None):
        self.scanner = DiscountedPremiumScanner(upstox_adapter=upstox_adapter)
        self.universe = self._build_universe(universe)

    def _build_universe(self, universe):
        if universe:
            return universe
        symbols = list(self.scanner.fno_stocks.items())
        if len(symbols) <= DEFAULT_UNIVERSE_SIZE:
            return dict(symbols)
        return dict(symbols[:DEFAULT_UNIVERSE_SIZE])

    def _select_expiry(self, security_id, symbol, segment):
        expiries = self.scanner.get_expiry_list(security_id, segment)
        if not expiries:
            return None

        candidates = []
        for expiry in expiries:
            dte = self.scanner.days_to_expiry(expiry)
            if DTE_FILTER["min_dte"] <= dte <= DTE_FILTER["max_dte"]:
                candidates.append((dte, expiry))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]

        # Fallback to nearest valid expiry if exact DTE window is unavailable
        for expiry in expiries:
            dte = self.scanner.days_to_expiry(expiry)
            if dte >= DTE_FILTER["min_dte"]:
                return expiry
        return expiries[0]

    def _trend_context(self, price_df: pd.DataFrame) -> dict:
        if price_df.empty or len(price_df) < TREND_FILTER["ema_long"]:
            return {"trend": "neutral"}

        closes = price_df["close"].astype(float).copy()
        ema_fast = closes.ewm(span=TREND_FILTER["ema_fast"], adjust=False).mean()
        ema_mid = closes.ewm(span=TREND_FILTER["ema_mid"], adjust=False).mean()
        ema_slow = closes.ewm(span=TREND_FILTER["ema_slow"], adjust=False).mean()
        ema_long = closes.ewm(span=TREND_FILTER["ema_long"], adjust=False).mean()

        last_close = closes.iloc[-1]
        last_open = price_df["open"].astype(float).iloc[-1]
        is_bullish = last_close > ema_fast.iloc[-1] > ema_mid.iloc[-1] > ema_slow.iloc[-1] > ema_long.iloc[-1]
        is_bearish = last_close < ema_fast.iloc[-1] < ema_mid.iloc[-1] < ema_slow.iloc[-1] < ema_long.iloc[-1]
        trend = "neutral"
        if is_bullish:
            trend = "bullish"
        elif is_bearish:
            trend = "bearish"

        return {
            "trend": trend,
            "last_close": last_close,
            "last_open": last_open,
            "ema_fast": ema_fast.iloc[-1],
            "ema_mid": ema_mid.iloc[-1],
            "ema_slow": ema_slow.iloc[-1],
            "ema_long": ema_long.iloc[-1],
        }

    def _score_candidate(self, candidate: dict) -> float:
        score = 0.0
        score += candidate.get("trend_alignment", 0) * 1.3
        score += candidate.get("delta_score", 0) * 1.1
        score += candidate.get("iv_edge_score", 0) * 1.0
        score += candidate.get("liquidity_score", 0) * 0.9
        score += candidate.get("iv_rank_score", 0) * 0.9
        score += candidate.get("moneyness_score", 0) * 0.7
        score += candidate.get("expiry_score", 0) * 0.5
        return min(100.0, score)

    def _build_trade_plan(self, premium: float) -> dict:
        stop_loss = round(premium * (1 - RISK_CONFIG["sl_pct"]), 2)
        target = round(premium * RISK_CONFIG["target_mult"], 2)
        rr = None
        if premium > stop_loss:
            rr = round((target - premium) / (premium - stop_loss), 2)
        return {
            "entry": round(premium, 2),
            "stop_loss": stop_loss,
            "target": target,
            "risk_reward": rr,
        }

    def _spread_pct(self, bid: float, ask: float, mid: float) -> float:
        if bid is None or ask is None or mid is None or mid <= 0:
            return 1.0
        return abs(ask - bid) / mid

    def _liquidity_score(self, oi: float, volume: float) -> float:
        if oi <= 0 or volume <= 0:
            return 0.0
        return min(100.0, math.log1p(oi) * 10 + math.log1p(volume) * 6)

    def _moneyness_score(self, moneyness_pct: float) -> float:
        if moneyness_pct < 0 or moneyness_pct > IV_FILTER["max_moneyness_pct"]:
            return 0.0
        return clip_score(100 - (moneyness_pct / IV_FILTER["max_moneyness_pct"]) * 100)

    def _trend_alignment_score(self, bias: str, option_type: str) -> float:
        if bias == "bullish" and option_type == "CALL":
            return 100.0
        if bias == "bearish" and option_type == "PUT":
            return 100.0
        return 0.0

    def _iv_edge_score(self, current_iv: float, weighted_hv: float) -> float:
        if weighted_hv is None or weighted_hv <= 0 or current_iv <= 0:
            return 0.0
        diff = ((weighted_hv - current_iv) / weighted_hv) * 100
        return clip_score(diff * 1.5)

    def _normalise_delta(self, delta: float) -> float:
        return abs(delta)

    def scan_single_strike(self, strike_data: dict, strike_price: float, spot_price: float,
                           atm_context: dict, option_chain: dict, expected_move: float,
                           dte: int, trend_bias: str, has_iv_history: bool,
                           historical_ivs: list[float], hv_metrics: dict,
                           chain_metrics: dict) -> list[dict]:
        candidates = []
        if not strike_data or not isinstance(strike_data, dict):
            return []

        for side in ["ce", "pe"]:
            if side not in strike_data:
                continue
            opt = strike_data[side]
            option_type = "CALL" if side == "ce" else "PUT"
            delta = self._normalise_delta(opt.get("greeks", {}).get("delta", 0))
            if delta < IV_FILTER["min_delta"] or delta > IV_FILTER["max_delta"]:
                continue

            oi = opt.get("oi", 0) or 0
            volume = opt.get("volume", 0) or 0
            bid = opt.get("top_bid_price", opt.get("last_price", 0)) or 0
            ask = opt.get("top_ask_price", opt.get("last_price", 0)) or 0
            mid = (bid + ask) / 2 if bid and ask else opt.get("last_price", 0) or 0
            spread_pct = self._spread_pct(bid, ask, mid)
            if oi < LIQUIDITY["min_oi"] or volume < LIQUIDITY["min_volume"] or mid <= 0 or spread_pct > LIQUIDITY["max_spread_pct"]:
                continue

            current_iv = opt.get("implied_volatility", 0)
            if current_iv <= 0 or current_iv > IV_FILTER["max_atm_iv"]:
                continue

            iv_rank = self.scanner.calculate_iv_rank(current_iv, historical_ivs) if has_iv_history else None
            iv_percentile = self.scanner.calculate_iv_percentile(current_iv, historical_ivs) if has_iv_history else None
            if has_iv_history and iv_rank is not None and iv_rank > IV_FILTER["max_iv_rank"]:
                continue

            atm_iv = atm_context.get("atm_call_iv") if option_type == "CALL" else atm_context.get("atm_put_iv")
            if not atm_iv:
                atm_iv = atm_context.get("atm_iv")
            iv_edge = ((atm_iv - current_iv) / atm_iv) * 100 if atm_iv and atm_iv > 0 else 0
            if iv_edge < -5:
                continue

            expected_move_ratio = abs(strike_price - spot_price) / expected_move if expected_move else 0
            if expected_move and expected_move_ratio > IV_FILTER["max_expected_move_ratio"]:
                continue

            bias = trend_bias
            if bias == "neutral":
                continue
            if bias == "bullish" and option_type != "CALL":
                continue
            if bias == "bearish" and option_type != "PUT":
                continue

            trade_plan = self._build_trade_plan(mid)
            if trade_plan["risk_reward"] is None or trade_plan["risk_reward"] < 1.8:
                continue

            weighted_hv = hv_metrics.get("weighted_hv")
            reason = []
            if has_iv_history and iv_rank is not None:
                reason.append(f"IV Rank {iv_rank:.0f}")
            if iv_percentile is not None:
                reason.append(f"IV %%ile {iv_percentile:.0f}")
            if iv_edge > 0:
                reason.append(f"IV {iv_edge:.1f}%% below ATM")
            if expected_move and expected_move_ratio <= 1.0:
                reason.append("Inside expected move")
            if chain_metrics.get("call_put_oi_ratio"):
                reason.append(f"OI ratio {chain_metrics['call_put_oi_ratio']:.2f}")
            if bias == "bullish":
                reason.append("Trend bias bullish")
            if bias == "bearish":
                reason.append("Trend bias bearish")

            candidate = {
                "type": option_type,
                "strike": strike_price,
                "entry": round(mid, 2),
                "stop_loss": trade_plan["stop_loss"],
                "target": trade_plan["target"],
                "risk_reward": trade_plan["risk_reward"],
                "delta": round(delta, 3),
                "iv": round(current_iv, 2),
                "iv_rank": round(iv_rank, 2) if iv_rank is not None else None,
                "iv_percentile": round(iv_percentile, 2) if iv_percentile is not None else None,
                "atm_iv": round(atm_context.get("atm_iv") or 0, 2),
                "moneyness": round(abs(strike_price - spot_price) / spot_price * 100, 2),
                "oi": int(oi),
                "volume": int(volume),
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "expected_move": round(expected_move, 2) if expected_move else None,
                "expected_move_ratio": round(expected_move_ratio, 2),
                "trend_bias": bias,
                "trend": bias,
                "expiry_dte": dte,
                "call_put_oi_ratio": round(chain_metrics.get("call_put_oi_ratio") or 0, 2),
                "score_components": {
                    "trend_alignment": self._trend_alignment_score(bias, option_type),
                    "delta_score": 100.0 if IV_FILTER["min_delta"] <= delta <= 0.30 else 65.0,
                    "iv_edge_score": self._iv_edge_score(current_iv, weighted_hv),
                    "liquidity_score": self._liquidity_score(oi, volume),
                    "iv_rank_score": 100.0 - min(iv_rank or 50.0, 100.0) if iv_rank is not None else 50.0,
                    "moneyness_score": self._moneyness_score(abs(strike_price - spot_price) / spot_price * 100),
                    "expiry_score": 100.0 if DTE_FILTER["min_dte"] <= dte <= 21 else 65.0,
                },
                "score": 0.0,
                "reason": reason,
                "spot": round(spot_price, 2),
                "symbol": None,
            }
            candidate["score"] = round(self._score_candidate(candidate), 2)
            if candidate["score"] < MIN_SCORE:
                continue
            candidates.append(candidate)

        return candidates

    def scan_underlying(self, security_id, segment, symbol):
        expiry = self._select_expiry(security_id, symbol, segment)
        if not expiry:
            logger.warning("No expiry available for %s", symbol)
            return []

        chain_response = self.scanner.get_option_chain(security_id, segment, expiry)
        if chain_response.get("status") != "success":
            return []

        chain_data = unwrap_dhan_payload(chain_response.get("data") or {})
        spot_price = chain_data.get("last_price")
        option_chain = chain_data.get("oc")
        if spot_price is None or not isinstance(option_chain, dict):
            logger.warning("Bad option chain payload for %s", symbol)
            return []

        atm_context = self.scanner.extract_atm_reference_ivs(option_chain, spot_price)
        if (
            atm_context.get("atm_call_oi", 0) < LIQUIDITY["min_atm_oi"]
            and atm_context.get("atm_put_oi", 0) < LIQUIDITY["min_atm_oi"]
        ):
            logger.warning(
                "Skipping %s due to low ATM liquidity (call %s, put %s)",
                symbol,
                atm_context.get("atm_call_oi"),
                atm_context.get("atm_put_oi"),
            )
            return []

        historical_ivs = self.scanner.fetch_historical_iv(security_id, segment)
        has_iv_history = len(historical_ivs) >= 10
        dte = self.scanner.days_to_expiry(expiry)
        expected_move = self.scanner.compute_expected_move(spot_price, atm_context.get("atm_iv"), dte)
        hv_metrics = {"weighted_hv": None}
        trend_context = {"trend": "neutral"}
        hist_prices = self.scanner.fetch_historical_prices(
            security_id=security_id,
            exchange_segment=segment,
            from_date=(date.today() - timedelta(days=260)).isoformat(),
            to_date=date.today().isoformat(),
        )
        if not hist_prices.empty:
            hv_metrics = self.scanner.calculate_hv_metrics(hist_prices)
            trend_context = self._trend_context(hist_prices)

        total_metrics = self.scanner.extract_chain_metrics(option_chain)
        if total_metrics.get("total_put_oi"):
            total_metrics["call_put_oi_ratio"] = (
                total_metrics.get("total_call_oi", 0) / max(total_metrics.get("total_put_oi", 1), 1)
            )
        else:
            total_metrics["call_put_oi_ratio"] = None

        candidates = []
        for strike_str, strike_data in option_chain.items():
            try:
                strike_price = float(strike_str)
            except (TypeError, ValueError):
                continue
            entries = self.scan_single_strike(
                strike_data=strike_data,
                strike_price=strike_price,
                spot_price=spot_price,
                atm_context=atm_context,
                option_chain=option_chain,
                expected_move=expected_move,
                dte=dte,
                trend_bias=trend_context.get("trend", "neutral"),
                has_iv_history=has_iv_history,
                historical_ivs=historical_ivs,
                hv_metrics=hv_metrics,
                chain_metrics=total_metrics,
            )
            for entry in entries:
                entry["symbol"] = symbol
                entry["trend"] = trend_context.get("trend", "neutral")
                entry["hv"] = round(hv_metrics.get("weighted_hv") or 0, 2)
                entry["atm_iv"] = round(atm_context.get("atm_iv") or 0, 2)
                candidates.append(entry)

        candidates.sort(key=lambda item: item["score"], reverse=True)
        return candidates

    def scan_all_underlyings(self):
        all_opportunities = []
        for security_id, symbol in self.universe.items():
            segment = "IDX_I" if symbol in {"NIFTY", "BANKNIFTY"} else "NSE_FNO"
            try:
                logger.info("Scanning %s", symbol)
                candidates = self.scan_underlying(security_id, segment, symbol)
                for c in candidates:
                    if c["score"] >= MIN_SCORE:
                        all_opportunities.append(c)
                # rate-limit
                time.sleep(1)
            except Exception:
                logger.exception("Error scanning %s", symbol)

        if not all_opportunities:
            return pd.DataFrame()

        return pd.DataFrame(all_opportunities).sort_values("score", ascending=False)

    def generate_report(self, opportunities_df: pd.DataFrame):
        if opportunities_df.empty:
            logger.info("Directional IV scanner found no qualifying opportunities")
            return

        logger.info("%s", "=" * 100)
        logger.info("DIRECTIONAL IV BUY OPPORTUNITIES")
        logger.info("%s", "=" * 100)
        for _, row in opportunities_df.head(20).iterrows():
            logger.info("%s %s | Strike %.0f | Score %.1f | Trend %s", row["symbol"], row["type"], row["strike"], row["score"], row["trend"])
            logger.info("  Spot %.2f | Entry %.2f | SL %.2f | Target %.2f | R/R %.2f", row["spot"], row["entry"], row["stop_loss"], row["target"], row["risk_reward"])
            logger.info("  Delta %.3f | IV %.2f | IV Rank %s | HV %.2f | EM ratio %.2f", row["delta"], row["iv"], row.get("iv_rank", "N/A"), row["hv"], row["expected_move_ratio"])
            logger.info("  OI %s | Vol %s | Moneyness %.2f%% | OI ratio %.2f | Reasons: %s", f"{int(row['oi']):,}", f"{int(row['volume']):,}", row["moneyness"], row.get("call_put_oi_ratio", 0.0), "; ".join(row["reason"]))
            logger.info("  Expiry DTE: %s | ATM IV: %.2f", row["expiry_dte"], row["atm_iv"])
            logger.info("%s", "-" * 100)

    def send_telegram_summary(self, opportunities_df: pd.DataFrame):
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            logger.info("Telegram summary skipped; bot token or chat ID missing")
            return

        if opportunities_df.empty:
            text = "Directional IV scan completed. No strong buy setups found."
        else:
            strong_rows = opportunities_df[opportunities_df["score"] >= TELEGRAM_ALERT_THRESHOLD]
            top_rows = strong_rows.head(5) if not strong_rows.empty else opportunities_df.head(5)
            header = [
                "Directional IV Scanner Summary",
                f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            ]
            if not strong_rows.empty:
                header.append(f"⚡ Strong ideas (score ≥ {TELEGRAM_ALERT_THRESHOLD}):")
            else:
                header.append(f"No strong setups above threshold {TELEGRAM_ALERT_THRESHOLD}. Top candidates:")

            lines = header
            for _, row in top_rows.iterrows():
                lines.append(
                    f"{row['symbol']} {row['type']} {row['strike']:.0f} | Score {row['score']:.1f} | Entry {row['entry']:.2f} | SL {row['stop_loss']:.2f} | R/R {row['risk_reward']:.2f} | OI {int(row['oi']):,} | IV {row['iv']:.1f}%"
                )
                lines.append(f"  Reasons: {', '.join(row['reason'])}")

            if len(top_rows) < len(opportunities_df):
                lines.append(f"(+{len(opportunities_df) - len(top_rows)} additional candidates)")
            text = "\n".join(lines)

        try:
            import requests
            response = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
            response.raise_for_status()
            logger.info("Directional IV telegram summary sent")
        except Exception:
            logger.exception("Failed to send directional IV telegram summary")


def clip_score(value, floor=0.0, ceiling=100.0) -> float:
    return max(floor, min(ceiling, value))
