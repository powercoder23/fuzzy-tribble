# -*- coding: utf-8 -*-
"""
entry_gate.py — composite entry gate (ARCHITECTURE_REFACTOR_PLAN.md §9).

A single, reusable check any strategy can call before booking a trade:

    from entry_gate import passes
    if passes(security_id, side):       # side = "CE" | "PE"
        ... book the trade ...

Behaviour is config-driven (entry_gate_config.GATE_MODE):
  off  -> always allow (no lookup) — default, zero behaviour change
  soft -> always allow, but evaluate() returns the composite score for ranking
  hard -> block trades that disagree with the composite or are weak/low-score

Reads the composite_history table via composite_scanner.get_latest_composite
(zero broker calls, fail-open).
"""

from __future__ import annotations

import logging

import entry_gate_config as cfg

logger = logging.getLogger(__name__)

CE, PE = "CE", "PE"


def _norm_side(raw) -> str | None:
    s = str(raw or "").upper()
    if s in ("CE", "CALL", "C"):
        return CE
    if s in ("PE", "PUT", "P"):
        return PE
    return None


def evaluate(security_id, side, mode: str | None = None) -> dict:
    """Return {allow, reason, score, direction, grade} for a candidate trade."""
    mode = (mode or cfg.GATE_MODE).lower()
    side = _norm_side(side)

    if mode == "off":
        return {"allow": True, "reason": "gate_off", "score": None,
                "direction": None, "grade": None}

    try:
        from composite_scanner import get_latest_composite
        c = get_latest_composite(security_id)
    except Exception:
        c = {}

    if not c:
        return {"allow": cfg.ALLOW_IF_NO_COMPOSITE, "reason": "no_composite",
                "score": None, "direction": None, "grade": None}

    info = {"score": c.get("score"), "direction": c.get("direction"), "grade": c.get("grade")}

    if mode == "soft":
        # Never blocks — just surfaces the conviction for ranking/annotation.
        return {"allow": True, "reason": "soft", **info}

    # hard mode
    if c.get("grade") == "WEAK":
        return {"allow": False, "reason": "weak_grade", **info}
    if (c.get("score") or 0) < cfg.MIN_GATE_SCORE:
        return {"allow": False, "reason": "below_min_score", **info}
    if side is not None and c.get("direction") not in (None, side):
        return {"allow": False, "reason": "direction_mismatch", **info}
    return {"allow": True, "reason": "pass", **info}


def passes(security_id, side, mode: str | None = None) -> bool:
    """Boolean convenience wrapper around evaluate()."""
    return bool(evaluate(security_id, side, mode)["allow"])
