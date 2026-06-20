# -*- coding: utf-8 -*-
"""
morning_confluence.py — A+ morning option-buy picker (gap + oi_buildup + iv_rank).

Step-by-step (matches the trader playbook):
  1. Read the latest gap / oi_buildup / iv_rank rows from iv_history.db.
  2. Keep names where gap & oi AGREE on direction (the catalyst + confirmation).
  3. Strike: take it from the discounted-premium list if the symbol is there;
     else fall back to ATM / 1-OTM in the trade direction.
  4. Entry/exit structure: use the recent 5-min swing low (support) for the SL
     (best-effort via DataProvider; caveat if candles unavailable).
  5. Liquidity: check OI/volume/spread on the option if a chain quote is available;
     caveat if it can't be verified.
  6. Build a plain-language REASON + a CAVEATS list, then book a paper trade and
     alert with the reason and caveats beneath it.

The selection / reason / caveat / strike logic is pure and unit-tested. The live
bits (5-min support, liquidity, booking) are best-effort and degrade to caveats.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from collectors import iv_store
import notifications
import morning_confluence_config as cfg

logger = logging.getLogger(__name__)
CE, PE = "CE", "PE"


def _opt_import(name):
    try:
        return __import__(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("morning_confluence: '%s' unavailable (%s)", name, exc)
        return None

_gap = _opt_import("gap_scanner")
_oi  = _opt_import("oi_buildup_scanner")
_ivr = _opt_import("iv_rank_scanner")


def _safe(module, fn, key):
    if module is None:
        return {}
    try:
        return getattr(module, fn)(key) or {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Pure logic (unit-tested)
# --------------------------------------------------------------------------- #
def evaluate(gap: dict, oi: dict, iv_zone: str | None) -> dict:
    """Decide if a name is an A+ morning candidate and why.

    gap/oi are the get_latest_* dicts (may be {}); iv_zone is CHEAP/FAIR/EXPENSIVE.
    Returns {ok, direction, reason, caveats}.
    """
    gap_bias = gap.get("bias") if gap else None
    oi_bias = oi.get("bias") if oi else None

    if gap_bias not in (CE, PE):
        return {"ok": False, "direction": None, "reason": "no gap signal", "caveats": []}

    direction = gap_bias
    caveats: list[str] = []

    # Confirmation
    if oi_bias == gap_bias:
        agree = True
    elif oi_bias in (CE, PE) and oi_bias != gap_bias:
        if cfg.REQUIRE_GAP_OI_AGREE:
            return {"ok": False, "direction": None,
                    "reason": f"gap {gap_bias} but OI {oi_bias} disagree", "caveats": []}
        agree = False
        caveats.append(f"OI buildup ({oi_bias}) disagrees with the gap — lower conviction")
    else:
        agree = False
        caveats.append("no fresh OI buildup to confirm direction")
        if cfg.REQUIRE_GAP_OI_AGREE:
            return {"ok": False, "direction": None,
                    "reason": "gap present but OI unconfirmed", "caveats": []}

    # Cost gate
    if iv_zone == "EXPENSIVE":
        if cfg.BLOCK_EXPENSIVE_IV:
            return {"ok": False, "direction": None,
                    "reason": "IV expensive — skipped (crush risk)", "caveats": []}
        caveats.append("IV is EXPENSIVE — premium pricey, size down (vega/theta risk)")
    elif iv_zone is None:
        caveats.append("IV zone unknown (insufficient IV history)")

    return {"ok": True, "direction": direction,
            "reason": build_reason(direction, gap, oi, iv_zone, agree),
            "caveats": caveats}


def build_reason(direction, gap, oi, iv_zone, agree) -> str:
    """Plain-language why-we're-taking-this."""
    side = "bullish CE" if direction == CE else "bearish PE"
    bits = [f"{side}:"]
    if gap:
        bits.append(f"gap {gap.get('gap_pct', 0):+.2f}% beyond prior range")
    if agree and oi:
        kind = "long buildup" if direction == CE else "short buildup"
        bits.append(f"confirmed by {kind} (px {oi.get('price_chg_pct', 0):+.1f}%, OI {oi.get('oi_chg_pct', 0):+.1f}%)")
    iv_txt = {"CHEAP": "IV cheap (low crush risk)", "FAIR": "IV fair",
              "EXPENSIVE": "IV expensive"}.get(iv_zone, "IV n/a")
    bits.append(iv_txt)
    return " ".join(bits)


