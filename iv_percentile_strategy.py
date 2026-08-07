"""
iv_percentile_strategy.py - Pure IV Percentile Mean Reversion Strategy

Uses 60-day IV history to identify extremes:
- IV >= 85th percentile -> SELL strangle (mean reversion down)
- IV <= 15th percentile -> BUY option (mean reversion up)

This is the simplest, most proven strategy using only IV data.
"""

import logging
import time
import uuid

import numpy as np

from discount import DiscountedPremiumScanner, unwrap_dhan_payload
from collectors import iv_store
from order_manager import OrderManager

logger = logging.getLogger(__name__)

# Configuration lives here, not in docker-compose env vars or a shared config
# module - this strategy owns its own tuning knobs.
IV_PERCENTILE_CONFIG = {
    "SELL_ZONE": 85,          # Sell when IV percentile >= 85%
    "BUY_ZONE": 15,           # Buy when IV percentile <= 15%
    "MIN_HISTORY_DAYS": 20,   # Minimum days for percentile
    "MAX_SIGNALS_PER_DAY": 3, # Reduce friction - was unlimited
    "MAX_RISK_RUPEES": 1500,  # Same as existing risk cap
    "DTE_MIN": 5,             # Minimum days to expiry
    "DTE_MAX": 15,            # Maximum days to expiry
    "MIN_OPTION_PREMIUM": 5,  # Minimum premium in rupees
    "MIN_OI": 2500,           # Minimum open interest
    "MIN_VOLUME": 500,        # Minimum volume
    "MAX_SPREAD_PCT": 0.20,   # Maximum spread as % of mid
    "STRANGLE_DELTA_TARGET": 0.20,  # Delta target for strangle legs
    "SELL_SL_MULTIPLIER": 1.5,      # SL at 1.5x credit received
    "SELL_TARGET_MULTIPLIER": 0.5,   # Target at 50% of credit received
    "BUY_SL_MULTIPLIER": 0.30,       # SL at 30% of premium paid
    "BUY_TARGET_MULTIPLIER": 1.30,   # Target at 30% gain
}

