# -*- coding: utf-8 -*-
"""
pre_market_gate.py — 5-gate pre-entry quality filter for the discount scanner.

Companion to entry_gate.py (which checks composite-scanner conviction).
This gate enforces structural option-quality rules BEFORE any trade is
booked, regardless of signal score. Zero broker calls — reads only from
the signal dict and iv_history.db.

Why this exists
───────────────
The discount scanner scores correctly but the score is a PASS/FAIL cutoff,
not a quality rank. All 5 trades on 2026-06-25 scored 95.0 yet 4 lost:
• 360ONE  CE 1160: 5.7% OTM — strike unreachable (Gate 3 fails)
• GRASIM  CE 3300: 5.4% OTM + IV/HV 1.26 (Gates 2 + 3 fail)
• COFORGE CE 1540: IV/HV 1.06 + no OI backing (Gate 2 fails)
• FEDERALBNK CE:   IV/HV 1.19 (Gate 2 fails)
Only CUMMINSIND (IV/HV 0.74, OTM 3.0%) would survive — which was the winner.

Gate summary
────────────
Gate 1 │ IVR ≤ MAX_IVR             options cheap on 52-week history
Gate 2 │ IV/HV ≤ MAX_IV_HV_RATIO   IV not expensive vs realised vol
Gate 3 │ OTM% ≤ MAX_OTM_PCT        strike reachable within the session
Gate 4 │ PCR direction check        CE: PCR ≥ MIN_PCR_CE | PE: PCR ≤ MAX_PCR_PE
Gate 5 │ open positions < MAX_SIM   hard position cap (no shotgun entries)

Integration
───────────
OrderManager._apply_pre_market_gate() calls this in submit_signals() so
every opportunity from the discount scanner passes all 5 gates before
reaching paper_trader.open_trade().

Usage
─────
    from pre_market_gate import evaluate, passes

    result = evaluate(
        security_id="1234",
        symbol="CUMMINSIND",
        side="CE",
        spot=5629.0,
        strike=5800.0,
        iv=27.6,
        hv=37.5,
        iv_rank=39.0,
        open_positions=0,
    )
    if result["allow"]:
        ... book the trade ...
    else:
        logger.info("blocked %s: %s", symbol, result["reason"])
"""

from __future__ import annotations

import logging

import pre_market_gate_config as cfg
from collectors import iv_store

logger = logging.getLogger(__name__)

CE, PE = "CE", "PE"


# ── Helpers ──────────────────────────────────────────────────────────────── #

def _norm_side(raw) -> str | None:
    s = str(raw or "").upper().strip()
    if s in ("CE", "CALL", "C"):
        return CE
    if s in ("PE", "PUT", "P"):
        return PE
    return None


def _otm_pct(side: str, spot: float, strike: float) -> float | None:
    """Percent OTM from spot to strike. Always ≥ 0."""
    if not spot or spot <= 0 or not strike or strike <= 0:
        return None
    if side == CE:
        return max(0.0, (strike - spot) / spot * 100.0)
    return max(0.0, (spot - strike) / spot * 100.0)


def _get_pcr(security_id) -> float | None:
    """
    Read PCR (put OI / call OI) from the latest intraday snapshot in
    iv_history.db. Zero broker calls. Returns None if data is absent.
    """
    try:
        snap = iv_store.get_latest_snapshot(str(security_id))
        if not snap:
            return None
        call_oi = snap.get("total_call_oi") or 0.0
        put_oi  = snap.get("total_put_oi")  or 0.0
        if call_oi > 0:
            return round(put_oi / call_oi, 3)
    except Exception:
        logger.debug("pre_market_gate: PCR read failed for %s", security_id)
    return None


def _gate(name: str, passed: bool, value, threshold, reason: str) -> dict:
    return {
        "gate":      name,
        "pass":      passed,
        "value":     value,
        "threshold": threshold,
        "reason":    reason,
    }


# ── Public API ───────────────────────────────────────────────────────────── #

