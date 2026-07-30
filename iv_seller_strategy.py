"""
iv_seller_strategy.py — IV-based short premium scanner (strangle/straddle).

Uses the IV history already being collected (collectors/iv_store.py ->
iv_history.db) to find symbols whose IV is RICH relative to their own recent
history, then sells premium expecting IV / price mean-reversion — the mirror
image of directional_iv_strategy.py (which buys CHEAP IV) and discount.py
(which also buys, scored on cheap-vs-peers skew).

Reuses existing infrastructure rather than reimplementing it:
  * DiscountedPremiumScanner (discount.py) for option-chain / expiry / greeks
    access — same composition pattern DirectionalIVScanner already uses.
  * iv_rank_scanner.iv_percentile() (pure function) for the rank math.
  * collectors.iv_store for the historical-IV read (zero broker API calls to
    find candidates — only qualifying symbols hit the option-chain API).

Structure is chosen dynamically per symbol based on how rich the IV is
(2026-07-29 decision): STRADDLE_MIN_PCT or richer -> ATM short straddle
(bigger credit, bigger risk); SELL_ZONE_MIN..STRADDLE_MIN_PCT -> OTM short
strangle at STRANGLE_DELTA_TARGET (smaller credit, smaller risk, higher POP).
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import pandas as pd

from discount import DiscountedPremiumScanner, unwrap_dhan_payload
from collectors import iv_store
from iv_rank_scanner import iv_percentile
import iv_seller_config as cfg

logger = logging.getLogger(__name__)


def _passes_liquidity(opt: dict) -> bool:
    oi = opt.get("oi", 0) or 0
    volume = opt.get("volume", 0) or 0
    return oi >= cfg.LIQUIDITY["min_oi"] and volume >= cfg.LIQUIDITY["min_volume"]


def _spread_pct(bid: float, ask: float, mid: float) -> float:
    if not bid or not ask or not mid or mid <= 0:
        return 1.0
    return abs(ask - bid) / mid


class IVSellerScanner:
    """Short premium (strangle/straddle) scanner driven by collected IV history."""

    def __init__(self, hardtoken=None, client_id=None, universe=None,
                 upstox_adapter=None):
        self.scanner = DiscountedPremiumScanner(upstox_adapter=upstox_adapter)
        self.universe = universe  # optional {security_id: symbol} override

    # ---- IV sell-zone candidates (zero broker calls) -----------------------
    def _iv_sell_candidates(self) -> list[dict]:
        """Symbols whose current IV percentile clears SELL_ZONE_MIN, using
        only the locally-collected iv_history.db (no option-chain API calls
        yet — those only happen for symbols that qualify here)."""
        with iv_store.connect() as conn:
            universe = pd.read_sql(
                """
                SELECT security_id, MAX(symbol) AS symbol
                FROM   iv_history
                WHERE  data_type = 'daily' AND atm_iv BETWEEN 1.0 AND 200.0
                GROUP  BY security_id
                """,
                conn,
            )

        candidates = []
        for _, u in universe.iterrows():
            sid = str(u["security_id"])
            symbol = u["symbol"]
            if self.universe is not None and sid not in self.universe:
                continue
            hist = iv_store.get_iv_history(sid, days=cfg.LOOKBACK_DAYS)
            if len(hist) < cfg.MIN_HISTORY_DAYS:
                continue  # fail-open: not enough baseline to rank
            cur = hist[-1]
            pct = iv_percentile(cur, hist)
            if pct is None or pct < cfg.SELL_ZONE_MIN:
                continue
            candidates.append({
                "security_id": sid,
                "symbol": symbol,
                "current_iv": round(cur, 2),
                "iv_percentile": round(pct, 1),
                "hist_days": len(hist),
                "structure": (cfg.STRATEGY_STRADDLE if pct >= cfg.STRADDLE_MIN_PCT
                              else cfg.STRATEGY_STRANGLE),
            })
        candidates.sort(key=lambda c: c["iv_percentile"], reverse=True)
        return candidates

    # ---- expiry / strike selection ------------------------------------------
    def _select_expiry(self, security_id, segment):
        expiries = self.scanner.get_expiry_list(security_id, segment)
        if not expiries:
            return None
        in_band = []
        for expiry in expiries:
            dte = self.scanner.days_to_expiry(expiry)
            if cfg.DTE_MIN <= dte <= cfg.DTE_MAX:
                in_band.append((dte, expiry))
        if in_band:
            in_band.sort(key=lambda x: x[0])
            return in_band[0][1]
        # Fall back to the nearest expiry at/after DTE_MIN.
        for expiry in expiries:
            if self.scanner.days_to_expiry(expiry) >= cfg.DTE_MIN:
                return expiry
        return expiries[0]

    def _select_expiry_0dte(self, security_id, segment):
        """The expiry landing TODAY (DTE=0) — no fallback; if today isn't this
        underlying's expiry day, there is no 0DTE trade for it."""
        expiries = self.scanner.get_expiry_list(security_id, segment)
        if not expiries:
            return None
        for expiry in expiries:
            if self.scanner.days_to_expiry(expiry) == 0:
                return expiry
        return None

    def _pick_strangle_leg(self, option_chain, side_key, target_delta):
        """Nearest-delta strike (by |delta - target|) among liquid strikes."""
        best = None
        best_gap = None
        for strike_str, strike_data in option_chain.items():
            opt = (strike_data or {}).get(side_key)
            if not opt or not _passes_liquidity(opt):
                continue
            delta = abs(opt.get("greeks", {}).get("delta", 0) or 0)
            if delta <= 0:
                continue
            pricing = self.scanner.get_execution_prices(opt)
            if _spread_pct(pricing["bid"], pricing["ask"], pricing["mid_price"]) > cfg.LIQUIDITY["max_spread_pct"]:
                continue
            gap = abs(delta - target_delta)
            if best is None or gap < best_gap:
                best, best_gap = (float(strike_str), opt, pricing, delta), gap
        return best

    def _atm_leg(self, option_chain, atm_strike, side_key):
        strike_data = option_chain.get(str(atm_strike)) or option_chain.get(atm_strike)
        if not strike_data:
            # Strike keys can be formatted like "1400.000000" — match by float.
            for key, val in option_chain.items():
                try:
                    if abs(float(key) - atm_strike) < 1e-6:
                        strike_data = val
                        break
                except (TypeError, ValueError):
                    continue
        opt = (strike_data or {}).get(side_key)
        if not opt:
            return None
        pricing = self.scanner.get_execution_prices(opt)
        delta = abs(opt.get("greeks", {}).get("delta", 0) or 0)
        return (float(atm_strike), opt, pricing, delta)

    def _build_leg(self, symbol, security_id, segment, expiry, dte, side_label,
                    strike, pricing, delta, candidate, dte_mode="swing"):
        """One short leg: entry = bid (what selling actually fills at), SL/target
        are credit multiples of that entry (mirrors discount_config.TRADE_PLAN's
        "multiples of entry" style, inverted for a sold position).

        dte_mode="0dte" uses the tighter ZERO_DTE_* multiples (faster gamma =
        cut losses sooner) and tags the strategy label so it's distinguishable
        in the paper book from the 7-15 DTE swing structure."""
        entry = round(pricing["bid"] or pricing["mid_price"] or 0.0, 2)
        if entry <= 0:
            return None
        if dte_mode == "0dte":
            sl_mult, target_mult = cfg.ZERO_DTE_SL_CREDIT_MULT, cfg.ZERO_DTE_TARGET_CREDIT_MULT
            strategy_label = f"{candidate['structure']} [0DTE]"
        else:
            sl_mult, target_mult = cfg.SL_CREDIT_MULT, cfg.TARGET_CREDIT_MULT
            strategy_label = candidate["structure"]
        sl = round(entry * sl_mult, 2)
        target = round(entry * target_mult, 2)
        return {
            "symbol": symbol,
            "security_id": security_id,
            "exchange_segment": segment,
            "side": side_label,
            "strike": strike,
            "expiry": expiry,
            "entry": entry,
            "sl": sl,
            "t1": target,
            "t2": target,          # single SL + single target model; t2 unused live
            "t1_book_fraction": 1.0,
            # Real bid/ask so paper_trader computes the honest half-spread
            # instead of the 2% fallback (same as break_bounce_strategy.py).
            "bid": round(pricing["bid"], 2) if pricing.get("bid") else entry,
            "ask": round(pricing["ask"], 2) if pricing.get("ask") else entry,
            "delta": round(delta, 3),
            "iv": candidate["current_iv"],
            "iv_percentile": candidate["iv_percentile"],
            "dte": dte,
            "direction": "short",
            "structure": candidate["structure"],
            "strategy": strategy_label,
        }

    def scan_underlying(self, candidate: dict, dte_mode: str = "swing") -> dict | None:
        symbol = candidate["symbol"]
        security_id = candidate["security_id"]
        segment = "IDX_I" if symbol in {"NIFTY", "BANKNIFTY"} else "NSE_FNO"

        expiry = (self._select_expiry_0dte(security_id, segment) if dte_mode == "0dte"
                  else self._select_expiry(security_id, segment))
        if not expiry:
            logger.info("IV seller%s: no expiry for %s",
                        " 0DTE" if dte_mode == "0dte" else "", symbol)
            return None
        dte = self.scanner.days_to_expiry(expiry)

        chain_response = self.scanner.get_option_chain(security_id, segment, expiry)
        if chain_response.get("status") != "success":
            return None
        chain_data = unwrap_dhan_payload(chain_response.get("data") or {})
        spot_price = chain_data.get("last_price")
        option_chain = chain_data.get("oc")
        if spot_price is None or not isinstance(option_chain, dict):
            return None

        atm_context = self.scanner.extract_atm_reference_ivs(option_chain, spot_price)
        if (
            (atm_context.get("atm_call_oi") or 0) < cfg.LIQUIDITY["min_atm_oi"]
            and (atm_context.get("atm_put_oi") or 0) < cfg.LIQUIDITY["min_atm_oi"]
        ):
            logger.info("IV seller: low ATM liquidity for %s — skip", symbol)
            return None

        if candidate["structure"] == cfg.STRATEGY_STRADDLE:
            atm_strike = atm_context.get("atm_strike")
            if atm_strike is None:
                return None
            ce = self._atm_leg(option_chain, atm_strike, "ce")
            pe = self._atm_leg(option_chain, atm_strike, "pe")
        else:
            ce = self._pick_strangle_leg(option_chain, "ce", cfg.STRANGLE_DELTA_TARGET)
            pe = self._pick_strangle_leg(option_chain, "pe", cfg.STRANGLE_DELTA_TARGET)

        if not ce or not pe:
            logger.info("IV seller: could not fill both legs for %s (%s)",
                        symbol, candidate["structure"])
            return None

        ce_strike, ce_opt, ce_pricing, ce_delta = ce
        pe_strike, pe_opt, pe_pricing, pe_delta = pe

        ce_leg = self._build_leg(symbol, security_id, segment, expiry, dte, "CE",
                                  ce_strike, ce_pricing, ce_delta, candidate, dte_mode=dte_mode)
        pe_leg = self._build_leg(symbol, security_id, segment, expiry, dte, "PE",
                                  pe_strike, pe_pricing, pe_delta, candidate, dte_mode=dte_mode)
        if not ce_leg or not pe_leg:
            return None

        return {
            "symbol": symbol,
            "security_id": security_id,
            "structure": candidate["structure"],
            "strategy": candidate["structure"],
            "iv_percentile": candidate["iv_percentile"],
            "expiry": expiry,
            "dte": dte,
            "spot": spot_price,
            "legs": [ce_leg, pe_leg],
        }

    def scan_all_underlyings(self) -> list[dict]:
        combos = []
        for candidate in self._iv_sell_candidates():
            try:
                combo = self.scan_underlying(candidate)
                if combo:
                    combos.append(combo)
                time.sleep(1)  # rate-limit, same convention as directional_iv
            except Exception:
                logger.exception("IV seller: scan failed for %s", candidate.get("symbol"))
        combos.sort(key=lambda c: c["iv_percentile"], reverse=True)
        return combos

    def scan_all_underlyings_0dte(self) -> list[dict]:
        """0DTE variant: same rich-IV candidate list, a stricter IV bar
        (ZERO_DTE_SELL_ZONE_MIN), and an expiry that must land today."""
        combos = []
        for candidate in self._iv_sell_candidates():
            if candidate["iv_percentile"] < cfg.ZERO_DTE_SELL_ZONE_MIN:
                continue
            try:
                combo = self.scan_underlying(candidate, dte_mode="0dte")
                if combo:
                    combos.append(combo)
                time.sleep(1)
            except Exception:
                logger.exception("IV seller 0DTE: scan failed for %s", candidate.get("symbol"))
        combos.sort(key=lambda c: c["iv_percentile"], reverse=True)
        return combos
