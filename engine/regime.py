# -*- coding: utf-8 -*-
"""Regime engine — market posture before anything else (funnel step 1).

Pure classifier + a fail-open loader that reads VIX from vix_daily and market
breadth from breadth.compute(). Index trend (SuperSmoother slope on NIFTY) is
optional in P0 and passed in when available.
"""

from __future__ import annotations

import logging
import sqlite3

from engine import config as cfg
from engine.contracts import RegimeState, GREEN, AMBER, RED, CE, PE

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Pure classification (unit-tested)
# --------------------------------------------------------------------------- #
def classify(vix: float | None, breadth_pct: float | None,
             index_slope_pct: float | None = None,
             event_blackout: bool = False) -> RegimeState:
    """Map (VIX, breadth, index slope, calendar) -> RegimeState.

    RED   : engine observes and journals but emits nothing.
    AMBER : half size, only A+/A grades emitted.
    GREEN : full size.
    """
    reasons = []

    # Hard red conditions
    if event_blackout:
        return RegimeState(RED, None, vix, breadth_pct, index_slope_pct,
                           cfg.SIZE_MULT[RED], ["event_blackout"])
    if vix is not None and vix >= cfg.VIX_RED:
        return RegimeState(RED, None, vix, breadth_pct, index_slope_pct,
                           cfg.SIZE_MULT[RED], [f"vix {vix:.1f} >= {cfg.VIX_RED}"])

    # Directional lean — breadth and slope must not contradict
    lean = None
    if breadth_pct is not None:
        if breadth_pct >= cfg.BREADTH_BULL:
            lean = CE
        elif breadth_pct <= cfg.BREADTH_BEAR:
            lean = PE
    if index_slope_pct is not None and abs(index_slope_pct) >= cfg.INDEX_SLOPE_MIN:
        slope_lean = CE if index_slope_pct > 0 else PE
        if lean is None:
            lean = slope_lean
        elif lean != slope_lean:
            lean = None
            reasons.append("breadth/index disagree")

    # Posture
    elevated_vix = vix is not None and vix >= cfg.VIX_ELEVATED
    if elevated_vix:
        reasons.append(f"vix elevated {vix:.1f}")
    no_direction = lean is None
    if no_direction:
        reasons.append("no directional lean")

    posture = AMBER if (elevated_vix or no_direction) else GREEN
    if posture == GREEN:
        reasons.append(f"lean {lean}, breadth {breadth_pct}, vix {vix}")

    return RegimeState(posture, lean, vix, breadth_pct, index_slope_pct,
                       cfg.SIZE_MULT[posture], reasons)


# --------------------------------------------------------------------------- #
# Fail-open loaders (no broker calls — DB only)
# --------------------------------------------------------------------------- #
def _latest_vix(db_path: str) -> float | None:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT close FROM vix_daily ORDER BY date DESC LIMIT 1"
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except sqlite3.OperationalError:
        return None


def _market_breadth(db_path: str) -> float | None:
    try:
        import breadth
        snap = breadth.compute(db_path=db_path)
        return snap.market_pct
    except Exception:  # noqa: BLE001 — fail-open, breadth is optional context
        logger.debug("regime: breadth unavailable", exc_info=True)
        return None


# --- index slope (self-contained SuperSmoother, no sonar/pandas dependency) -- #
def _super_smoother(series: list[float], period: int) -> list[float]:
    """Ehlers 2-pole SuperSmoother low-pass filter — same math as the sonar
    scanner, inlined so the engine stays dependency-free (README: P2 pulls
    computations in-engine)."""
    import math
    n = len(series)
    if n < 3 or period < 2:
        return list(series)
    arg = 1.414 * math.pi / period
    a1 = math.exp(-arg)
    c2 = 2 * a1 * math.cos(arg)
    c3 = -a1 * a1
    c1 = 1 - c2 - c3
    ss = list(series)  # seed first two with raw prices
    for i in range(2, n):
        ss[i] = c1 * (series[i] + series[i - 1]) / 2.0 + c2 * ss[i - 1] + c3 * ss[i - 2]
    return ss


def _index_slope(db_path: str) -> float | None:
    """NIFTY SuperSmoother slope over the latest session, signed % of price.

    Fail-open: any missing data / error returns None (regime then falls back to
    breadth-only lean, exactly as before this fix)."""
    try:
        with sqlite3.connect(db_path) as conn:
            day = conn.execute(
                "SELECT MAX(substr(ts,1,10)) FROM candles_5m WHERE security_id=?",
                (cfg.INDEX_SECURITY_ID,),
            ).fetchone()
            if not day or not day[0]:
                return None
            rows = conn.execute(
                "SELECT close FROM candles_5m WHERE security_id=? "
                "AND substr(ts,1,10)=? ORDER BY ts",
                (cfg.INDEX_SECURITY_ID, day[0]),
            ).fetchall()
    except sqlite3.OperationalError:
        return None
    closes = [float(r[0]) for r in rows if r[0] is not None]
    lb = cfg.INDEX_SLOPE_LOOKBACK
    if len(closes) < max(cfg.INDEX_SLOPE_MIN_BARS, lb + 1) or not closes[-1]:
        return None
    ss = _super_smoother(closes, cfg.INDEX_SLOPE_PERIOD)
    return (ss[-1] - ss[-1 - lb]) / closes[-1] * 100.0


def load(db_path: str, index_slope_pct: float | None = None,
         event_blackout: bool = False) -> RegimeState:
    """Read current inputs and classify. Missing inputs degrade to AMBER, never crash.

    When ``index_slope_pct`` is not supplied it is computed from NIFTY candles
    (E2-1); pass an explicit value only to override (e.g. tests)."""
    if index_slope_pct is None:
        index_slope_pct = _index_slope(db_path)
    return classify(_latest_vix(db_path), _market_breadth(db_path),
                    index_slope_pct, event_blackout)
