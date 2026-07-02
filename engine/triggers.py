# -*- coding: utf-8 -*-
"""Trigger detectors — ORB, VWAP reclaim/break, PDH/PDL break-retest (V2 P2).

Pure price-action math over persisted 5-min candles (engine/candles.py).
Doctrine enforced here, not suggested:
  * completed candles only (aggregation drops partial groups)
  * BODY close beyond the level — wick touches never count
  * volume confirmation on every trigger
  * ORB entries valid 09:30–11:30 only; breakout window for B&B till 11:45

Detection order = edge order: BREAK_RETEST (level + confirmation, the cleanest
entry) > ORB > VWAP. The first hit wins; sonar bands remain the fallback.
"""

from __future__ import annotations

import logging
from datetime import datetime

from engine import candles as cd
from engine.contracts import TriggerEvent, CE, PE

logger = logging.getLogger(__name__)

# knobs (doctrine defaults; env-tunable later if journal says so)
ORB_RANGE_CANDLES_15M = 1          # 09:15–09:30 opening range
ORB_VOL_MULT = 1.5
ORB_ENTRY_START = "09:30"
ORB_ENTRY_END = "11:30"
VWAP_VOL_MULT = 1.3
BB_BREAKOUT_END = "11:45"
BB_RETEST_TOL_PCT = 0.3            # 5m low/high within 0.3% of the level
BB_WICK_RATIO = 1.5                # hammer: lower wick >= 1.5 x body
MIN_5M_CANDLES = 9                 # need ≥ 45 min of session


def _hhmm(ts: str) -> str:
    return str(ts)[11:16]


def _clip01(x):
    return max(0.0, min(1.0, x))


def vol_ratio(candles: list[dict], i: int | None = None, lookback: int = 5) -> float:
    """Volume of candle i vs mean of the `lookback` candles before it."""
    i = len(candles) - 1 if i is None else i
    prev = [c.get("volume") or 0.0 for c in candles[max(0, i - lookback):i]]
    if not prev or sum(prev) == 0:
        return 0.0
    return (candles[i].get("volume") or 0.0) / (sum(prev) / len(prev))


# --------------------------------------------------------------------------- #
# ORB — 15m body close beyond the opening range + volume (pure)
# --------------------------------------------------------------------------- #
def detect_orb(c15: list[dict]) -> TriggerEvent | None:
    if len(c15) <= ORB_RANGE_CANDLES_15M:
        return None
    last = c15[-1]
    t = _hhmm(last["ts"])
    if not (ORB_ENTRY_START <= t <= ORB_ENTRY_END):
        return None
    orb_high = max(c["high"] for c in c15[:ORB_RANGE_CANDLES_15M])
    orb_low = min(c["low"] for c in c15[:ORB_RANGE_CANDLES_15M])
    vr = vol_ratio(c15)
    if vr < ORB_VOL_MULT:
        return None
    body_close = last["close"]
    if body_close > orb_high:
        direction = CE
    elif body_close < orb_low:
        direction = PE
    else:
        return None
    quality = _clip01(0.4 + (vr - ORB_VOL_MULT) * 0.2 +
                      abs(body_close - (orb_high if direction == CE else orb_low))
                      / body_close * 20)
    return TriggerEvent("ORB", direction, round(quality, 2),
                        {"orb_high": orb_high, "orb_low": orb_low,
                         "vol_ratio": round(vr, 2), "ts": last["ts"]})


# --------------------------------------------------------------------------- #
# VWAP reclaim / break — 15m cross with volume (pure)
# --------------------------------------------------------------------------- #
def detect_vwap(c15: list[dict], vwap: list[float]) -> TriggerEvent | None:
    if len(c15) < 2 or len(vwap) != len(c15):
        return None
    prev, last = c15[-2], c15[-1]
    v_prev, v_last = vwap[-2], vwap[-1]
    vr = vol_ratio(c15)
    if vr < VWAP_VOL_MULT:
        return None
    if prev["close"] < v_prev and last["close"] > v_last:
        direction, kind = CE, "vwap_reclaim"
    elif prev["close"] > v_prev and last["close"] < v_last:
        direction, kind = PE, "vwap_break"
    else:
        return None
    quality = _clip01(0.35 + (vr - VWAP_VOL_MULT) * 0.2)
    return TriggerEvent("VWAP", direction, round(quality, 2),
                        {"kind": kind, "vwap": round(v_last, 2),
                         "vol_ratio": round(vr, 2), "ts": last["ts"]})


