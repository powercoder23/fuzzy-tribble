# -*- coding: utf-8 -*-
"""
trade_suggester.py — Intraday Trade Suggester (soft / rank-only).

The alert-only scanners (gap, oi_buildup, iv_rank, smart_money, delivery_surge)
give a *bias on the underlying* but no orderable contract. The discount scanner
gives an *orderable contract* (strike, entry, SL, T1/T2) but doesn't look across
the other scans. This module fuses them: it takes discount's candidates and
re-ranks each by how strongly the scanners AGREE with that trade's direction,
producing a single ranked "suggested trades" list.

SOFT by design: nothing is filtered out. Confluence only re-orders and scales the
score (suggestion_score = discount_score * (1 + agree_sum * CONFLUENCE_GAIN)),
so a discount trade with no scanner support still shows — just lower.

Zero broker calls: reads only the persisted *_history tables via each scanner's
get_latest_* helper (fail-open if a table/module is missing).

Public surface
    score_candidate(side, factors, discount_score, vix_regime) -> dict  (pure)
    TradeSuggester().suggest(opportunities) -> ranked DataFrame
    TradeSuggester().suggest_and_alert(opportunities)
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

import pandas as pd

from collectors import iv_store
import notifications
import trade_suggester_config as cfg

logger = logging.getLogger(__name__)

CE, PE = "CE", "PE"


# --------------------------------------------------------------------------- #
# Defensive factor imports (one broken scanner must not break the suggester)
# --------------------------------------------------------------------------- #
def _opt_import(name):
    try:
        return __import__(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trade_suggester: factor '%s' unavailable (%s)", name, exc)
        return None

_oi    = _opt_import("oi_buildup_scanner")
_gap   = _opt_import("gap_scanner")
_smart = _opt_import("smart_money_scanner")
_deliv = _opt_import("delivery_surge_scanner")
_ivr   = _opt_import("iv_rank_scanner")


def _safe(module, fnname, key):
    if module is None:
        return {}
    try:
        return getattr(module, fnname)(key) or {}
    except Exception:
        return {}


def _norm_side(raw) -> str | None:
    s = str(raw or "").upper()
    if s in ("CE", "CALL", "C"):
        return CE
    if s in ("PE", "PUT", "P"):
        return PE
    return None


# --------------------------------------------------------------------------- #
# Pure scoring (no I/O — unit-testable)
# --------------------------------------------------------------------------- #
def score_candidate(side: str, factors: dict, discount_score: float,
                    vix_regime: str = "NORMAL") -> dict:
    """Re-rank one discount candidate by scanner confluence.

    `factors` keys (any may be None): oi, gap, smart, deliv -> {"bias","strength"};
    iv_zone -> "CHEAP"|"FAIR"|"EXPENSIVE"|None.
    Returns suggestion_score and an agreement breakdown. SOFT: never excludes.
    """
    weights = {"oi": cfg.W_OI_BUILDUP, "smart": cfg.W_SMART_MONEY,
               "deliv": cfg.W_DELIVERY_SURGE, "gap": cfg.W_GAP}
    total_w = sum(weights.values()) or 1.0

    signed = 0.0
    agree, disagree = [], []
    for key, w in weights.items():
        f = factors.get(key)
        if not f or f.get("bias") not in (CE, PE):
            continue
        strength = max(0.0, min(1.0, float(f.get("strength", 1.0) or 1.0)))
        if f["bias"] == side:
            signed += w * strength
            agree.append(key)
        else:
            signed -= w * strength * cfg.DISAGREE_FACTOR
            disagree.append(key)

    agree_sum = max(-1.0, min(1.0, signed / total_w))   # [-1, +1]

    # IV-rank cost modifier (additive into agree_sum, then clipped).
    iv_zone = factors.get("iv_zone")
    if iv_zone == "CHEAP":
        agree_sum = min(1.0, agree_sum + cfg.IV_CHEAP_BOOST)
    elif iv_zone == "EXPENSIVE":
        agree_sum = max(-1.0, agree_sum - cfg.IV_EXPENSIVE_PEN)

    # VIX regime modifier.
    if vix_regime == "ELEVATED":
        agree_sum = max(-1.0, agree_sum - cfg.VIX_HIGH_PENALTY)
    elif vix_regime == "CALM":
        agree_sum = min(1.0, agree_sum + cfg.VIX_LOW_BOOST)

    ds = float(discount_score or 0.0)
    suggestion_score = max(0.0, ds * (1.0 + agree_sum * cfg.CONFLUENCE_GAIN))

    n_agree = len(agree)
    confidence = "HIGH" if n_agree >= cfg.HIGH_CONF_AGREE else "MED" if n_agree >= 1 else "LOW"

    return {
        "suggestion_score": round(suggestion_score, 1),
        "discount_score": round(ds, 1),
        "agree_sum": round(agree_sum, 3),
        "n_agree": n_agree,
        "n_disagree": len(disagree),
        "agree": ",".join(agree) or "-",
        "disagree": ",".join(disagree) or "-",
        "iv_zone": iv_zone or "-",
        "vix_regime": vix_regime,
        "confidence": confidence,
    }


# --------------------------------------------------------------------------- #
# Factor normalisers (scanner dict -> {bias, strength})
# --------------------------------------------------------------------------- #
def _n_oi(d):
    if not d or d.get("bias") not in (CE, PE):
        return None
    return {"bias": d["bias"], "strength": 1.0 if str(d.get("strength", "")).upper() == "STRONG" else 0.6}

def _n_gap(d):
    if not d or d.get("bias") not in (CE, PE):
        return None
    return {"bias": d["bias"], "strength": 1.0 if d.get("extreme") else 0.7}

def _n_deliv(d):
    if not d or d.get("bias") not in (CE, PE):
        return None
    surge = float(d.get("surge_x") or 1.0)
    return {"bias": d["bias"], "strength": max(0.5, min(1.0, (surge - 1.0) / 2.0 + 0.5))}

def _n_smart(d):
    if not d or d.get("bias") not in (CE, PE):
        return None
    net = abs(float(d.get("net_value_cr") or 0.0))
    return {"bias": d["bias"], "strength": max(0.5, min(1.0, net / 20.0))}


# --------------------------------------------------------------------------- #
# Suggester
# --------------------------------------------------------------------------- #
class TradeSuggester:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or iv_store.DB_PATH

    def _vix_regime(self) -> str:
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT close FROM vix_daily ORDER BY date DESC LIMIT 1"
                ).fetchone()
        except sqlite3.OperationalError:
            return "NORMAL"
        if not row or row[0] is None:
            return "NORMAL"
        vix = float(row[0])
        if vix >= cfg.VIX_HIGH:
            return "ELEVATED"
        if vix <= cfg.VIX_LOW:
            return "CALM"
        return "NORMAL"

    def _factors_for(self, security_id, symbol) -> dict:
        sid = str(security_id)
        return {
            "oi":      _n_oi(_safe(_oi, "get_latest_buildup", sid)),
            "gap":     _n_gap(_safe(_gap, "get_latest_gap", sid)),
            "smart":   _n_smart(_safe(_smart, "get_latest_smart_money", symbol)),
            "deliv":   _n_deliv(_safe(_deliv, "get_latest_surge", symbol)),
            "iv_zone": (_safe(_ivr, "get_latest_zone", sid) or {}).get("zone"),
        }

    def suggest(self, opportunities) -> pd.DataFrame:
        """Rank discount candidates by scanner confluence. `opportunities` is the
        discount scan DataFrame/list (or None -> read OUTPUT of last scan CSV)."""
        if opportunities is None:
            try:
                opportunities = pd.read_csv("data/discounted_premiums.csv")
            except Exception:
                return pd.DataFrame()

        rows = opportunities.to_dict("records") if hasattr(opportunities, "to_dict") else list(opportunities or [])
        vix_regime = self._vix_regime()

        out = []
        for r in rows:
            side = _norm_side(r.get("type") or r.get("side"))
            if side is None or r.get("strike") is None or not r.get("entry"):
                continue  # not an orderable candidate
            factors = self._factors_for(r.get("security_id"), r.get("symbol"))
            res = score_candidate(side, factors, r.get("score", 0), vix_regime)
            out.append({
                "symbol": r.get("symbol"),
                "security_id": r.get("security_id"),
                "side": side,
                "strike": r.get("strike"),
                "entry": r.get("entry"),
                "sl": r.get("stop_loss"),
                "t1": r.get("t1"),
                "t2": r.get("t2"),
                **res,
            })

        if not out:
            logger.info("trade-suggester: no orderable discount candidates this cycle")
            return pd.DataFrame()

        df = pd.DataFrame(out).sort_values("suggestion_score", ascending=False).reset_index(drop=True)
        logger.info("trade-suggester: ranked %d candidates | top=%s %.0f (%s, %dagree)",
                    len(df), df.iloc[0]["symbol"], df.iloc[0]["suggestion_score"],
                    df.iloc[0]["confidence"], df.iloc[0]["n_agree"])
        return df

    def suggest_and_alert(self, opportunities) -> pd.DataFrame:
        df = self.suggest(opportunities)
        if df.empty:
            return df
        try:
            df.to_csv(cfg.OUTPUT_CSV, index=False)
        except Exception:
            logger.exception("trade-suggester: CSV write failed")
        # Entry-only alerting policy: the suggester writes CSV every cycle but
        # only pushes Telegram if explicitly enabled (TS_ALERT=true).
        if cfg.ALERT:
            self.send_telegram(df)
        return df

    def send_telegram(self, df: pd.DataFrame) -> None:
        lines = [
            "🧭 Trade Suggestions (discount × scanner confluence)",
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')} — soft rank, not filtered",
        ]
        for _, r in df.head(cfg.TOP_N_ALERT).iterrows():
            dot = "🟢" if r["side"] == CE else "🔴"
            lines.append(
                f"{dot} {r['symbol']:<11} {r['side']} {float(r['strike']):.0f} "
                f"| score {r['suggestion_score']:.0f} ({r['confidence']}) "
                f"| {r['n_agree']}✓/{r['n_disagree']}✗ [{r['agree']}] IV {r['iv_zone']}"
            )
        lines.append("\nℹ️ Discount supplies the contract; scanners rank conviction. Confirm entry.")
        if notifications.notify("\n".join(l