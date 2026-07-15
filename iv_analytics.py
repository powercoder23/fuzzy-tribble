# -*- coding: utf-8 -*-
"""
iv_analytics.py — IV History Analytics Engine (read-only, zero broker calls).

Computes four statistical edges from data ALREADY collected in iv_history.db.
Used by dashboard_app.py; safe to import anywhere (stdlib only, no pandas).

What each analytic is backed by — and what it is NOT:

I.   IV Percentile (IVP)
     Authoritative source: iv_rank_history.iv_percentile, written by the
     iv-rank scanner (percentile is its PRIMARY_METRIC). This module only
     READS it; if the scanner hasn't run yet it falls back to computing the
     percentile directly from daily iv_history rows. Buyer rule (<20 buy,
     >80 avoid) is evaluated here for display; the trading-side gate remains
     the scanner's zone thresholds (IVR_BUY_ZONE_MAX / IVR_SELECTIVE_MAX).

II.  Volatility Expansion (pre-event proxy)
     NO economic-calendar collector exists in this system, so event dates are
     NOT known. What IS in hand: the 3-4 day slope of daily ATM IV per symbol.
     A steep positive slope IS the "climbing IV into an event" signature —
     detected from data, not from a calendar. Labelled honestly as such.

III. Intraday IV Decay Curve
     Average ATM IV per 15-minute bucket over the last N sessions, straight
     from intraday iv_history snapshots (collector sweeps every ~15 min).
     The minimum-IV bucket window = the midday lull to avoid.

IV.  Put-Call IV Skew (equidistant strikes)
     From skew_snapshots (per-strike CE/PE IV, ±7 strikes around ATM, written
     by iv-collector each pass). Tilt = mean(OTM put IV) − mean(OTM call IV)
     at strikes equidistant from ATM. Tracked across today's snapshots; a
     jump vs the day's own baseline is flagged. History accrues only from the
     first collector run with skew support — no backfill exists.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
IV_DB    = DATA_DIR / "iv_history.db"

# Display thresholds for the buyer rule (analytics/display only — the iv-rank
# scanner's own zone config drives alerts/composite, not these).
IVP_BUY_BELOW   = float(os.getenv("IVP_BUY_BELOW",   "20"))
IVP_AVOID_ABOVE = float(os.getenv("IVP_AVOID_ABOVE", "80"))

# Skew-tilt panic flag: latest tilt this many IV points above the day's mean.
TILT_PANIC_JUMP = float(os.getenv("TILT_PANIC_JUMP", "2.0"))


def _q(sql: str, params=()) -> list[dict]:
    if not IV_DB.exists():
        return []
    try:
        with sqlite3.connect(IV_DB) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        # missing table (fresh install / pre-upgrade DB) — fail open
        return []


# ── I. IV Percentile ─────────────────────────────────────────────────────── #
def iv_percentile(symbol: str) -> dict:
    """Latest scanner-computed IVP for a symbol (+ buyer-rule verdict).
    Falls back to computing from daily iv_history if the scanner hasn't run."""
    sym = symbol.upper()
    rows = _q("""
        SELECT iv_percentile, iv_rank, current_iv, zone, hist_days, timestamp
        FROM   iv_rank_history WHERE symbol = ?
        ORDER  BY timestamp DESC LIMIT 1
    """, (sym,))
    if rows and rows[0]["iv_percentile"] is not None:
        r = rows[0]
        ivp = r["iv_percentile"]
        src = "iv_rank_scanner"
        out = {"iv_percentile": ivp, "iv_rank": r["iv_rank"], "zone": r["zone"],
               "hist_days": r["hist_days"], "as_of": r["timestamp"]}
    else:
        # Fallback: direct computation from daily history (same formula as
        # iv_rank_scanner.iv_percentile — % of days with IV below current).
        hist = _q("""
            SELECT atm_iv FROM iv_history
            WHERE symbol = ? AND data_type = 'daily' AND atm_iv BETWEEN 1 AND 200
            ORDER BY timestamp DESC LIMIT 252
        """, (sym,))
        vals = [h["atm_iv"] for h in hist]
        if len(vals) < 5:
            return {"symbol": sym, "iv_percentile": None, "verdict": None,
                    "source": None, "note": f"only {len(vals)} daily samples"}
        current = vals[0]
        ivp = round(sum(1 for v in vals[1:] if v < current) / len(vals[1:]) * 100, 1)
        src = "fallback_direct"
        out = {"iv_percentile": ivp, "iv_rank": None, "zone": None,
               "hist_days": len(vals), "as_of": None}

    verdict = ("BUY" if ivp < IVP_BUY_BELOW
               else "AVOID" if ivp > IVP_AVOID_ABOVE else "NEUTRAL")
    out.update({"symbol": sym, "verdict": verdict, "source": src,
                "rule": f"BUY < {IVP_BUY_BELOW:.0f} · AVOID > {IVP_AVOID_ABOVE:.0f}"})
    return out