class IVPercentileStrategy:
    """Pure IV Percentile strategy using 60-day history."""

    def __init__(self, upstox_adapter=None):
        self.scanner = DiscountedPremiumScanner(upstox_adapter=upstox_adapter)
        self.order_manager = OrderManager()
        self._traded_today = set()
        self._signals_today = 0

    def get_iv_percentile(self, security_id: str, current_iv: float) -> float:
        """Get IV percentile from 60-day history."""
        if not current_iv or current_iv <= 0:
            return 50.0

        hist = iv_store.get_iv_history(security_id, days=60)
        if len(hist) < IV_PERCENTILE_CONFIG["MIN_HISTORY_DAYS"]:
            return 50.0

        ivs = [row.get("atm_iv", 0) for row in hist if row.get("atm_iv")]
        ivs = [x for x in ivs if x and x > 0]

        if len(ivs) < IV_PERCENTILE_CONFIG["MIN_HISTORY_DAYS"]:
            return 50.0

        return float((np.array(ivs) < current_iv).mean() * 100)

    def get_iv_zscore(self, security_id: str, current_iv: float) -> float:
        """Get IV Z-score (how many std devs from mean)."""
        if not current_iv or current_iv <= 0:
            return 0.0

        hist = iv_store.get_iv_history(security_id, days=60)
        if len(hist) < IV_PERCENTILE_CONFIG["MIN_HISTORY_DAYS"]:
            return 0.0

        ivs = [row.get("atm_iv", 0) for row in hist if row.get("atm_iv")]
        ivs = [x for x in ivs if x and x > 0]

        if len(ivs) < IV_PERCENTILE_CONFIG["MIN_HISTORY_DAYS"]:
            return 0.0

        mean = np.mean(ivs)
        std = np.std(ivs)
        if std == 0:
            return 0.0

        return (current_iv - mean) / std

    def get_spot_trend(self, security_id: str, spot: float) -> str:
        """Determine spot trend from last 5 days."""
        hist = iv_store.get_iv_history(security_id, days=10)
        spots = [row.get("spot_price", 0) for row in hist if row.get("spot_price")]
        spots = [x for x in spots if x and x > 0]

        if len(spots) < 3:
            return "neutral"

        avg = np.mean(spots[-5:]) if len(spots) >= 5 else np.mean(spots)
        pct_change = (spot - avg) / avg * 100 if avg > 0 else 0

        if pct_change > 0.5:
            return "up"
        elif pct_change < -0.5:
            return "down"
        return "neutral"

    def scan_underlying(self, security_id: str, symbol: str) -> dict | None:
        """Scan one underlying for IV percentile signals."""

        # Get current IV and spot
        snapshots = iv_store.get_bulk_latest_snapshots([security_id])
        snap = snapshots.get(security_id, {})
        current_iv = snap.get("atm_iv")
        spot = snap.get("spot_price")

        if not current_iv or current_iv <= 0:
            return None
        if not spot or spot <= 0:
            return None

        # Compute metrics
        percentile = self.get_iv_percentile(security_id, current_iv)
        zscore = self.get_iv_zscore(security_id, current_iv)
        trend = self.get_spot_trend(security_id, spot)

        # Determine signal
        if percentile >= IV_PERCENTILE_CONFIG["SELL_ZONE"]:
            return self._build_sell_signal(security_id, symbol, spot, current_iv, percentile, zscore, trend)

        elif percentile <= IV_PERCENTILE_CONFIG["BUY_ZONE"]:
            return self._build_buy_signal(security_id, symbol, spot, current_iv, percentile, zscore, trend)

        return None

    def _build_sell_signal(self, security_id, symbol, spot, current_iv, percentile, zscore, trend):
        """Build SELL strangle signal."""
        segment = "IDX_I" if symbol in ["NIFTY", "BANKNIFTY"] else "NSE_FNO"

        # Get expiry
        expiry = self._get_expiry(security_id, segment)
        if not expiry:
            return None

        dte = self.scanner.days_to_expiry(expiry)

        # Get option chain
        chain_response = self.scanner.get_option_chain(security_id, segment, expiry)
        if chain_response.get("status") != "success":
            return None

        chain_data = unwrap_dhan_payload(chain_response.get("data") or {})
        option_chain = chain_data.get("oc")
        if not option_chain:
            return None

        # Get ATM context
        atm_context = self.scanner.extract_atm_reference_ivs(option_chain, spot)
        atm_strike = atm_context.get("atm_strike")
        if atm_strike is None:
            return None

        # Find OTM strangle legs at ~20 delta
        strikes = sorted([float(k) for k in option_chain.keys()])
        ce_leg = None
        pe_leg = None

        for strike in strikes:
            if strike <= atm_strike:
                continue
            strike_data = option_chain.get(str(strike))
            if not strike_data:
                continue
            opt = strike_data.get("ce")
            if opt:
                delta = abs(opt.get("greeks", {}).get("delta", 0) or 0)
                if IV_PERCENTILE_CONFIG["STRANGLE_DELTA_TARGET"] - 0.05 <= delta <= IV_PERCENTILE_CONFIG["STRANGLE_DELTA_TARGET"] + 0.05:
                    ce_leg = (strike, opt)
                    break

        for strike in reversed(strikes):
            if strike >= atm_strike:
                continue
            strike_data = option_chain.get(str(strike))
            if not strike_data:
                continue
            opt = strike_data.get("pe")
            if opt:
                delta = abs(opt.get("greeks", {}).get("delta", 0) or 0)
                if IV_PERCENTILE_CONFIG["STRANGLE_DELTA_TARGET"] - 0.05 <= delta <= IV_PERCENTILE_CONFIG["STRANGLE_DELTA_TARGET"] + 0.05:
                    pe_leg = (strike, opt)
                    break

        if not ce_leg or not pe_leg:
            return None

        ce_strike, ce_opt = ce_leg
        pe_strike, pe_opt = pe_leg

        # Check liquidity
        ce_oi = ce_opt.get("oi", 0) or 0
        ce_vol = ce_opt.get("volume", 0) or 0
        pe_oi = pe_opt.get("oi", 0) or 0
        pe_vol = pe_opt.get("volume", 0) or 0

        if ce_oi < IV_PERCENTILE_CONFIG["MIN_OI"] or pe_oi < IV_PERCENTILE_CONFIG["MIN_OI"]:
            return None
        if ce_vol < IV_PERCENTILE_CONFIG["MIN_VOLUME"] or pe_vol < IV_PERCENTILE_CONFIG["MIN_VOLUME"]:
            return None

        # Get pricing
        ce_pricing = self.scanner.get_execution_prices(ce_opt)
        pe_pricing = self.scanner.get_execution_prices(pe_opt)

        ce_entry = ce_pricing.get("bid") or ce_pricing.get("mid_price") or 0
        pe_entry = pe_pricing.get("bid") or pe_pricing.get("mid_price") or 0

        if ce_entry <= 0 or pe_entry <= 0:
            return None
        if ce_entry < IV_PERCENTILE_CONFIG["MIN_OPTION_PREMIUM"] or pe_entry < IV_PERCENTILE_CONFIG["MIN_OPTION_PREMIUM"]:
            return None

        # Check spread
        ce_spread = self.scanner._spread_pct(ce_pricing.get("bid"), ce_pricing.get("ask"), ce_pricing.get("mid_price"))
        pe_spread = self.scanner._spread_pct(pe_pricing.get("bid"), pe_pricing.get("ask"), pe_pricing.get("mid_price"))
        if ce_spread > IV_PERCENTILE_CONFIG["MAX_SPREAD_PCT"] or pe_spread > IV_PERCENTILE_CONFIG["MAX_SPREAD_PCT"]:
            return None

        total_credit = ce_entry + pe_entry
        confidence = min(100, (percentile - IV_PERCENTILE_CONFIG["SELL_ZONE"]) * 2 + 50)

        return {
            "symbol": symbol,
            "security_id": security_id,
            "exchange_segment": segment,
            "strategy": f"IV_SELL_STRANGLE (IVP {percentile:.1f}%)",
            "signal_type": "SELL",
            "direction": "short",
            "percentile": round(percentile, 1),
            "zscore": round(zscore, 2),
            "confidence": round(confidence, 1),
            "expiry": expiry,
            "dte": dte,
            "spot": round(spot, 2),
            "legs": [
                {
                    "side": "CE",
                    "strike": ce_strike,
                    "entry": round(ce_entry, 2),
                    "bid": round(ce_pricing.get("bid", 0), 2),
                    "ask": round(ce_pricing.get("ask", 0), 2),
                    "delta": round(abs(ce_opt.get("greeks", {}).get("delta", 0) or 0), 3),
                    "oi": ce_oi,
                    "volume": ce_vol,
                },
                {
                    "side": "PE",
                    "strike": pe_strike,
                    "entry": round(pe_entry, 2),
                    "bid": round(pe_pricing.get("bid", 0), 2),
                    "ask": round(pe_pricing.get("ask", 0), 2),
                    "delta": round(abs(pe_opt.get("greeks", {}).get("delta", 0) or 0), 3),
                    "oi": pe_oi,
                    "volume": pe_vol,
                }
            ],
            "total_credit": round(total_credit, 2),
            "sl": round(total_credit * IV_PERCENTILE_CONFIG["SELL_SL_MULTIPLIER"], 2),
            "target": round(total_credit * IV_PERCENTILE_CONFIG["SELL_TARGET_MULTIPLIER"], 2),
        }

    def _build_buy_signal(self, security_id, symbol, spot, current_iv, percentile, zscore, trend):
        """Build BUY option signal."""
        segment = "IDX_I" if symbol in ["NIFTY", "BANKNIFTY"] else "NSE_FNO"

        # Get expiry
        expiry = self._get_expiry(security_id, segment)
        if not expiry:
            return None

        dte = self.scanner.days_to_expiry(expiry)

        # Get option chain
        chain_response = self.scanner.get_option_chain(security_id, segment, expiry)
        if chain_response.get("status") != "success":
            return None

        chain_data = unwrap_dhan_payload(chain_response.get("data") or {})
        option_chain = chain_data.get("oc")
        if not option_chain:
            return None

        # Get ATM context
        atm_context = self.scanner.extract_atm_reference_ivs(option_chain, spot)
        atm_strike = atm_context.get("atm_strike")
        if atm_strike is None:
            return None

        # Determine side from trend
        if trend == "up":
            side = "CE"
            strike_offset = 1  # 1 strike OTM for call
        elif trend == "down":
            side = "PE"
            strike_offset = 1  # 1 strike OTM for put
        else:
            # Neutral: use composite or skip
            return None

        # Select strike
        strikes = sorted([float(k) for k in option_chain.keys()])
        idx = strikes.index(atm_strike) if atm_strike in strikes else len(strikes) // 2

        if side == "CE":
            strike = strikes[min(idx + strike_offset, len(strikes) - 1)]
        else:
            strike = strikes[max(idx - strike_offset, 0)]

        strike_data = option_chain.get(str(strike))
        opt = (strike_data or {}).get(side.lower()) if strike_data else None
        if not opt:
            return None

        # Check liquidity
        oi = opt.get("oi", 0) or 0
        vol = opt.get("volume", 0) or 0
        if oi < IV_PERCENTILE_CONFIG["MIN_OI"]:
            return None
        if vol < IV_PERCENTILE_CONFIG["MIN_VOLUME"]:
            return None

        # Get pricing
        pricing = self.scanner.get_execution_prices(opt)
        entry = pricing.get("ask") or pricing.get("mid_price") or 0

        if entry <= 0:
            return None
        if entry < IV_PERCENTILE_CONFIG["MIN_OPTION_PREMIUM"]:
            return None

        # Check spread
        spread = self.scanner._spread_pct(pricing.get("bid"), pricing.get("ask"), pricing.get("mid_price"))
        if spread > IV_PERCENTILE_CONFIG["MAX_SPREAD_PCT"]:
            return None

        confidence = min(100, (IV_PERCENTILE_CONFIG["BUY_ZONE"] - percentile) * 2 + 50)

        return {
            "symbol": symbol,
            "security_id": security_id,
            "exchange_segment": segment,
            "strategy": f"IV_BUY_{side} (IVP {percentile:.1f}%)",
            "signal_type": "BUY",
            "direction": "long",
            "side": side,
            "percentile": round(percentile, 1),
            "zscore": round(zscore, 2),
            "confidence": round(confidence, 1),
            "expiry": expiry,
            "dte": dte,
            "spot": round(spot, 2),
            "strike": strike,
            "entry": round(entry, 2),
            "bid": round(pricing.get("bid", 0), 2),
            "ask": round(pricing.get("ask", 0), 2),
            "delta": round(abs(opt.get("greeks", {}).get("delta", 0) or 0), 3),
            "oi": oi,
            "volume": vol,
            "sl": round(entry * IV_PERCENTILE_CONFIG["BUY_SL_MULTIPLIER"], 2),
            "target": round(entry * IV_PERCENTILE_CONFIG["BUY_TARGET_MULTIPLIER"], 2),
        }

    def _get_expiry(self, security_id, segment):
        """Get suitable expiry within DTE range."""
        expiries = self.scanner.get_expiry_list(security_id, segment)
        if not expiries:
            return None

        for expiry in expiries:
            dte = self.scanner.days_to_expiry(expiry)
            if IV_PERCENTILE_CONFIG["DTE_MIN"] <= dte <= IV_PERCENTILE_CONFIG["DTE_MAX"]:
                return expiry

        # Fallback: nearest expiry within range
        for expiry in expiries:
            if self.scanner.days_to_expiry(expiry) >= IV_PERCENTILE_CONFIG["DTE_MIN"]:
                return expiry

        return expiries[0]

    def scan_all(self, securities: dict) -> list:
        """Scan all securities for IV percentile signals."""
        results = []
        self._signals_today = 0

        for sec_id, symbol in securities.items():
            # Check daily limit
            if self._signals_today >= IV_PERCENTILE_CONFIG["MAX_SIGNALS_PER_DAY"]:
                break

            try:
                signal = self.scan_underlying(sec_id, symbol)
                if signal:
                    results.append(signal)
                    self._signals_today += 1
                time.sleep(0.5)  # Rate limit
            except Exception as e:
                logger.error(f"Error scanning {symbol}: {e}")

        # Rank by confidence
        results.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        return results[:IV_PERCENTILE_CONFIG["MAX_SIGNALS_PER_DAY"]]

    def _leg_signal(self, signal: dict, side: str, strike: float, entry: float) -> dict:
        """Build one paper_trader-shaped row for a single strangle leg.

        Sold premium: SL sits above entry (loss if the leg runs up), target
        sits below entry (profit as the written option decays) - same
        SELL_SL_MULTIPLIER / SELL_TARGET_MULTIPLIER the strategy already uses
        for the combined credit, applied per leg since paper_trader tracks
        each option position as its own row (direction="short" is the
        existing IV-seller convention in paper_trader.new_trade_runtime).
        """
        return {
            "symbol": signal["symbol"],
            "security_id": signal["security_id"],
            "exchange_segment": signal.get("exchange_segment"),
            "strategy": signal["strategy"],
            "direction": "short",
            "side": side,
            "strike": strike,
            "expiry": signal["expiry"],
            "dte": signal["dte"],
            "spot": signal["spot"],
            "entry": entry,
            "sl": round(entry * IV_PERCENTILE_CONFIG["SELL_SL_MULTIPLIER"], 2),
            "t1": round(entry * IV_PERCENTILE_CONFIG["SELL_TARGET_MULTIPLIER"], 2),
            "t2": round(entry * IV_PERCENTILE_CONFIG["SELL_TARGET_MULTIPLIER"], 2),
            "iv_rank": signal["percentile"],
            "iv_zscore": signal["zscore"],
            "confidence": signal["confidence"],
            "max_risk_rupees": IV_PERCENTILE_CONFIG["MAX_RISK_RUPEES"],
            "min_premium": IV_PERCENTILE_CONFIG["MIN_OPTION_PREMIUM"],
        }

    def run_paper_trade(self, signal: dict) -> dict:
        """Book a paper trade from a signal via the shared paper book
        (OrderManager.submit_external_signal), same path Break & Bounce and
        other non-discount strategies use so trades land in the same book /
        monitor / EOD / analytics pipeline with this strategy's own tag.
        """
        try:
            if signal["direction"] == "short":
                # Strangle: two independent legs sharing a combo_id, so the
                # dashboard/EOD can still show them as one position.
                combo_id = f"{signal['symbol']}_{signal['expiry']}_{uuid.uuid4().hex[:6]}"
                booked_legs = []
                for leg in signal["legs"]:
                    leg_sig = self._leg_signal(signal, leg["side"], leg["strike"], leg["entry"])
                    leg_sig["combo_id"] = combo_id
                    booked = self.order_manager.submit_external_signal(leg_sig)
                    if booked:
                        booked_legs.append(booked)

                if not booked_legs:
                    return {"status": "rejected", "reason": "no leg passed gates/allowlist"}
                return {"status": "booked", "legs": booked_legs, "combo_id": combo_id}

            else:
                trade = {
                    "symbol": signal["symbol"],
                    "security_id": signal["security_id"],
                    "exchange_segment": signal.get("exchange_segment"),
                    "strategy": signal["strategy"],
                    "direction": "long",
                    "side": signal["side"],
                    "strike": signal["strike"],
                    "expiry": signal["expiry"],
                    "dte": signal["dte"],
                    "spot": signal["spot"],
                    "entry": signal["entry"],
                    "sl": signal["sl"],
                    "t1": signal["target"],
                    "t2": signal["target"],
                    "iv_rank": signal["percentile"],
                    "iv_zscore": signal["zscore"],
                    "confidence": signal["confidence"],
                    "max_risk_rupees": IV_PERCENTILE_CONFIG["MAX_RISK_RUPEES"],
                    "min_premium": IV_PERCENTILE_CONFIG["MIN_OPTION_PREMIUM"],
                }

                booked = self.order_manager.submit_external_signal(trade)
                if not booked:
                    return {"status": "rejected", "reason": "gate/allowlist refused signal"}
                return {"status": "booked", "trade": booked}

        except Exception as e:
            logger.error(f"Failed to book trade for {signal.get('symbol')}: {e}")
            return {"status": "error", "reason": str(e)}