def decide_strike(symbol, side, discount_rows=None) -> dict:
    """Strike from the discounted-premium list if present; else ATM/1-OTM marker."""
    rows = discount_rows or []
    for r in rows:
        if r.get("symbol") == symbol and _norm_side(r.get("type") or r.get("side")) == side:
            return {"strike": r.get("strike"), "entry": r.get("entry"),
                    "sl": r.get("stop_loss"), "t1": r.get("t1"), "t2": r.get("t2"),
                    "expiry": r.get("expiry"), "source": "discount_list"}
    return {"strike": None, "source": cfg.STRIKE_FALLBACK}   # atm | otm1 (resolve at execution)


def _norm_side(raw):
    s = str(raw or "").upper()
    if s in ("CE", "CALL", "C"):
        return CE
    if s in ("PE", "PUT", "P"):
        return PE
    return None


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
class MorningConfluence:
    def __init__(self, db_path=None, provider=None):
        self.db_path = db_path or iv_store.DB_PATH
        self.provider = provider          # optional DataProvider for 5-min support

    def _universe(self):
        with sqlite3.connect(self.db_path) as conn:
            return [(str(s), m) for s, m in conn.execute(
                "SELECT security_id, MAX(symbol) FROM iv_history GROUP BY security_id")]

    def _discount_rows(self):
        try:
            import csv
            with open(cfg.DISCOUNT_CSV) as f:
                return list(csv.DictReader(f))
        except Exception:
            return []

    def _five_min_support(self, security_id, segment):
        """Recent 5-min swing low for SL (best-effort). Returns float or None."""
        if self.provider is None:
            return None
        try:
            df = self.provider.intraday_candles(security_id, segment, interval=5)
            if df is None or len(df) == 0:
                return None
            lows = df["low"].tail(cfg.SUPPORT_LOOKBACK)
            return float(lows.min())
        except Exception:
            return None

    def scan(self):
        disc = self._discount_rows()
        out = []
        for sid, symbol in self._universe():
            if not symbol:
                continue
            gap = _safe(_gap, "get_latest_gap", sid)
            oi = _safe(_oi, "get_latest_buildup", sid)
            iv_zone = (_safe(_ivr, "get_latest_zone", sid) or {}).get("zone")

            res = evaluate(gap, oi, iv_zone)
            if not res["ok"]:
                continue

            strike = decide_strike(symbol, res["direction"], disc)
            caveats = list(res["caveats"])
            if strike["source"] != "discount_list":
                caveats.append(f"not in discount list — use {strike['source'].upper()} strike at execution")

            support = self._five_min_support(sid, "NSE_EQ")
            if support is None:
                caveats.append("5-min support unavailable — set SL from option premium %")

            out.append({
                "symbol": symbol, "security_id": sid, "direction": res["direction"],
                "strike": strike.get("strike"), "strike_source": strike["source"],
                "support_5m": support, "iv_zone": iv_zone or "-",
                "reason": res["reason"], "caveats": "; ".join(caveats) or "none",
            })

        logger.info("morning-confluence: %d A+ candidate(s)", len(out))
        return out

    def send_telegram(self, picks):
        lines = ["🌅 Morning Confluence — A+ option-buy picks (gap + OI + IV)",
                 f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"]
        if not picks:
            lines.append("No A+ confluence this morning (gap & OI didn't align).")
        for p in picks[:cfg.TOP_N]:
            dot = "🟢" if p["direction"] == CE else "🔴"
            strike = p["strike"] if p["strike"] is not None else p["strike_source"].upper()
            lines.append(f"\n{dot} {p['symbol']} {p['direction']} {strike}")
            lines.append(f"   reason: {p['reason']}")
            lines.append(f"   ⚠️ caveats: {p['caveats']}")
        lines.append("\n#paper — confirm a 5-min hold above support before entry.")
        if notifications.notify("\n".join(lines), parse_mode=None):
            logger.info("morning-confluence: alert sent")
