# -*- coding: utf-8 -*-
"""
market_context/regime/axes.py — the six independent axis classifiers.

Each classifier is a PURE function of the feature vector plus the axis's own
currently-published state (needed for asymmetric hysteresis bands), returning:

    AxisResult(state, score, parts, agreement, margin, reasons, event)

`parts` is the normalised sub-feature dict, kept for audit and persisted into
mc_regime.axis_inputs. `agreement` is the fraction of an axis's own inputs
pointing the same way as the classification — the evidence term in confidence.
`margin` is how far the deciding quantity sits from its threshold, so a score
of 0.301 against a 0.300 threshold is correctly reported as a coin flip.

ASYMMETRIC BANDS
----------------
Entry and exit thresholds differ on purpose. Volatility enters HIGH_VOL at the
75th percentile but does not leave until below the 60th; trend enters
"trending" at ER 0.35 but does not fall back to RANGE until below 0.20. Equal
thresholds guarantee chatter at the boundary, which is the single most common
way a regime classifier becomes useless.

NOTHING HERE DECIDES A TRADE. There is no bias, no size, no veto, no exit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from market_context import config as cfg
from market_context.contracts import (
    BREAKDOWN, BREAKOUT, EVENT_NONE, HIGH_PARTICIPATION, HIGH_VOL, ILLIQUID,
    LIQUID, LOW_PARTICIPATION, LOW_VOL, NEGATIVE, NORMAL_LIQUIDITY,
    NORMAL_PARTICIPATION, NORMAL_VOL, PANIC, POSITIONING_NEUTRAL, POSITIVE,
    RANGE, REVERSAL, STRONG_NEGATIVE, STRONG_POSITIVE, THIN, TRENDING_DOWN,
    TRENDING_UP, UNKNOWN,
)
from market_context.contracts import BREADTH_NEUTRAL
from market_context.features import estimators as est


@dataclass
class AxisResult:
    state: str = UNKNOWN
    score: float | None = None
    parts: dict = field(default_factory=dict)
    agreement: float | None = None
    margin: float | None = None
    reasons: list = field(default_factory=list)
    event: str = EVENT_NONE


def _agreement(parts: dict, positive: bool) -> float | None:
    """Fraction of present sub-features agreeing with the classified sign."""
    values = [v for v in parts.values() if v is not None]
    if not values:
        return None
    agreeing = sum(1 for v in values if (v > 0) == positive or v == 0)
    return agreeing / len(values)


def _margin(value: float | None, threshold: float, scale: float) -> float | None:
    if value is None or scale <= 0:
        return None
    return est.clip(abs(value - threshold) / scale)


# --------------------------------------------------------------------------- #
# Trend
# --------------------------------------------------------------------------- #
def classify_trend(fv, current: str = UNKNOWN) -> AxisResult:
    parts = {
        "ef_ratio": est.normalise(fv.ef_ratio, 0.0, 1.0),
        "ss_slope": est.signed_normalise(fv.ss_slope_pct, 0.5),
        "mom_z": est.signed_normalise(fv.mom_z, 2.0),
        "vwap_position": fv.vwap_position,
    }
    # Efficiency ratio is unsigned — it says HOW cleanly price travelled, not
    # which way. Give it the sign of the directional inputs so it reinforces
    # rather than fights them.
    directional = [v for k, v in parts.items()
                   if k != "ef_ratio" and v is not None]
    lean = sum(directional) / len(directional) if directional else None
    if parts["ef_ratio"] is not None and lean is not None:
        parts["ef_ratio"] = parts["ef_ratio"] * (1.0 if lean >= 0 else -1.0)

    score, coverage = est.weighted_score(parts, cfg.TREND_WEIGHTS)
    result = AxisResult(parts=parts, score=score)
    if score is None or coverage <= 0:
        result.reasons.append("no trend inputs")
        return result

    # Asymmetric range band: leaving a trend needs a lower ER than entering one.
    was_trending = current in (TRENDING_UP, TRENDING_DOWN)
    er_floor = cfg.TREND_ER_RANGE_MAX if was_trending else cfg.TREND_ER_TRENDING_MIN
    efficient = fv.ef_ratio is None or fv.ef_ratio >= er_floor

    if not efficient:
        result.state = RANGE
        result.reasons.append(
            f"ER {fv.ef_ratio:.2f} < {er_floor:.2f} — path inefficient")
        result.margin = _margin(fv.ef_ratio, er_floor, 0.35)
    elif score >= cfg.TREND_SCORE_UP_MIN:
        result.state = TRENDING_UP
        result.reasons.append(f"score {score:+.2f} >= {cfg.TREND_SCORE_UP_MIN:+.2f}")
        result.margin = _margin(score, cfg.TREND_SCORE_UP_MIN, 0.7)
    elif score <= cfg.TREND_SCORE_DOWN_MAX:
        result.state = TRENDING_DOWN
        result.reasons.append(f"score {score:+.2f} <= {cfg.TREND_SCORE_DOWN_MAX:+.2f}")
        result.margin = _margin(score, cfg.TREND_SCORE_DOWN_MAX, 0.7)
    else:
        result.state = RANGE
        result.reasons.append(f"score {score:+.2f} inside trend band")
        result.margin = _margin(score, 0.0, cfg.TREND_SCORE_UP_MIN)

    result.agreement = _agreement(parts, positive=(score >= 0))
    result.event = _trend_event(fv, result.state)
    if result.event != EVENT_NONE:
        result.reasons.append(f"event {result.event}")
    return result


def _trend_event(fv, state: str) -> str:
    """Structural event layered on the trend axis.

    REVERSAL demands THREE independent confirmations (breadth divergence, a
    trend-score sign opposing the day's location, and an extreme range
    position). Reversal calls have the worst false-positive rate of any state,
    so they get the strictest evidence bar — see STRUCT_REVERSAL_MIN_CONF.
    """
    confirmations = 0
    if fv.breadth_divergence:
        confirmations += 1
    if fv.range_position is not None and (fv.range_position > 0.9
                                          or fv.range_position < 0.1):
        confirmations += 1
    if fv.thrust is not None and abs(fv.thrust) >= 10:
        confirmations += 1
    if confirmations >= cfg.STRUCT_REVERSAL_MIN_CONFIRMATIONS:
        return REVERSAL

    if fv.orb_state == "ABOVE" and fv.prior_day_state in ("ABOVE", None) \
            and state == TRENDING_UP:
        return BREAKOUT
    if fv.orb_state == "BELOW" and fv.prior_day_state in ("BELOW", None) \
            and state == TRENDING_DOWN:
        return BREAKDOWN
    return EVENT_NONE


# --------------------------------------------------------------------------- #
# Volatility
# --------------------------------------------------------------------------- #
def classify_volatility(fv, current: str = UNKNOWN) -> AxisResult:
    parts = {
        "vix_percentile": est.normalise(fv.vix_percentile, 0.0, 100.0),
        "rv_ratio": est.normalise(fv.rv_ratio, 0.5, 2.0),
        "rv_level": est.normalise(fv.rv_yz_short, 5.0, 45.0),
        "vol_of_vol": est.normalise(fv.vol_of_vol, 0.0, 10.0),
    }
    score, coverage = est.weighted_score(parts, cfg.VOL_WEIGHTS)
    result = AxisResult(parts=parts, score=score)
    if score is None or coverage <= 0:
        result.reasons.append("no volatility inputs")
        return result

    pctile = fv.vix_percentile
    was_high = current in (HIGH_VOL, PANIC)
    was_low = current == LOW_VOL
    high_bar = cfg.VOL_HIGH_EXIT_PCTILE if was_high else cfg.VOL_HIGH_VIX_PCTILE
    low_bar = cfg.VOL_LOW_EXIT_PCTILE if was_low else cfg.VOL_LOW_VIX_PCTILE

    panic = (pctile is not None and pctile >= cfg.VOL_PANIC_VIX_PCTILE) or \
            (fv.rv_ratio is not None and fv.rv_ratio >= cfg.VOL_PANIC_RV_RATIO)
    if panic:
        result.state = PANIC
        result.reasons.append(
            f"VIX pctile {pctile if pctile is None else round(pctile)} / "
            f"RV ratio {fv.rv_ratio if fv.rv_ratio is None else round(fv.rv_ratio, 2)}")
        result.margin = _margin(pctile, cfg.VOL_PANIC_VIX_PCTILE, 20.0)
    elif pctile is not None and pctile >= high_bar:
        result.state = HIGH_VOL
        result.reasons.append(f"VIX pctile {pctile:.0f} >= {high_bar:.0f}")
        result.margin = _margin(pctile, high_bar, 25.0)
    elif pctile is not None and pctile <= low_bar:
        result.state = LOW_VOL
        result.reasons.append(f"VIX pctile {pctile:.0f} <= {low_bar:.0f}")
        result.margin = _margin(pctile, low_bar, 25.0)
    elif pctile is not None:
        result.state = NORMAL_VOL
        result.reasons.append(f"VIX pctile {pctile:.0f} mid-range")
        result.margin = _margin(pctile, 50.0, 25.0)
    else:
        # No VIX baseline yet (needs vix_daily history) — fall back to the
        # expansion/compression ratio alone rather than claiming nothing.
        if fv.rv_ratio is None:
            result.reasons.append("no VIX percentile and no RV ratio")
            return result
        result.state = (HIGH_VOL if fv.rv_ratio >= cfg.VOL_EXPANSION_RV_RATIO
                        else NORMAL_VOL)
        result.reasons.append(f"RV ratio {fv.rv_ratio:.2f} (no VIX baseline)")
        result.margin = _margin(fv.rv_ratio, cfg.VOL_EXPANSION_RV_RATIO, 0.5)

    result.agreement = _agreement(
        {k: (v - 0.5 if v is not None else None) for k, v in parts.items()},
        positive=result.state in (HIGH_VOL, PANIC))
    return result


# --------------------------------------------------------------------------- #
# Liquidity
# --------------------------------------------------------------------------- #
def classify_liquidity(fv, current: str = UNKNOWN) -> AxisResult:
    imbalance = None
    if fv.depth_imbalance is not None and fv.depth_imbalance > 0:
        # Distance from balanced (1.0), capped by the configured max.
        skew = max(fv.depth_imbalance, 1.0 / fv.depth_imbalance)
        imbalance = est.normalise(skew, 1.0, max(cfg.LIQ_DEPTH_IMBALANCE_MAX, 1.1))
    parts = {
        "spread_pctile": est.normalise(fv.spread_pctile, 0.0, 100.0),
        "depth_total": None,          # absolute depth is not comparable; see below
        "depth_imbalance": imbalance,
    }
    score, coverage = est.weighted_score(parts, cfg.LIQ_WEIGHTS)
    result = AxisResult(parts=parts, score=score)
    if fv.spread_pctile is None:
        result.reasons.append("no spread history yet")
        return result

    pctile = fv.spread_pctile
    was_thin = current in (THIN, ILLIQUID)
    thin_bar = (cfg.LIQ_THIN_SPREAD_PCTILE - 10.0) if was_thin else cfg.LIQ_THIN_SPREAD_PCTILE

    if pctile >= cfg.LIQ_ILLIQUID_SPREAD_PCTILE:
        result.state = ILLIQUID
        result.margin = _margin(pctile, cfg.LIQ_ILLIQUID_SPREAD_PCTILE, 15.0)
    elif pctile >= thin_bar:
        result.state = THIN
        result.margin = _margin(pctile, thin_bar, 20.0)
    elif pctile <= cfg.LIQ_LIQUID_SPREAD_PCTILE:
        result.state = LIQUID
        result.margin = _margin(pctile, cfg.LIQ_LIQUID_SPREAD_PCTILE, 20.0)
    else:
        result.state = NORMAL_LIQUIDITY
        result.margin = _margin(pctile, 50.0, 25.0)
    result.reasons.append(f"spread at {pctile:.0f}th pctile of its own session")
    result.agreement = _agreement(
        {k: (v - 0.5 if v is not None else None) for k, v in parts.items()},
        positive=result.state in (THIN, ILLIQUID))
    return result


# --------------------------------------------------------------------------- #
# Participation
# --------------------------------------------------------------------------- #
def classify_participation(fv, current: str = UNKNOWN) -> AxisResult:
    parts = {
        "volume_ratio": est.normalise(fv.volume_ratio, 0.0, 2.0),
        "trade_count_ratio": est.normalise(fv.trade_count_ratio, 0.0, 2.0),
        "active_names_pct": est.normalise(fv.active_names_pct, 0.0, 100.0),
    }
    score, coverage = est.weighted_score(parts, cfg.PART_WEIGHTS)
    result = AxisResult(parts=parts, score=score)
    if fv.volume_ratio is None and fv.trade_count_ratio is None:
        result.reasons.append("no participation baseline yet "
                              "(needs prior sessions for the time-of-day bucket)")
        return result

    ratio = fv.volume_ratio if fv.volume_ratio is not None else fv.trade_count_ratio
    if ratio >= cfg.PART_HIGH_RATIO:
        result.state = HIGH_PARTICIPATION
        result.margin = _margin(ratio, cfg.PART_HIGH_RATIO, 0.6)
    elif ratio <= cfg.PART_LOW_RATIO:
        result.state = LOW_PARTICIPATION
        result.margin = _margin(ratio, cfg.PART_LOW_RATIO, 0.4)
    else:
        result.state = NORMAL_PARTICIPATION
        result.margin = _margin(ratio, 1.0, 0.3)
    result.reasons.append(f"volume {ratio:.2f}x its own time-of-day median")
    result.agreement = _agreement(
        {k: (v - 0.5 if v is not None else None) for k, v in parts.items()},
        positive=result.state == HIGH_PARTICIPATION)
    return result


# --------------------------------------------------------------------------- #
# Positioning
# --------------------------------------------------------------------------- #
_QUADRANT_SCORE = {
    "LONG_BUILDUP": 1.0,
    "SHORT_COVERING": 0.4,      # buying, but exhausted rather than fresh
    POSITIONING_NEUTRAL: 0.0,
    "LONG_LIQUIDATION": -0.4,
    "SHORT_BUILDUP": -1.0,
}


def classify_positioning(fv, current: str = UNKNOWN) -> AxisResult:
    """Index positioning as an OI-weighted blend of NIFTY and BANKNIFTY.

    The quadrants are mechanical (measured price x measured OI), not a
    forecast, which is why this axis carries a high confidence weight relative
    to trend. The state reported is the dominant index's quadrant; the score
    carries the blend, so a disagreeing BANKNIFTY drags the magnitude down
    without corrupting the label.
    """
    weights = cfg.POS_INDEX_WEIGHTS
    entries = [
        ("NIFTY", fv.nifty_quadrant, weights.get("NIFTY", 0.5)),
        ("BANKNIFTY", fv.banknifty_quadrant, weights.get("BANKNIFTY", 0.5)),
    ]
    present = [(name, q, w) for name, q, w in entries
               if q and q != UNKNOWN]
    parts = {name.lower(): _QUADRANT_SCORE.get(q) for name, q, _w in entries if q}
    if fv.stock_fut_long_pct is not None:
        parts["stock_fut_long"] = (fv.stock_fut_long_pct - 50.0) / 50.0

    result = AxisResult(parts=parts)
    if not present:
        result.reasons.append("no futures quadrant available")
        return result

    total_weight = sum(w for _n, _q, w in present) or 1.0
    score = sum(_QUADRANT_SCORE.get(q, 0.0) * w for _n, q, w in present) / total_weight
    result.score = score

    dominant = max(present, key=lambda item: item[2])
    result.state = dominant[1]
    result.reasons.append(
        " / ".join(f"{name} {q}" for name, q, _w in present))
    result.margin = est.clip(abs(score))
    result.agreement = _agreement(parts, positive=(score >= 0))
    return result


# --------------------------------------------------------------------------- #
# Breadth
# --------------------------------------------------------------------------- #
def classify_breadth(fv, current: str = UNKNOWN) -> AxisResult:
    parts = {
        "adv_dec_pct": est.signed_normalise(
            (fv.adv_dec_pct - 50.0) if fv.adv_dec_pct is not None else None, 30.0),
        "volume_breadth_pct": est.signed_normalise(
            (fv.volume_breadth_pct - 50.0) if fv.volume_breadth_pct is not None
            else None, 30.0),
        "thrust": est.signed_normalise(fv.thrust, 20.0),
    }
    score, coverage = est.weighted_score(parts, cfg.BREADTH_WEIGHTS)
    result = AxisResult(parts=parts, score=score)
    if fv.adv_dec_pct is None:
        result.reasons.append("no breadth reading (too few moving names)")
        return result

    pct = fv.adv_dec_pct
    if pct >= cfg.BREADTH_STRONG_POSITIVE_PCT:
        result.state = STRONG_POSITIVE
        result.margin = _margin(pct, cfg.BREADTH_STRONG_POSITIVE_PCT, 20.0)
    elif pct >= cfg.BREADTH_POSITIVE_PCT:
        result.state = POSITIVE
        result.margin = _margin(pct, cfg.BREADTH_POSITIVE_PCT, 15.0)
    elif pct <= cfg.BREADTH_STRONG_NEGATIVE_PCT:
        result.state = STRONG_NEGATIVE
        result.margin = _margin(pct, cfg.BREADTH_STRONG_NEGATIVE_PCT, 20.0)
    elif pct <= cfg.BREADTH_NEGATIVE_PCT:
        result.state = NEGATIVE
        result.margin = _margin(pct, cfg.BREADTH_NEGATIVE_PCT, 15.0)
    else:
        result.state = BREADTH_NEUTRAL
        result.margin = _margin(pct, 50.0, 10.0)

    result.reasons.append(f"{pct:.0f}% advancing")
    if fv.breadth_is_subsample:
        result.reasons.append("partial-universe subsample — confidence capped")
    result.agreement = _agreement(parts, positive=(score or 0) >= 0)
    return result


CLASSIFIERS = {
    "trend": classify_trend,
    "volatility": classify_volatility,
    "liquidity": classify_liquidity,
    "participation": classify_participation,
    "positioning": classify_positioning,
    "breadth": classify_breadth,
}
