# -*- coding: utf-8 -*-
"""
vol_expansion_strategy.py — paper strategy for the 4-day IV-slope expansion
signal (dashboard section II).

Long premium on names whose daily ATM IV is climbing (positive slope) while
still cheap on 52-wk history (IVP buy zone). The signal is direction-agnostic
(vega); we book a DIRECTIONAL single leg, picking CE/PE from the underlying's
recent daily trend, and skip names with no clear lean (REQUIRE_TREND).

Booked through OrderManager.submit_external_signal into the SHARED paper book,
so trades ride the same monitor / fill alerts / auto-exit / EOD / analytics as
discount and B&B. No real orders are ever placed (paper only).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime

import iv_analytics
import notifications
import order_manager
import vol_expansion_config as CFG
# NOTE: discount / momentum_strategy are imported lazily inside methods so this
# module (and its pure helpers) can be imported without the broker SDK present.

logger = logging.getLogger(__name__)

INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
IV_DB = os.path.join(os.path.dirname(__file__), "data", "iv_history.db")


def exchange_segment(symbol: str) -> str:
    return "IDX_I" if symbol in INDEX_SYMBOLS else "NSE_FNO"


def underlying_bias(symbol: str, min_move_pct: float = 1.0, lookback: int = 6) -> str | None:
    """CE / PE / None from the underlying's recent daily spot trend in
    iv_history (zero broker calls). A move inside +/-min_move_pct reads as no
    trend (None) — don't force a directional bet on a pure-vega signal."""
    try:
        with sqlite3.connect(IV_DB) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            rows = conn.execute(
                "SELECT spot_price FROM iv_history "
                "WHERE symbol = ? AND data_type = 'daily' AND spot_price > 0 "
                "ORDER BY timestamp DESC LIMIT ?",
                (symbol, lookback),
            ).fetchall()
    except sqlite3.Error:
        return None
    vals = [r[0] for r in rows][::-1]  # oldest -> newest
    if len(vals) < 3 or not vals[0]:
        return None
    change_pct = (vals[-1] - vals[0]) / vals[0] * 100
    if change_pct >= min_move_pct:
        return "CE"
    if change_pct <= -min_move_pct:
        return "PE"
    return None


def select_atm_option(oc: dict, spot: float, side: str, offset: int = 0):
    """Return (strike, option_dict) for the ATM(+offset) strike on `side`."""
    if not oc or not spot or spot <= 0:
        return None
    strikes = sorted(float(k) for k in oc.keys())
    if not strikes:
        return None
    gaps = [strikes[i + 1] - strikes[i] for i in range(min(5, len(strikes) - 1))]
    gap = max(set(gaps), key=gaps.count) if gaps else 50.0
    atm = round(spot / gap) * gap
    target = atm + offset * gap if side == "CE" else atm - offset * gap
    closest = min(strikes, key=lambda s: abs(s - target))
    key = next((k for k in oc.keys() if float(k) == closest), None)
    if key is None:
        return None
    sub = (oc.get(key) or {}).get("ce" if side == "CE" else "pe") or {}
    if not sub:
        return None
    return closest, sub