def evaluate(
    security_id,
    symbol: str,
    side: str,
    spot: float | None,
    strike: float | None,
    iv: float | None = None,
    hv: float | None = None,
    iv_rank: float | None = None,
    open_positions: int = 0,
    enforce_position_cap: bool = True,
) -> dict:
    """
    Run all gates and return a result dict.

    Parameters
    ----------
    security_id   : NSE security_id (int or str)
    symbol        : ticker e.g. "CUMMINSIND"
    side          : "CE" / "PE" (or "CALL" / "PUT")
    spot          : current spot price
    strike        : option strike being considered
    iv            : ATM implied volatility (%, e.g. 27.6)
    hv            : historical / realised volatility (%, e.g. 37.5)
    iv_rank       : 52-week IV rank (0–100)
    open_positions: count of already-open positions this session

    Returns
    -------
    {
        "allow"  : bool,
        "reason" : str,   # first failure description or "all_pass"
        "gates"  : list,  # per-gate dicts for logging / diagnostics
    }
    """
    # Settings-DB override (UI toggle) wins over the env/config default.
    try:
        import settings_store
        mode = settings_store.flag_str("PMG_GATE_MODE")
    except Exception:
        mode = cfg.GATE_MODE
    side = _norm_side(side)

    # Unknown side → fail-open (let existing validators catch it)
    if side is None:
        return {"allow": True, "reason": "unknown_side", "gates": []}

    if mode == "off":
        return {"allow": True, "reason": "gate_off", "gates": []}

    gates    = []
    failures = []

    # ── Gate 1: IVR ──────────────────────────────────────────────────────── #
    if iv_rank is not None:
        ok = iv_rank <= cfg.MAX_IVR
        g  = _gate("ivr", ok, round(iv_rank, 1), cfg.MAX_IVR,
                   f"IVR {iv_rank:.1f} {'≤' if ok else '>'} {cfg.MAX_IVR}")
        gates.append(g)
        if not ok:
            failures.append(g["reason"])
    elif cfg.SKIP_IVR_IF_MISSING:
        gates.append(_gate("ivr", True, None, cfg.MAX_IVR, "IVR missing — skipped (fail-open)"))
    else:
        g = _gate("ivr", False, None, cfg.MAX_IVR, "IVR missing — blocked")
        gates.append(g)
        failures.append(g["reason"])

    # ── Gate 2: IV / HV ratio ────────────────────────────────────────────── #
    if iv is not None and hv is not None and hv > 0:
        ratio = round(iv / hv, 3)
        ok    = ratio <= cfg.MAX_IV_HV_RATIO
        g     = _gate("iv_hv", ok, ratio, cfg.MAX_IV_HV_RATIO,
                      f"IV/HV {ratio:.2f} {'≤' if ok else '>'} {cfg.MAX_IV_HV_RATIO}"
                      f" (IV {iv:.1f} / HV {hv:.1f})")
        gates.append(g)
        if not ok:
            failures.append(g["reason"])
    elif cfg.SKIP_IV_HV_IF_MISSING:
        gates.append(_gate("iv_hv", True, None, cfg.MAX_IV_HV_RATIO,
                           "IV/HV missing — skipped (fail-open)"))
    else:
        g = _gate("iv_hv", False, None, cfg.MAX_IV_HV_RATIO, "IV/HV data missing — blocked")
        gates.append(g)
        failures.append(g["reason"])

    # ── Gate 3: OTM% ─────────────────────────────────────────────────────── #
    if spot is not None and strike is not None:
        spot_f, strike_f = float(spot), float(strike)
        if spot_f > 0 and strike_f > 0:
            otm = _otm_pct(side, spot_f, strike_f)
            if otm is not None:
                ok = otm <= cfg.MAX_OTM_PCT
                g  = _gate("otm_pct", ok, round(otm, 2), cfg.MAX_OTM_PCT,
                           f"OTM {otm:.1f}% {'≤' if ok else '>'} {cfg.MAX_OTM_PCT}%"
                           f" (spot {spot} → strike {strike})")
                gates.append(g)
                if not ok:
                    failures.append(g["reason"])
            else:
                gates.append(_gate("otm_pct", True, None, cfg.MAX_OTM_PCT,
                                   "OTM% calc failed — skipped"))
        else:
            g = _gate("otm_pct", False, spot, cfg.MAX_OTM_PCT,
                      f"spot/strike is zero — blocked (spot={spot}, strike={strike})")
            gates.append(g)
            failures.append(g["reason"])
    else:
        gates.append(_gate("otm_pct", True, None, cfg.MAX_OTM_PCT,
                           "spot/strike missing — skipped"))

    # ── Gate 4: PCR direction ────────────────────────────────────────────── #
    pcr = _get_pcr(security_id)
    if pcr is not None:
        if side == CE:
            ok = pcr >= cfg.MIN_PCR_CE
            g  = _gate("pcr", ok, pcr, cfg.MIN_PCR_CE,
                       f"PCR {pcr:.2f} {'≥' if ok else '<'} {cfg.MIN_PCR_CE}"
                       f" (CE needs put backing)")
        else:
            ok = pcr <= cfg.MAX_PCR_PE
            g  = _gate("pcr", ok, pcr, cfg.MAX_PCR_PE,
                       f"PCR {pcr:.2f} {'≤' if ok else '>'} {cfg.MAX_PCR_PE}"
                       f" (PE: no squeeze risk)")
        gates.append(g)
        if not ok:
            failures.append(g["reason"])
    elif cfg.PCR_FAIL_OPEN:
        gates.append(_gate("pcr", True, None, None,
                           "PCR unavailable — fail-open"))
    else:
        g = _gate("pcr", False, None, None, "PCR unavailable — blocked")
        gates.append(g)
        failures.append(g["reason"])

    # ── Gate 5: simultaneous position cap ───────────────────────────────── #
    # External strategies (e.g. Break & Bounce) own their own daily cap and are
    # routed here only for the QUALITY gates; the shared 2-position cap would
    # otherwise reject them whenever the discount scanner has already filled its
    # slots, so it is skipped when enforce_position_cap is False.
    if enforce_position_cap:
        ok = open_positions < cfg.MAX_SIMULTANEOUS
        g  = _gate("simultaneous", ok, open_positions, cfg.MAX_SIMULTANEOUS,
                   f"{open_positions} open {'<' if ok else '≥'} {cfg.MAX_SIMULTANEOUS} max")
        gates.append(g)
        if not ok:
            failures.append(g["reason"])
    else:
        gates.append(_gate("simultaneous", True, open_positions, cfg.MAX_SIMULTANEOUS,
                           "position cap skipped (external strategy owns its own cap)"))

    # ── Verdict ──────────────────────────────────────────────────────────── #
    if failures:
        reason = "; ".join(failures)
        allow  = mode == "soft"   # soft never blocks, hard does
        label  = "ALLOW (soft)" if allow else "BLOCK"
        logger.info(
            "pre_market_gate [%s] %s %s spot=%s strike=%s → %s | %s",
            mode.upper(), symbol, side, spot, strike, label, reason,
        )
    else:
        allow  = True
        reason = "all_pass"
        logger.debug("pre_market_gate PASS %s %s spot=%s", symbol, side, spot)

    return {"allow": allow, "reason": reason, "gates": gates}


def passes(
    security_id,
    symbol: str,
    side: str,
    spot: float | None,
    strike: float | None,
    iv: float | None = None,
    hv: float | None = None,
    iv_rank: float | None = None,
    open_positions: int = 0,
) -> bool:
    """Boolean convenience wrapper around evaluate()."""
    return bool(evaluate(
        security_id=security_id, symbol=symbol, side=side,
        spot=spot, strike=strike,
        iv=iv, hv=hv, iv_rank=iv_rank,
        open_positions=open_positions,
    )["allow"])