# --------------------------------------------------------------------------- #
# Break & retest — yesterday H/L break on 15m, retest entry on 5m (pure)
# --------------------------------------------------------------------------- #
def _is_hammer(c: dict, bullish: bool) -> bool:
    body = abs(c["close"] - c["open"]) or 1e-9
    lower = min(c["open"], c["close"]) - c["low"]
    upper = c["high"] - max(c["open"], c["close"])
    if bullish:
        return lower >= BB_WICK_RATIO * body and upper <= body
    return upper >= BB_WICK_RATIO * body and lower <= body


def _engulfs(prev: dict, curr: dict, bullish: bool) -> bool:
    if bullish:
        return (curr["low"] < prev["low"] and curr["high"] > prev["high"]
                and curr["close"] > curr["open"])
    return (curr["high"] > prev["high"] and curr["low"] < prev["low"]
            and curr["close"] < curr["open"])


def detect_break_retest(c5: list[dict], c15: list[dict],
                        prev_high: float | None, prev_low: float | None) -> TriggerEvent | None:
    if prev_high is None or prev_low is None or len(c5) < 3 or not c15:
        return None
    # 1) breakout: any COMPLETED 15m candle closing beyond yesterday's level
    #    inside the breakout window.
    level = direction = None
    for c in c15:
        if _hhmm(c["ts"]) > BB_BREAKOUT_END:
            break
        if c["close"] > prev_high:
            level, direction = prev_high, CE
            break
        if c["close"] < prev_low:
            level, direction = prev_low, PE
            break
    if direction is None:
        return None
    # 2) retest on the latest completed 5m candle: tag the level, confirm with
    #    hammer or engulfing in the trigger direction.
    prev5, last5 = c5[-2], c5[-1]
    tol = level * BB_RETEST_TOL_PCT / 100.0
    bullish = direction == CE
    tagged = (abs(last5["low"] - level) <= tol) if bullish else (abs(last5["high"] - level) <= tol)
    if not tagged:
        return None
    hammer = _is_hammer(last5, bullish)
    engulf = _engulfs(prev5, last5, bullish)
    if not (hammer or engulf):
        return None
    closes_right_way = last5["close"] > last5["open"] if bullish else last5["close"] < last5["open"]
    quality = _clip01(0.55 + (0.2 if hammer else 0.15) + (0.1 if closes_right_way else 0.0)
                      + min(0.15, vol_ratio(c5) * 0.05))
    return TriggerEvent("BREAK_RETEST", direction, round(quality, 2),
                        {"level": level, "pattern": "hammer" if hammer else "engulfing",
                         "ts": last5["ts"]})


# --------------------------------------------------------------------------- #
# Orchestrator — used by the pipeline as trigger_fn (DB in, TriggerEvent out)
# --------------------------------------------------------------------------- #
def detect(db_path: str, security_id: str, symbol: str,
           day: str | None = None) -> TriggerEvent | None:
    """Best candle-based trigger for one symbol, else sonar-band fallback."""
    c5 = cd.load_today(db_path, security_id, day)
    if len(c5) >= MIN_5M_CANDLES:
        c15 = cd.aggregate(c5, 3)
        ph, pl = cd.yesterday_levels(db_path, symbol, day)
        for t in (detect_break_retest(c5, c15, ph, pl),
                  detect_orb(c15),
                  detect_vwap(c15, cd.session_vwap(c15))):
            if t is not None:
                return t
    # Fallback: sonar band breakout already persisted by the sonar service.
    from engine.factors import load_trigger as sonar_trigger
    return sonar_trigger(security_id)


def make_trigger_fn(db_path: str, symbol_map: dict[str, str] | None = None):
    """Adapter for EnginePipeline(trigger_fn=...): sid -> TriggerEvent | None."""
    symbol_map = symbol_map or {}

    def _fn(security_id: str):
        return detect(db_path, str(security_id), symbol_map.get(str(security_id), ""))
    return _fn
