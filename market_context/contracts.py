# -*- coding: utf-8 -*-
"""
market_context/contracts.py — frozen, read-only shapes returned by
``market_context.get()``.

DESIGN RULE (operator decision 2026-08-03)
------------------------------------------
The Market Context subsystem **describes the market. It does not decide
trades.** There is deliberately NO field here for:

  * long/short premium bias      * size multiplier
  * entry veto / allow           * exit warning
  * any "should I ..." verdict

Each strategy consumes only the axes it needs and makes its own decision.
Adding a decision field to this module is a design regression — the whole
point is that market description lives in one place and trade policy stays
with the strategy that owns it.

Six independent axes, each with an identical shape so a consumer can treat
them uniformly:

    trend         — direction and quality of price movement
    volatility    — level and expansion/compression of volatility
    liquidity     — the market's capacity to absorb size (spread/depth)
    participation — how much activity there is (volume vs its own normal)
    positioning   — futures price x OI quadrant, basis
    breadth       — how many names are moving, and in which direction

Axes are INDEPENDENT on purpose. TRENDING_UP + PANIC + SHORT_COVERING is a
real, distinct, actionable tape; collapsing it to one label would destroy
that. There is no composite score.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Mapping

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# Axis names — the canonical keys everywhere (DB columns, dicts, get().axis())
# --------------------------------------------------------------------------- #
AXIS_TREND = "trend"
AXIS_VOLATILITY = "volatility"
AXIS_LIQUIDITY = "liquidity"
AXIS_PARTICIPATION = "participation"
AXIS_POSITIONING = "positioning"
AXIS_BREADTH = "breadth"

ALL_AXES = (
    AXIS_TREND,
    AXIS_VOLATILITY,
    AXIS_LIQUIDITY,
    AXIS_PARTICIPATION,
    AXIS_POSITIONING,
    AXIS_BREADTH,
)

# --------------------------------------------------------------------------- #
# State vocabularies (descriptive labels only — no verdicts)
# --------------------------------------------------------------------------- #
UNKNOWN = "UNKNOWN"

# trend.state
TRENDING_UP = "TRENDING_UP"
TRENDING_DOWN = "TRENDING_DOWN"
RANGE = "RANGE"
TRANSITIONING = "TRANSITIONING"
TREND_STATES = (TRENDING_UP, TRENDING_DOWN, RANGE, TRANSITIONING, UNKNOWN)

# trend.event — structural events layered on the trend axis. Kept on `trend`
# rather than promoted to a seventh axis because a breakout IS a trend-
# structure event, and the operator specified exactly six axes.
EVENT_NONE = "NONE"
BREAKOUT = "BREAKOUT"
BREAKDOWN = "BREAKDOWN"
REVERSAL = "REVERSAL"
TREND_EVENTS = (EVENT_NONE, BREAKOUT, BREAKDOWN, REVERSAL)

# volatility.state
LOW_VOL = "LOW_VOL"
NORMAL_VOL = "NORMAL_VOL"
HIGH_VOL = "HIGH_VOL"
PANIC = "PANIC"
VOLATILITY_STATES = (LOW_VOL, NORMAL_VOL, HIGH_VOL, PANIC, UNKNOWN)

# liquidity.state
LIQUID = "LIQUID"
NORMAL_LIQUIDITY = "NORMAL_LIQUIDITY"
THIN = "THIN"
ILLIQUID = "ILLIQUID"
LIQUIDITY_STATES = (LIQUID, NORMAL_LIQUIDITY, THIN, ILLIQUID, UNKNOWN)

# participation.state
HIGH_PARTICIPATION = "HIGH_PARTICIPATION"
NORMAL_PARTICIPATION = "NORMAL_PARTICIPATION"
LOW_PARTICIPATION = "LOW_PARTICIPATION"
PARTICIPATION_STATES = (
    HIGH_PARTICIPATION, NORMAL_PARTICIPATION, LOW_PARTICIPATION, UNKNOWN,
)

# positioning.state — the India-standard futures price x OI quadrant
LONG_BUILDUP = "LONG_BUILDUP"
SHORT_BUILDUP = "SHORT_BUILDUP"
SHORT_COVERING = "SHORT_COVERING"
LONG_LIQUIDATION = "LONG_LIQUIDATION"
POSITIONING_NEUTRAL = "NEUTRAL"
POSITIONING_STATES = (
    LONG_BUILDUP, SHORT_BUILDUP, SHORT_COVERING, LONG_LIQUIDATION,
    POSITIONING_NEUTRAL, UNKNOWN,
)

# breadth.state
STRONG_POSITIVE = "STRONG_POSITIVE"
POSITIVE = "POSITIVE"
BREADTH_NEUTRAL = "NEUTRAL"
NEGATIVE = "NEGATIVE"
STRONG_NEGATIVE = "STRONG_NEGATIVE"
BREADTH_STATES = (
    STRONG_POSITIVE, POSITIVE, BREADTH_NEUTRAL, NEGATIVE, STRONG_NEGATIVE,
    UNKNOWN,
)

# <axis>.direction — the derivative of the axis score, i.e. is the CURRENT
# state getting more or less pronounced. This is early; a state flip is late.
STRENGTHENING = "STRENGTHENING"
WEAKENING = "WEAKENING"
STABLE = "STABLE"
DIRECTIONS = (STRENGTHENING, WEAKENING, STABLE, UNKNOWN)

_NEUTRAL_STATE_BY_AXIS = {
    AXIS_TREND: UNKNOWN,
    AXIS_VOLATILITY: UNKNOWN,
    AXIS_LIQUIDITY: UNKNOWN,
    AXIS_PARTICIPATION: UNKNOWN,
    AXIS_POSITIONING: UNKNOWN,
    AXIS_BREADTH: UNKNOWN,
}


# --------------------------------------------------------------------------- #
# One axis
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AxisState:
    """A single market-description axis.

    name          canonical axis key (see ALL_AXES)
    state         descriptive label from that axis's vocabulary
    score         normalised magnitude. Signed (-1..+1) for `trend`, which has
                  a natural direction; unsigned (0..1) for every other axis.
    confidence    0..1 — how well IDENTIFIED this state is right now. NOT a
                  probability of profit. Low confidence means "the reading is
                  ambiguous or the inputs are incomplete", nothing more.
    direction     STRENGTHENING / WEAKENING / STABLE — derivative of `score`
                  WITHIN the current state.
    dwell_minutes how long this state has been continuously held.
    transition_prob 0..1 — likelihood the state changes within the engine's
                  configured horizon.
    event         structural event; only `trend` populates it today.
    available     False when the axis could not be computed. `state` is then
                  UNKNOWN and a consumer must not read `score`.
    inputs        raw feature values behind the classification, for audit and
                  for research. Point-in-time: what was known at `as_of`.
    reasons       human-readable explanation, for alerts and the dashboard.
    """

    name: str
    state: str = UNKNOWN
    score: float = 0.0
    confidence: float = 0.0
    direction: str = UNKNOWN
    dwell_minutes: int = 0
    transition_prob: float = 0.0
    event: str = EVENT_NONE
    available: bool = False
    inputs: Mapping[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    def is_(self, *states: str) -> bool:
        """True when this axis is in any of `states`. Reads naturally at the
        call site: ``ctx.volatility.is_(HIGH_VOL, PANIC)``."""
        return self.available and self.state in states

    def as_dict(self) -> dict:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        d["inputs"] = dict(self.inputs or {})
        return d


def neutral_axis(name: str) -> AxisState:
    """An explicitly-unavailable axis. Every field is inert, so a consumer
    that forgets to check `available` still cannot be misled into acting."""
    return AxisState(
        name=name,
        state=_NEUTRAL_STATE_BY_AXIS.get(name, UNKNOWN),
        score=0.0,
        confidence=0.0,
        direction=UNKNOWN,
        dwell_minutes=0,
        transition_prob=0.0,
        event=EVENT_NONE,
        available=False,
        inputs={},
        reasons=("market context unavailable",),
    )


# --------------------------------------------------------------------------- #
# The full snapshot
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MarketContext:
    """Point-in-time description of the market. Returned by ``get()``.

    `available` is the ONE field every consumer must check. When False, every
    axis is neutral/unavailable and the snapshot carries no information — it
    is not a bearish reading, it is an absent reading.
    """

    available: bool = False
    as_of: str | None = None
    age_seconds: float | None = None
    data_quality: float = 0.0
    missing_inputs: tuple[str, ...] = ()
    config_version: str = ""
    config_hash: str = ""
    schema_version: int = SCHEMA_VERSION
    source: str = "neutral"          # neutral | db | cache

    trend: AxisState = field(default_factory=lambda: neutral_axis(AXIS_TREND))
    volatility: AxisState = field(default_factory=lambda: neutral_axis(AXIS_VOLATILITY))
    liquidity: AxisState = field(default_factory=lambda: neutral_axis(AXIS_LIQUIDITY))
    participation: AxisState = field(default_factory=lambda: neutral_axis(AXIS_PARTICIPATION))
    positioning: AxisState = field(default_factory=lambda: neutral_axis(AXIS_POSITIONING))
    breadth: AxisState = field(default_factory=lambda: neutral_axis(AXIS_BREADTH))

    # ---- access helpers ---------------------------------------------------- #
    def axis(self, name: str) -> AxisState:
        """Axis by name. Unknown names return a neutral axis rather than
        raising — a typo in a strategy must never break a scan."""
        return getattr(self, name, None) or neutral_axis(name)

    def axes(self) -> dict[str, AxisState]:
        return {n: self.axis(n) for n in ALL_AXES}

    def available_axes(self) -> tuple[str, ...]:
        return tuple(n for n in ALL_AXES if self.axis(n).available)

    def as_dict(self) -> dict:
        """JSON-safe dict for persistence into ``paper_trades.factors_json``.

        Deliberately flat-ish and stable: this becomes the historical record
        used to answer 'did market context predict anything?', so its shape
        must not churn. Shape changes require a SCHEMA_VERSION bump.
        """
        return {
            "available": self.available,
            "as_of": self.as_of,
            "age_seconds": self.age_seconds,
            "data_quality": self.data_quality,
            "missing_inputs": list(self.missing_inputs),
            "config_version": self.config_version,
            "config_hash": self.config_hash,
            "schema_version": self.schema_version,
            "source": self.source,
            "axes": {n: self.axis(n).as_dict() for n in ALL_AXES},
        }

    def summary(self) -> str:
        """One-line human summary for logs and Telegram."""
        if not self.available:
            return "market context: UNAVAILABLE"
        bits = []
        for n in ALL_AXES:
            a = self.axis(n)
            if a.available:
                bits.append(f"{n}={a.state}")
        return (
            f"market context @ {self.as_of} "
            f"({', '.join(bits) if bits else 'no axes'}) "
            f"dq={self.data_quality:.2f}"
        )


NEUTRAL_CONTEXT = MarketContext()
"""The fail-open singleton. Returned whenever context cannot be produced.

Every field is inert by construction, so a consumer that ignores
`available` cannot be pushed into a decision by a missing subsystem.
"""
