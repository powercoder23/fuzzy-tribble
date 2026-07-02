# -*- coding: utf-8 -*-
"""Engine data contracts — plain dataclasses shared across the funnel.

Everything here is broker-agnostic and JSON-serializable via .to_dict().
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict

CE, PE = "CE", "PE"
GREEN, AMBER, RED = "GREEN", "AMBER", "RED"
EMITTED, REJECTED, WATCH = "EMITTED", "REJECTED", "WATCH"


@dataclass
class FactorReading:
    """One factor's vote for one symbol. bias None = no signal (silent)."""
    name: str
    bias: str | None = None            # CE | PE | None
    strength: float = 0.0              # 0..1
    detail: dict = field(default_factory=dict)

    def alignment(self, direction: str) -> float:
        """Signed strength vs a trade direction: +strength aligned, -strength opposed."""
        if self.bias not in (CE, PE):
            return 0.0
        return self.strength if self.bias == direction else -self.strength


@dataclass
class TriggerEvent:
    """A price-action entry event on a completed candle."""
    kind: str                          # ORB | VWAP | BREAK_RETEST | SONAR_BAND
    direction: str                     # CE | PE
    quality: float = 0.5               # 0..1 (volume ratio, body %, level cleanliness)
    detail: dict = field(default_factory=dict)


@dataclass
class RegimeState:
    posture: str = AMBER               # GREEN | AMBER | RED
    lean: str | None = None            # CE | PE | None
    vix: float | None = None
    breadth_pct: float | None = None
    index_slope_pct: float | None = None
    size_mult: float = 0.5
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GateResult:
    name: str
    passed: bool
    reason: str = ""


@dataclass
class Decision:
    """The engine's single output object — emitted, rejected, or watch."""
    symbol: str
    security_id: str
    status: str                        # EMITTED | REJECTED | WATCH
    direction: str | None = None
    score: float = 0.0
    grade: str | None = None           # A+ | A | B | None
    trigger: TriggerEvent | None = None
    factors: list = field(default_factory=list)     # [FactorReading]
    gates: list = field(default_factory=list)       # [GateResult]
    breakdown: dict = field(default_factory=dict)   # per-factor contributions
    reject_reason: str = ""
    why: str = ""                                   # human one-liner for cockpit/telegram
    formula_ver: str = ""

    def factor_json(self) -> str:
        return json.dumps([asdict(f) for f in self.factors])

    def gate_json(self) -> str:
        return json.dumps([asdict(g) for g in self.gates])