# ── II. Volatility Expansion (3-4 day IV slope) ──────────────────────────── #
def _expansion_rows(lookback_days: int = 4) -> list[dict]:
    """Full per-symbol IV-slope list (unenriched), sorted steepest-first.
    Shared by vol_expansion (leaderboard) and buy_zone_leaderboard."""
    rows = _q("""
        SELECT symbol, date(timestamp) AS d, atm_iv
        FROM   iv_history
        WHERE  data_type = 'daily' AND atm_iv BETWEEN 1 AND 200
          AND  date(timestamp) >= date('now', ?)
        ORDER  BY symbol, d ASC
    """, (f"-{lookback_days + 3} days",))

    by_sym: dict[str, list[float]] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r["atm_iv"])

    out = []
    for sym, ivs in by_sym.items():
        ivs = ivs[-lookback_days:]
        n = len(ivs)
        if n < 3:
            continue
        xm, ym = (n - 1) / 2, sum(ivs) / n
        denom = sum((i - xm) ** 2 for i in range(n))
        slope = sum((i - xm) * (ivs[i] - ym) for i in range(n)) / denom if denom else 0.0
        chg_pct = (ivs[-1] - ivs[0]) / ivs[0] * 100 if ivs[0] else 0.0
        out.append({
            "symbol": sym,
            "slope_iv_pts_per_day": round(slope, 2),
            "iv_start": round(ivs[0], 1),
            "iv_now": round(ivs[-1], 1),
            "change_pct": round(chg_pct, 1),
            "n_days": n,
            "expanding": slope > 0.5,
        })
    out.sort(key=lambda x: x["slope_iv_pts_per_day"], reverse=True)
    return out


def vol_expansion(lookback_days: int = 4, top_n: int = 15) -> dict:
    """Per-symbol slope of the last `lookback_days` daily ATM IV points.
    Steep positive slope = premium expansion under way (the pre-event
    signature) — detected from IV data; NO event calendar is collected."""
    out = _expansion_rows(lookback_days)
    # Buy-zone enrichment (top rows only — one IVP lookup per displayed
    # symbol). The tradeable pattern is the COMBINATION: IV climbing (slope)
    # while STILL cheap on 52-week history (IVP in the buy zone) → long
    # premium can win on Vega before the event even resolves. EXPANDING but
    # already-rich names are chase entries — vol crush eats the edge.
    for row in out[:top_n]:
        try:
            ivp = iv_percentile(row["symbol"])
            row["iv_percentile"] = ivp.get("iv_percentile")
            row["iv_rank"] = ivp.get("iv_rank")
            row["buy_zone"] = ivp.get("verdict")     # BUY / NEUTRAL / AVOID
        except Exception:
            row["iv_percentile"] = row["iv_rank"] = row["buy_zone"] = None
    return {
        "lookback_days": lookback_days,
        "note": ("Slope-detected expansion. No economic-calendar collector "
                 "exists — cross-check known event dates (RBI, budget, "
                 "earnings) manually before riding Vega."),
        "expanding_count": sum(1 for x in out if x["expanding"]),
        "symbols": out[:top_n],
    }


# ── III. Intraday IV Decay Curve (15-min buckets) ────────────────────────── #
def buy_zone_leaderboard(lookback_days: int = 4, scan_n: int = 60,
                         limit: int = 25, min_slope: float = 0.5) -> dict:
    """The prime long-premium buyer setup: IV *climbing* (positive slope) while
    *still cheap* on 52-wk history (IVP in the buy zone). Unlike vol_expansion,
    which enriches only the top-`top_n` by slope, this scans the top `scan_n`
    expanding names for IVP so a modestly-climbing-but-cheap name is not missed,
    then keeps only BUY-verdict rows and ranks them by a climb x cheapness blend.

    buy_score = slope * (1 - IVP/100): rewards a steeper climb AND a lower
    percentile, so a cheap name that is climbing outranks a rich name climbing
    faster (the latter is a vol-crush chase, not a buy)."""
    rows = [r for r in _expansion_rows(lookback_days)
            if r["slope_iv_pts_per_day"] >= min_slope][:scan_n]

    picks = []
    for row in rows:
        try:
            ivp = iv_percentile(row["symbol"])
        except Exception:
            continue
        ivp_val = ivp.get("iv_percentile")
        if ivp.get("verdict") != "BUY" or ivp_val is None:
            continue
        buy_score = row["slope_iv_pts_per_day"] * (1.0 - ivp_val / 100.0)
        picks.append({
            **row,
            "iv_percentile": ivp_val,
            "iv_rank": ivp.get("iv_rank"),
            "buy_zone": "BUY",
            "buy_score": round(buy_score, 2),
        })

    picks.sort(key=lambda x: x["buy_score"], reverse=True)
    return {
        "lookback_days": lookback_days,
        "rule": f"EXPANDING (slope >= {min_slope}/d) AND IVP < {IVP_BUY_BELOW:.0f} "
                f"(cheap on 52-wk history). Ranked by slope x (1 - IVP/100).",
        "note": ("Long premium here can win on Vega before the move even "
                 "resolves. Still cross-check event dates (RBI, budget, "
                 "earnings) manually - no economic-calendar collector exists."),
        "scanned": len(rows),
        "count": len(picks),
        "symbols": picks[:limit],
    }


