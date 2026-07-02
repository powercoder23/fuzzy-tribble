# -*- coding: utf-8 -*-
"""Conviction scorer — gates first, then ONE score (funnel steps 5–6).

Pure functions only. Supersedes the three V1 fusion layers (composite_scanner,
trade_suggester, morning_confluence) and the entry/cycle/pre-market gate trio.
Formula version travels with every Decision so weights can evolve on journal
evidence without corrupting attribution history.
"""

from __future__ import annotations

from engine import config as cfg
from engine.contracts import (FactorReading, TriggerEvent, RegimeState,
                              GateResult, RED, CE, PE)

FACTOR_WEIGHTS = {
    "oi_flow": cfg.W_OI_FLOW,
    "trend": cfg.W_TREND,
    "sector_rs": cfg.W_SECTOR_RS,
    "inst_flow": cfg.W_INST_FLOW,
    "gap": cfg.W_GAP,
}


# --------------------------------------------------------------------------- #
# Gate stack — hard, unordered, any-fail = reject with reason
# --------------------------------------------------------------------------- #
def run_gates(regime: RegimeState, factors: dict, trigger: TriggerEvent,
              risk_state: dict | None = None, now_hhmm: str | None = None) -> list[GateResult]:
    """risk_state: {open_positions, day_pnl_pct, sl_hits_today} (executor-fed)."""
    rs = risk_state or {}
    gates: list[GateResult] = []

    def gate(name, ok, why=""):
        gates.append(GateResult(name, bool(ok), why))

    gate("regime_not_red", regime.posture != RED,
         "" if regime.posture != RED else f"regime RED: {';'.join(regime.reasons)}")
    gate("vix_ceiling", regime.vix is None or regime.vix < cfg.VIX_RED,
         "" if regime.vix is None or regime.vix < cfg.VIX_RED else f"vix {regime.vix:.1f}")

    zone = factors.get("premium_value", FactorReading("premium_value")).detail.get("zone")
    gate("premium_not_expensive", zone != "EXPENSIVE",
         "" if zone != "EXPENSIVE" else "IV rank EXPENSIVE — buyer's edge gone")

    # Net factor confluence must not contradict the trigger direction.
    net = sum(f.alignment(trigger.direction) for f in factors.values()
              if f.name != "premium_value")
    gate("factors_not_contradicting", net >= 0.0,
         "" if net >= 0.0 else f"net factor alignment {net:.2f} against {trigger.direction}")

    # Regime lean: in AMBER/GREEN with a lean, don't fight the tape.
    if regime.lean in (CE, PE):
        gate("with_the_tape", trigger.direction == regime.lean,
             "" if trigger.direction == regime.lean
             else f"trigger {trigger.direction} vs market lean {regime.lean}")

    if now_hhmm is not None:
        gate("entry_cutoff", now_hhmm <= cfg.ENTRY_CUTOFF,
             "" if now_hhmm <= cfg.ENTRY_CUTOFF else f"past {cfg.ENTRY_CUTOFF}")

    gate("slots_free", rs.get("open_positions", 0) < cfg.MAX_CONCURRENT,
         "" if rs.get("open_positions", 0) < cfg.MAX_CONCURRENT else "max concurrent")
    gate("daily_loss_ok", rs.get("day_pnl_pct", 0.0) > -cfg.DAILY_LOSS_LIMIT_PCT,
         "" if rs.get("day_pnl_pct", 0.0) > -cfg.DAILY_LOSS_LIMIT_PCT else "daily loss lockout")
    gate("sl_hits_ok", rs.get("sl_hits_today", 0) < cfg.MAX_SL_HITS_PER_DAY,
         "" if rs.get("sl_hits_today", 0) < cfg.MAX_SL_HITS_PER_DAY else "2 SL hits — stop")

    return gates


def gates_pass(gates: list[GateResult]) -> tuple[bool, str]:
    failed = [g for g in gates if not g.passed]
    return (not failed), "; ".join(f"{g.name}: {g.reason}" for g in failed)


# --------------------------------------------------------------------------- #
# Score — one formula (0–100), direction set by the trigger
# --------------------------------------------------------------------------- #
def score(trigger: TriggerEvent, factors: dict, regime: RegimeState) -> dict:
    """Weighted confluence around the trigger direction.

    Trigger contributes W_TRIGGER x quality (always positive — it defines the
    direction). Directional factors contribute signed alignment. premium_value
    is direction-neutral: CHEAP adds its full weight.
    Returns {score, grade, breakdown, n_agree}.
    """
    direction = trigger.direction
    breakdown = {"trigger": round(cfg.W_TRIGGER * trigger.quality, 2)}
    total = cfg.W_TRIGGER * trigger.quality
    n_agree = 0

    for name, w in FACTOR_WEIGHTS.items():
        f = factors.get(name)
        contrib = w * f.alignment(direction) if f else 0.0
        breakdown[name] = round(contrib, 2)
        total += contrib
        if contrib > 0:
            n_agree += 1

    pv = factors.get("premium_value")
    pv_contrib = cfg.W_PREMIUM_VALUE * (pv.strength if pv else 0.0)
    breakdown["premium_value"] = round(pv_contrib, 2)
    total += pv_contrib

    if n_agree >= cfg.CONFLUENCE_MIN_AGREE:
        total *= (1.0 + cfg.CONFLUENCE_BONUS)
        breakdown["confluence_bonus"] = f"+{cfg.CONFLUENCE_BONUS:.0%}"
    if regime.vix is not None and cfg.VIX_ELEVATED <= regime.vix < cfg.VIX_RED:
        total *= (1.0 - cfg.VIX_ELEVATED_PENALTY)
        breakdown["vix_penalty"] = f"-{cfg.VIX_ELEVATED_PENALTY:.0%}"

    s = max(0.0, min(100.0, total))
    grade = ("A+" if s >= cfg.GRADE_A_PLUS else
             "A" if s >= cfg.GRADE_A else
             "B" if s >= cfg.GRADE_B else None)
    return {"score": round(s, 1), "grade": grade,
            "breakdown": breakdown, "n_agree": n_agree}


def context_score(factors: dict) -> float:
    """Trigger-less score for the WATCH list: strongest one-sided confluence,
    normalized to 0–100 against the max possible factor total."""
    best = 0.0
    denom = sum(FACTOR_WEIGHTS.values()) + cfg.W_PREMIUM_VALUE
    pv = factors.get("premium_value")
    pv_c = cfg.W_PREMIUM_VALUE * (pv.strength if pv else 0.0)
    for side in (CE, PE):
        tot = sum(w * max(0.0, factors[n].alignment(side))
                  for n, w in FACTOR_WEIGHTS.items() if n in factors) + pv_c
        best = max(best, tot)
    return round(best / denom * 100.0, 1) if denom else 0.0


def why_line(symbol: str, trigger: TriggerEvent, res: dict, factors: dict) -> str:
    """Human one-liner for cockpit/Telegram — the trade's complete 'why'."""
    agree = [n for n, w in FACTOR_WEIGHTS.items()
             if factors.get(n) and factors[n].alignment(trigger.direction) > 0]
    zone = factors.get("premium_value", FactorReading("x")).detail.get("zone", "-")
    return (f"{symbol} {trigger.direction} — {trigger.kind} trigger (q{trigger.quality:.1f}) "
            f"+ {', '.join(agree) or 'no factor support'} | IV {zone} "
            f"| {res['grade']} {res['score']}")