class VolExpansionStrategy:
    """Scan buy-zone expansion names and book long-premium paper trades."""

    def __init__(self, scanner=None):
        from discount import DiscountedPremiumScanner
        from momentum_strategy import ScripMasterLotSizer
        self.scanner = scanner or DiscountedPremiumScanner()
        self.lot_sizer = ScripMasterLotSizer()
        self.bot_token = getattr(self.scanner, "telegram_bot_token", None)
        self.chat_id = getattr(self.scanner, "telegram_chat_id", None)
        self.order_manager = order_manager.OrderManager(
            bot_token=self.bot_token, chat_id=self.chat_id
        )
        # symbol -> underlying security_id (fno_stocks is {sec_id: symbol})
        self._sym_to_sid = {v: k for k, v in (self.scanner.fno_stocks or {}).items()}
        self._traded_today: set[str] = set()
        self._traded_day: str | None = None

    # -- candidate feed ------------------------------------------------------ #
    def candidates(self) -> list[dict]:
        if CFG.BUY_ZONE_ONLY:
            data = iv_analytics.buy_zone_leaderboard(
                lookback_days=CFG.LOOKBACK_DAYS, scan_n=CFG.MAX_SCAN,
                limit=CFG.MAX_SCAN, min_slope=CFG.MIN_SLOPE,
            )
            return data.get("symbols", [])
        data = iv_analytics.vol_expansion(lookback_days=CFG.LOOKBACK_DAYS, top_n=CFG.MAX_SCAN)
        return [r for r in data.get("symbols", []) if r.get("expanding")]

    # -- lifecycle ----------------------------------------------------------- #
    def _reset_if_new_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self._traded_day != today:
            self._traded_day = today
            self._traded_today = set()

    def run_scan(self, now: datetime | None = None) -> list[dict]:
        """One scan pass. Returns the list of booked (or alerted) signals."""
        if CFG.MODE == "off":
            return []
        now = now or datetime.now()
        self._reset_if_new_day(now)
        if now.strftime("%H:%M") >= CFG.ENTRY_CUTOFF:
            logger.info("VolExp: past entry cutoff %s — no new trades", CFG.ENTRY_CUTOFF)
            return []

        out = []
        for row in self.candidates():
            if len(self._traded_today) >= CFG.MAX_TRADES_PER_DAY:
                break
            symbol = row.get("symbol")
            if not symbol or symbol in self._traded_today:
                continue
            sid = self._sym_to_sid.get(symbol)
            if not sid:
                logger.info("VolExp: no security_id for %s — skip", symbol)
                continue

            side = underlying_bias(symbol, CFG.MIN_MOVE_PCT, CFG.TREND_LOOKBACK)
            if side is None:
                if CFG.REQUIRE_TREND:
                    logger.info("VolExp: %s expanding but no clear trend — skip", symbol)
                    continue
                side = "CE"

            sig = self._build_signal(symbol, sid, side, row, now)
            if not sig:
                continue

            if CFG.MODE == "alert":
                self._alert(sig)
                out.append(sig)
                self._traded_today.add(symbol)
            else:  # paper — submit_external_signal fires the PAPER-TRADE-TAKEN alert
                booked = self.order_manager.submit_external_signal(sig, now=now)
                if booked:
                    logger.info("VolExp booked %s %s K%s [%s]", symbol, side,
                                sig["strike"], CFG.STRATEGY_TAG)
                    out.append(booked)
                    self._traded_today.add(symbol)
                else:
                    logger.info("VolExp: %s %s not booked (gate/guard)", symbol, side)
        return out

    # -- signal construction ------------------------------------------------- #
    def _build_signal(self, symbol, sid, side, row, now):
        from discount import unwrap_dhan_payload, get_trading_days_to_expiry
        seg = exchange_segment(symbol)
        try:
            expiries = [e for e in self.scanner.get_expiry_list(sid, seg)
                        if get_trading_days_to_expiry(e) >= CFG.MIN_DTE]
            if not expiries:
                return None
            expiry = expiries[0]
            resp = self.scanner.get_option_chain(sid, seg, expiry)
            if not (isinstance(resp, dict) and resp.get("status") == "success"):
                return None
            cd = unwrap_dhan_payload(resp.get("data") or {})
            spot = cd.get("last_price", 0)
            oc = cd.get("oc", {})
            picked = select_atm_option(oc, spot, side, CFG.STRIKE_OTM_OFFSET)
            if not picked:
                return None
            strike, opt = picked

            px = self.scanner.get_execution_prices(opt)
            entry = px.get("entry_price") or px.get("mid_price") or 0.0
            bid, ask = px.get("bid"), px.get("ask")
            if not entry or entry < CFG.MIN_PREMIUM:
                return None

            oi = int(opt.get("oi") or 0)
            volume = int(opt.get("volume") or 0)
            iv = opt.get("implied_volatility")
            mid = (bid + ask) / 2 if (bid and ask) else entry
            spread_pct = abs(ask - bid) / mid if (bid and ask and mid) else 1.0
            if oi < CFG.LIQ_MIN_OI or volume < CFG.LIQ_MIN_VOLUME or spread_pct > CFG.LIQ_MAX_SPREAD:
                logger.info("VolExp liquidity fail %s: oi=%s vol=%s spread=%.1f%%",
                            symbol, oi, volume, spread_pct * 100)
                return None

            lot_size = self.lot_sizer.get(symbol)
            try:
                opt_sid = self.lot_sizer.get_option_security_id(symbol, expiry, strike, side) or ""
            except Exception:
                opt_sid = ""

            entry = round(float(entry), 2)
            return {
                "symbol":            symbol,
                "security_id":       sid,          # underlying — monitor re-looks up the option
                "exchange_segment":  seg,
                "side":              side,
                "strike":            strike,
                "expiry":            expiry,
                "spot":              spot,
                "entry":             entry,
                "sl":                round(entry * (1 - CFG.SL_PCT), 2),
                "t1":                round(entry * CFG.T1_MULT, 2),
                "t2":                round(entry * CFG.T2_MULT, 2),
                "t1_book_fraction":  CFG.T1_BOOK_FRACTION,
                "lot_size":          lot_size,
                "iv":                iv,
                "bid":               bid,
                "ask":               ask,
                "oi":                oi,
                "volume":            volume,
                "option_security_id": opt_sid,
                "min_premium":       CFG.MIN_PREMIUM,
                "strategy":          CFG.STRATEGY_TAG,
                "iv_slope":          row.get("slope_iv_pts_per_day"),
                "iv_percentile":     row.get("iv_percentile"),
            }
        except Exception:
            logger.exception("VolExp _build_signal failed for %s", symbol)
            return None

    # -- alerts -------------------------------------------------------------- #
    def _alert(self, sig) -> None:
        try:
            notifications.notify(
                f"\U0001F4C8 <b>VOL-EXPANSION signal</b> ({sig['side']})\n"
                f"{sig['symbol']} K{sig['strike']} @ Rs {sig['entry']:.2f}\n"
                f"IV slope {sig.get('iv_slope')}/d · IVP {sig.get('iv_percentile')}\n"
                f"SL {sig['sl']:.2f} · T1 {sig['t1']:.2f} · T2 {sig['t2']:.2f}\n"
                f"(alert-only — set VOL_EXP_MODE=paper to book)",
                bot_token=self.bot_token, chat_id=self.chat_id,
            )
        except Exception:
            logger.exception("VolExp alert failed for %s", sig.get("symbol"))