def intraday_decay_curve(symbol: str | None = None, days: int = 10) -> dict:
    """Average ATM IV per 15-minute bucket over the last `days` sessions.
    symbol=None → cross-sectional average over the whole tracked universe.
    The lowest-IV midday window is surfaced as the lull to avoid."""
    where_sym = "AND symbol = ?" if symbol else ""
    params: tuple = ((symbol.upper(),) if symbol else ()) + (f"-{days} days",)
    rows = _q(f"""
        SELECT printf('%s:%02d',
                      strftime('%H', timestamp),
                      (CAST(strftime('%M', timestamp) AS INTEGER) / 15) * 15
               ) AS bucket,
               AVG(atm_iv)  AS avg_iv,
               COUNT(*)     AS n
        FROM   iv_history
        WHERE  data_type = 'intraday' AND atm_iv BETWEEN 1 AND 200
               {where_sym}
          AND  date(timestamp) >= date('now', ?)
          AND  time(timestamp) BETWEEN '09:15' AND '15:30'
        GROUP  BY bucket ORDER BY bucket ASC
    """, params)

    if not rows:
        return {"symbol": symbol.upper() if symbol else None, "days": days,
                "buckets": [], "lull": None}

    curve = [{"time": r["bucket"], "avg_iv": round(r["avg_iv"], 2), "n": r["n"]}
             for r in rows]
    # Midday lull: minimum average IV between 11:00 and 14:00
    mid = [c for c in curve if "11:00" <= c["time"] <= "14:00"]
    lull = min(mid, key=lambda c: c["avg_iv"]) if mid else None
    return {
        "symbol": symbol.upper() if symbol else None,
        "days": days,
        "buckets": curve,
        "lull": lull,
        "note": ("Avoid fresh long-premium entries around the lull bucket — "
                 "theta burns while IV is also at its intraday low."),
    }


# ── IV. Put-Call IV Skew tilt (equidistant strikes) ──────────────────────── #
def _tilt_from_strikes(strikes: list[dict], atm_strike: float,
                       wing: int = 3) -> float | None:
    """mean(put IV at ATM−1..−wing) − mean(call IV at ATM+1..+wing)."""
    if not strikes or atm_strike is None:
        return None
    below = sorted((s for s in strikes if s["strike"] < atm_strike),
                   key=lambda s: s["strike"], reverse=True)[:wing]
    above = sorted((s for s in strikes if s["strike"] > atm_strike),
                   key=lambda s: s["strike"])[:wing]
    puts  = [s["pe_iv"] for s in below if s.get("pe_iv")]
    calls = [s["ce_iv"] for s in above if s.get("ce_iv")]
    if not puts or not calls:
        return None
    return sum(puts) / len(puts) - sum(calls) / len(calls)


def skew_tilt(symbol: str, wing: int = 3) -> dict:
    """Today's put−call IV tilt series for a symbol from skew_snapshots.
    Positive tilt = puts pricier than equidistant calls (downside fear).
    A jump of TILT_PANIC_JUMP IV points above the day's mean = panic flag."""
    sym = symbol.upper()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _q("""
        SELECT timestamp, atm_strike, spot_price, strikes_json
        FROM   skew_snapshots
        WHERE  symbol = ? AND date(timestamp) = ?
        ORDER  BY timestamp ASC
    """, (sym, today))

    series = []
    for r in rows:
        try:
            strikes = json.loads(r["strikes_json"]) or []
        except Exception:
            continue
        t = _tilt_from_strikes(strikes, r["atm_strike"], wing)
        if t is not None:
            series.append({"time": r["timestamp"][11:16], "tilt": round(t, 2),
                           "spot": r["spot_price"]})

    if not series:
        return {"symbol": sym, "date": today, "series": [], "panic": None,
                "note": ("No skew snapshots yet — table fills from the next "
                         "iv-collector pass onward (no backfill exists).")}

    tilts = [p["tilt"] for p in series]
    mean_tilt = sum(tilts) / len(tilts)
    latest = series[-1]
    panic = latest["tilt"] - mean_tilt >= TILT_PANIC_JUMP and latest["tilt"] > 0
    return {
        "symbol": sym,
        "date": today,
        "wing_strikes": wing,
        "series": series,
        "latest_tilt": latest["tilt"],
        "day_mean_tilt": round(mean_tilt, 2),
        "panic": panic,
        "panic_rule": f"latest − day mean ≥ {TILT_PANIC_JUMP} IV pts (and tilt > 0)",
    }
