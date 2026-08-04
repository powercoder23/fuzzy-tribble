# -*- coding: utf-8 -*-
"""
market_context/features/estimators.py — pure statistical estimators.

No I/O, no config reads, no globals. Every function takes plain numbers and
returns plain numbers or None, so each formula can be checked against a
hand-computed value in a test.

`None` means "not computable from this input" and is never substituted with
0.0. A zero volatility or a zero efficiency ratio is a strong claim about the
market; "I don't know" is a different claim, and the distinction has to
survive all the way to the confidence score.
"""

from __future__ import annotations

import math

# --------------------------------------------------------------------------- #
# Trend
# --------------------------------------------------------------------------- #
def efficiency_ratio(closes) -> float | None:
    """Kaufman Efficiency Ratio: |net move| / sum(|bar moves|), in [0, 1].

    Measures DIRECTIONAL EFFICIENCY — how much net travel was achieved per
    unit of path walked. ER -> 1 is a clean trend; ER -> 0 is chop that
    covered the same gross distance and went nowhere.

    Chosen over an EMA stack because an EMA tells you where price *was*, while
    ER tells you whether the path was *tradeable*. It is also scale-free, so
    one threshold works for NIFTY and BANKNIFTY alike.
    """
    values = [float(c) for c in (closes or []) if c is not None]
    if len(values) < 3:
        return None
    path = sum(abs(values[i] - values[i - 1]) for i in range(1, len(values)))
    if path <= 0:
        return None
    return abs(values[-1] - values[0]) / path


def super_smoother(series, period: int) -> list[float]:
    """Ehlers 2-pole SuperSmoother low-pass filter.

    Identical math to engine/regime.py's inlined `_super_smoother`. It lives
    here so the Convex engine can eventually import it from one place instead
    of keeping its own copy (MARKET_CONTEXT_PLAN.md C1).
    """
    values = [float(v) for v in (series or [])]
    n = len(values)
    if n < 3 or period < 2:
        return values
    arg = 1.414 * math.pi / period
    a1 = math.exp(-arg)
    c2 = 2 * a1 * math.cos(arg)
    c3 = -a1 * a1
    c1 = 1 - c2 - c3
    out = list(values)                      # seed the first two with raw prices
    for i in range(2, n):
        out[i] = c1 * (values[i] + values[i - 1]) / 2.0 + c2 * out[i - 1] + c3 * out[i - 2]
    return out


def slope_pct(series, lookback: int, period: int) -> float | None:
    """SuperSmoother slope over `lookback` bars, as a signed % of last price."""
    values = [float(v) for v in (series or []) if v is not None]
    if len(values) < max(lookback + 1, 3) or not values[-1]:
        return None
    smoothed = super_smoother(values, period)
    return (smoothed[-1] - smoothed[-1 - lookback]) / values[-1] * 100.0


def vol_scaled_momentum(closes, rv_per_bar: float | None) -> float | None:
    """Return over the window divided by its expected volatility — the CTA
    convention. A 1% move means very different things at 8% and 25% vol, so
    the raw return is not comparable across regimes; this is.

    `rv_per_bar` is the PER-BAR standard deviation (not annualised).
    """
    values = [float(c) for c in (closes or []) if c is not None]
    if len(values) < 2 or not rv_per_bar or rv_per_bar <= 0 or not values[0]:
        return None
    n = len(values) - 1
    log_return = math.log(values[-1] / values[0]) if values[0] > 0 else None
    if log_return is None:
        return None
    return log_return / (rv_per_bar * math.sqrt(n))


def vwap_position(price: float | None, vwap: float | None,
                  day_high: float | None, day_low: float | None) -> float | None:
    """Where price sits relative to session VWAP, scaled by the day's range.

    Range-scaled so the number is comparable across instruments and days;
    a raw (price - vwap) is in points and means nothing on its own.
    """
    if price is None or vwap is None or day_high is None or day_low is None:
        return None
    span = day_high - day_low
    if span <= 0:
        return None
    return max(-1.0, min(1.0, (price - vwap) / span))


def range_position(price: float | None, day_high: float | None,
                   day_low: float | None) -> float | None:
    """0 = at the day's low, 1 = at the day's high."""
    if price is None or day_high is None or day_low is None:
        return None
    span = day_high - day_low
    if span <= 0:
        return None
    return max(0.0, min(1.0, (price - day_low) / span))


