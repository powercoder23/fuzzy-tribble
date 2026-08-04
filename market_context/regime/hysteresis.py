# -*- coding: utf-8 -*-
"""
market_context/regime/hysteresis.py — state persistence for the axis classifiers.

This is the least glamorous file in the subsystem and the one that decides
whether the output is usable. A classifier that flips every snapshot is worse
than no classifier: it whipsaws every consumer downstream, and any research
built on the series measures the flapping rather than the market.

Three independent guards, all configurable:

  * **K-of-M confirmation** — a new reading must appear at least
    CONFIRMATION_COUNT times in the last CONFIRMATION_WINDOW snapshots.
  * **Minimum dwell** — the OUTGOING state must have been held for at least
    MIN_DWELL_MINUTES.
  * **Explicit TRANSITIONING** — during genuine ambiguity the axis says so
    rather than picking a side, and confidence collapses accordingly.

(The fourth guard, asymmetric entry/exit thresholds, is axis-specific and
lives in axes.py where the thresholds are.)

The first observation on a fresh axis is adopted immediately: requiring dwell
in an UNKNOWN state would keep every axis unknown for the first five minutes
of every session for no benefit.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from market_context import config as cfg
from market_context.contracts import (
    STABLE, STRENGTHENING, TRANSITIONING, UNKNOWN, WEAKENING,
)


@dataclass
class AxisObservation:
    """What the tracker publishes for one axis this snapshot."""
    state: str
    dwell_minutes: int
    direction: str
    momentum: float | None
    transitioned: bool
    raw_state: str


@dataclass
class AxisTracker:
    """Per-axis published state, dwell and score history."""

    name: str
    state: str = UNKNOWN
    since: datetime | None = None
    raw_history: deque = field(default_factory=lambda: deque(maxlen=32))
    score_history: deque = field(default_factory=lambda: deque(maxlen=256))

    # ---- helpers ---------------------------------------------------------- #
    def dwell_minutes(self, now: datetime) -> int:
        if self.since is None:
            return 0
        return max(int((now - self.since).total_seconds() // 60), 0)

    def _confirmed(self, raw_state: str) -> bool:
        window = list(self.raw_history)[-cfg.CONFIRMATION_WINDOW:]
        return window.count(raw_state) >= cfg.CONFIRMATION_COUNT

    def momentum(self, now: datetime) -> float | None:
        """d(score)/dt over MOMENTUM_LOOKBACK_MIN, per minute.

        This is the derivative of the score WITHIN the current state, which is
        what makes 'strengthening' and 'weakening' available EARLY — a state
        flip is late by construction.
        """
        if len(self.score_history) < 2:
            return None
        cutoff_seconds = cfg.MOMENTUM_LOOKBACK_MIN * 60
        anchor = None
        for ts, score in self.score_history:
            if (now - ts).total_seconds() <= cutoff_seconds:
                anchor = (ts, score)
                break
        if anchor is None:
            anchor = self.score_history[0]
        latest_ts, latest_score = self.score_history[-1]
        elapsed_min = (latest_ts - anchor[0]).total_seconds() / 60.0
        if elapsed_min <= 0 or latest_score is None or anchor[1] is None:
            return None
        return (latest_score - anchor[1]) / elapsed_min

    # ---- main ------------------------------------------------------------- #
    def update(self, raw_state: str, score: float | None,
               now: datetime) -> AxisObservation:
        """Fold one raw classification into the published state."""
        self.raw_history.append(raw_state)
        if score is not None:
            self.score_history.append((now, float(score)))

        transitioned = False
        published = self.state

        if raw_state == UNKNOWN:
            # No reading this snapshot: hold the last published state rather
            # than blanking it, but do not extend its dwell credit.
            pass
        elif self.state in (UNKNOWN, "") or self.since is None:
            published = raw_state                    # adopt immediately
            self.state = raw_state
            self.since = now
            transitioned = True
        elif raw_state == self.state:
            published = self.state                   # dwell keeps accruing
        elif not self._confirmed(raw_state):
            published = (TRANSITIONING if cfg.EMIT_TRANSITIONING else self.state)
        elif self.dwell_minutes(now) < cfg.MIN_DWELL_MINUTES:
            published = self.state                   # too soon to flip
        else:
            published = raw_state
            self.state = raw_state
            self.since = now
            transitioned = True

        momentum = self.momentum(now)
        return AxisObservation(
            state=published,
            dwell_minutes=self.dwell_minutes(now),
            direction=classify_direction(momentum, published, self.state),
            momentum=momentum,
            transitioned=transitioned,
            raw_state=raw_state,
        )


def classify_direction(momentum: float | None, published: str,
                       current: str) -> str:
    """STRENGTHENING / WEAKENING / STABLE from the score derivative.

    'Strengthening' means the CURRENT state is becoming more pronounced, so a
    falling score in a bearish-signed state (e.g. TRENDING_DOWN, whose score
    is negative) is strengthening, not weakening. Sign is therefore taken
    relative to the state, not absolutely.
    """
    if momentum is None or published in (UNKNOWN, TRANSITIONING):
        return UNKNOWN
    if abs(momentum) < cfg.MOMENTUM_EPS:
        return STABLE
    negative_state = current.endswith("_DOWN") or current in (
        "NEGATIVE", "STRONG_NEGATIVE", "LONG_LIQUIDATION", "SHORT_BUILDUP")
    intensifying = (momentum < 0) if negative_state else (momentum > 0)
    return STRENGTHENING if intensifying else WEAKENING


def transition_probability(momentum: float | None, boundary_margin: float | None,
                           confidence: float | None,
                           base: float | None = None) -> float:
    """P(state changes within TRANSITION_HORIZON_MIN), in [0, 1].

    Rule-based in v1 by design. A fitted Markov model needs a history of
    point-in-time observations that does not exist yet; mc_regime is what
    produces it. `p_base` is the single seam to swap in later — the consumer
    contract does not change.
    """
    prob = cfg.TRANSITION_BASE_PROB if base is None else base
    if momentum is not None:
        prob += cfg.TRANSITION_K_MOMENTUM * min(abs(momentum), 1.0)
    if boundary_margin is not None:
        prob += cfg.TRANSITION_K_MARGIN * (1.0 - max(0.0, min(1.0, boundary_margin)))
    if confidence is not None:
        prob += cfg.TRANSITION_K_CONFIDENCE * (1.0 - max(0.0, min(1.0, confidence)))
    return max(0.0, min(1.0, prob))


def confidence_score(agreement: float | None, data_quality: float | None,
                     dwell_minutes: int, boundary_margin: float | None,
                     subsample: bool = False) -> float:
    """How well IDENTIFIED the current state is — NOT a probability of profit.

    Low confidence means the reading is ambiguous or the inputs are thin. It
    says nothing about whether the described market is favourable, which is
    deliberately not this subsystem's question.
    """
    weights = cfg.CONF_WEIGHTS
    parts = {
        "agreement": agreement,
        "data_quality": data_quality,
        "dwell": min(dwell_minutes / max(cfg.MIN_DWELL_MINUTES, 1), 1.0),
        "margin": boundary_margin,
    }
    used = 0.0
    acc = 0.0
    for key, weight in weights.items():
        value = parts.get(key)
        if value is None:
            continue
        acc += max(0.0, min(1.0, float(value))) * weight
        used += weight
    if used <= 0:
        return 0.0
    score = acc / used
    if subsample:
        # A partial-universe breadth reading cannot be fully trusted, and
        # saying so is the point of tracking is_subsample at all.
        score = min(score, cfg.CONF_SUBSAMPLE_CEILING)
    return max(0.0, min(1.0, score))
