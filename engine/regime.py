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


def load(db_path: str, index_slope_pct: float | None = None,
         event_blackout: bool = False) -> RegimeState:
    """Read current inputs and classify. Missing inputs degrade to AMBER, never crash."""
    return classify(_latest_vix(db_path), _market_breadth(db_path),
                    index_slope_pct, event_blackout)