# --------------------------------------------------------------------------- #
# Volatility
# --------------------------------------------------------------------------- #
def yang_zhang_variance(bars) -> float | None:
    """Yang-Zhang PER-BAR variance from OHLC bars.

    `bars` is a sequence of (open, high, low, close).

    Chosen over close-to-close / Parkinson / Garman-Klass because it is the
    only common estimator that handles BOTH the jump between bars and intraday
    drift, at roughly 8x the efficiency of close-to-close. That efficiency is
    what makes a usable volatility read possible from ~30 minutes of 1-minute
    bars instead of needing days of history.

        YZ = sigma_overnight^2 + k*sigma_open_close^2 + (1-k)*sigma_RS^2
        k  = 0.34 / (1.34 + (n+1)/(n-1))

    Applied to intraday bars the "overnight" term becomes the bar-to-bar gap,
    which is the correct generalisation: it is the jump component either way.
    """
    rows = []
    for bar in (bars or []):
        try:
            o, h, l, c = (float(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]))
        except (TypeError, ValueError, IndexError):
            continue
        if min(o, h, l, c) <= 0 or h < l:
            continue
        rows.append((o, h, l, c))
    n = len(rows)
    if n < 3:
        return None

    # Overnight (gap) component: ln(O_i / C_{i-1})
    gaps = []
    for i in range(1, n):
        prev_close = rows[i - 1][3]
        open_i = rows[i][0]
        if prev_close > 0 and open_i > 0:
            gaps.append(math.log(open_i / prev_close))
    if len(gaps) < 2:
        return None
    gap_mean = sum(gaps) / len(gaps)
    sigma_o = sum((g - gap_mean) ** 2 for g in gaps) / (len(gaps) - 1)

    # Open-to-close component: ln(C_i / O_i)
    ocs = [math.log(c / o) for (o, _h, _l, c) in rows if o > 0 and c > 0]
    if len(ocs) < 2:
        return None
    oc_mean = sum(ocs) / len(ocs)
    sigma_c = sum((x - oc_mean) ** 2 for x in ocs) / (len(ocs) - 1)

    # Rogers-Satchell component (drift-independent)
    rs_terms = []
    for (o, h, l, c) in rows:
        rs_terms.append(math.log(h / c) * math.log(h / o)
                        + math.log(l / c) * math.log(l / o))
    sigma_rs = sum(rs_terms) / len(rs_terms)

    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    variance = sigma_o + k * sigma_c + (1 - k) * sigma_rs
    return variance if variance > 0 else None


def yang_zhang_vol(bars, annualisation_periods: float) -> float | None:
    """Yang-Zhang volatility, annualised and expressed in PERCENT.

    Percent so it is directly comparable with India VIX, which is quoted in
    percent — that comparison is the variance risk premium.
    """
    variance = yang_zhang_variance(bars)
    if variance is None or annualisation_periods <= 0:
        return None
    return math.sqrt(variance * annualisation_periods) * 100.0


def realized_vol_per_bar(bars) -> float | None:
    """Per-bar standard deviation (not annualised), for vol-scaled momentum."""
    variance = yang_zhang_variance(bars)
    return math.sqrt(variance) if variance else None


def percentile_of(value: float | None, history) -> float | None:
    """Percentage of `history` strictly below `value`, in [0, 100].

    Same convention as iv_rank_scanner.iv_percentile, so a VIX percentile here
    means what an IV percentile means elsewhere in the platform.
    """
    if value is None:
        return None
    values = [float(v) for v in (history or []) if v is not None]
    if len(values) < 2:
        return None
    below = sum(1 for v in values if v < value)
    return below / len(values) * 100.0


def z_score(value: float | None, history) -> float | None:
    values = [float(v) for v in (history or []) if v is not None]
    if value is None or len(values) < 3:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    if var <= 0:
        return None
    return (value - mean) / math.sqrt(var)


def stdev(values) -> float | None:
    nums = [float(v) for v in (values or []) if v is not None]
    if len(nums) < 2:
        return None
    mean = sum(nums) / len(nums)
    return math.sqrt(sum((v - mean) ** 2 for v in nums) / (len(nums) - 1))


def pct_changes(values) -> list[float]:
    nums = [float(v) for v in (values or []) if v is not None]
    return [(nums[i] - nums[i - 1]) / nums[i - 1] * 100.0
            for i in range(1, len(nums)) if nums[i - 1]]


def variance_risk_premium(iv_pct: float | None,
                          rv_pct: float | None) -> float | None:
    """VRP = IV^2 - RV^2, in percent-squared units.

    Positive means implied exceeds subsequent realized — the structural reason
    option selling is profitable on average (Bollerslev, Tauchen & Zhou 2009).

    This is a DESCRIPTION. Whether a given VRP means "sell premium" is a
    trading decision and lives in the strategy, not here.
    """
    if iv_pct is None or rv_pct is None:
        return None
    return iv_pct ** 2 - rv_pct ** 2


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return numerator / denominator


# --------------------------------------------------------------------------- #
# Normalisation helpers used by the axis classifiers
# --------------------------------------------------------------------------- #
def clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def normalise(value: float | None, low: float, high: float) -> float | None:
    """Map [low, high] onto [0, 1], clipped. Returns None for a missing input."""
    if value is None or high == low:
        return None
    return clip((value - low) / (high - low))


def signed_normalise(value: float | None, scale: float) -> float | None:
    """Map a signed value onto [-1, +1] using `scale` as the saturation point."""
    if value is None or scale <= 0:
        return None
    return max(-1.0, min(1.0, value / scale))


def weighted_score(parts: dict, weights: dict) -> tuple[float | None, float]:
    """Weighted mean over the parts that are present.

    Returns (score, coverage) where coverage is the fraction of total weight
    actually available. Missing inputs REDUCE COVERAGE rather than being
    treated as zero — a zero would be an assertion about the market, and it
    would drag every score toward the middle exactly when data is thin.
    """
    total_weight = sum(abs(w) for w in weights.values()) or 1.0
    used = 0.0
    acc = 0.0
    for name, weight in weights.items():
        value = parts.get(name)
        if value is None:
            continue
        acc += value * weight
        used += abs(weight)
    if used <= 0:
        return None, 0.0
    return acc / used, used / total_weight
