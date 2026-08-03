# -*- coding: utf-8 -*-
"""Convex paper booking (E1-1) — trade the engine's own EMITTED decisions on
paper so grade->edge is measured on realized option P&L, not just forward spot
moves (which the Week-1 review flagged as a weak proxy).

Zero broker calls, by design:
  * entry premium is ESTIMATED from ATM IV via expected_move (Brenner-
    Subrahmanyam ~0.4 x 1-sigma), scaled to the contract's DTE;
  * the ATM strike is read from the iv_history snapshot the collector writes;
  * expiry / lot / option-id come from the local scrip-master DB.

Trades land in the SHARED paper_trades.db tagged
"{cfg.PAPER_STRATEGY_TAG}-{trigger_kind}" (e.g. "Convex-ORB",
"Convex-SONAR_BAND") through paper_trader.book_signal — the same universal
guards (entry cutoff, per symbol+strike+side dedup, min-premium floor,
hard rupee-risk cap) as every other strategy, but WITHOUT V1's pre-market
/ breadth / concentration gates, so the engine's own conviction is what
gets measured. The existing discount-container monitor then marks and
exits these rows with real quotes.

SL/target are NOT the shared max_risk_rupees cap (skip_risk_cap=True below
— this book measures raw conviction, sizing is separate, E5-1): SL is the
percentage-of-premium distance capped at PAPER_MAX_LOSS_RUPEES regardless
of lot_size; target is scaled to the stock's own 1-day expected ATM-premium
move (real ATM IV), not a flat multiplier. See engine/config.py.

Gated by ENGINE_PAPER_MODE (off | paper). off is a hard no-op — nothing is
written. Books only the configured grades (default A+/A), highest score first,
up to PAPER_MAX_TRADES per day across all cycles.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, date

from engine import config as cfg
from engine import expected_move

logger = logging.getLogger(__name__)

# (underlying, side, today_iso) -> (expiry_iso, lot_size) | None
_contract_cache: dict = {}


# --------------------------------------------------------------------------- #
# Zero-API contract resolution
# --------------------------------------------------------------------------- #
def _atm_strike(db_path: str, security_id: str) -> float | None:
    """Latest intraday ATM strike the collector recorded for this name."""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT atm_strike FROM iv_history WHERE security_id=? "
                "AND data_type='intraday' AND atm_strike IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 1",
                (str(security_id),)).fetchone()
        return float(row[0]) if row and row[0] else None
    except (sqlite3.OperationalError, ValueError, TypeError):
        return None


def _nearest_expiry(underlying: str, opt_type: str, today_iso: str,
                    min_dte: int) -> tuple[str, int] | None:
    """(expiry_iso, lot_size) for the nearest option expiry >= today+min_dte,
    read from the local scrip master. None when the name has no listed option."""
    key = (underlying, opt_type, today_iso)
    if key in _contract_cache:
        return _contract_cache[key]
    result = None
    try:
        with sqlite3.connect(cfg.SCRIP_MASTER_DB) as conn:
            rows = conn.execute(
                "SELECT SEM_EXPIRY_DATE, SEM_LOT_UNITS FROM scrip_master "
                "WHERE SEM_TRADING_SYMBOL LIKE ? AND SEM_OPTION_TYPE=? "
                "AND date(SEM_EXPIRY_DATE) >= date(?) "
                "ORDER BY SEM_EXPIRY_DATE LIMIT 5",
                (underlying + "-%", opt_type, today_iso)).fetchall()
        today = date.fromisoformat(today_iso)
        for exp_raw, lot in rows:
            exp_iso = str(exp_raw)[:10]
            try:
                dte = (date.fromisoformat(exp_iso) - today).days
            except ValueError:
                continue
            if dte >= min_dte:
                result = (exp_iso, int(float(lot)) if lot else 1)
                break
    except (sqlite3.OperationalError, ValueError, TypeError):
        result = None
    _contract_cache[key] = result
    return result


def _option_sid(underlying: str, opt_type: str, expiry_iso: str,
                strike: float) -> str | None:
    """Exact option SEM_SMST_SECURITY_ID for (underlying, expiry, strike, type)."""
    try:
        with sqlite3.connect(cfg.SCRIP_MASTER_DB) as conn:
            row = conn.execute(
                "SELECT SEM_SMST_SECURITY_ID FROM scrip_master "
                "WHERE SEM_TRADING_SYMBOL LIKE ? AND SEM_OPTION_TYPE=? "
                "AND date(SEM_EXPIRY_DATE)=date(?) "
                "AND CAST(SEM_STRIKE_PRICE AS REAL)=? LIMIT 1",
                (underlying + "-%", opt_type, expiry_iso, float(strike))).fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except (sqlite3.OperationalError, ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Decision -> bookable signal
# --------------------------------------------------------------------------- #
def build_signal(decision, db_path: str, now: datetime | None = None) -> dict | None:
    """Turn one EMITTED Decision into a paper-book signal dict, or None if the
    contract / price inputs cannot be resolved without a broker call."""
    now = now or datetime.now()
    sid = str(decision.security_id)
    underlying = decision.symbol
    side = "CALL" if decision.direction == "CE" else "PUT"
    opt_type = "CE" if decision.direction == "CE" else "PE"

    em = expected_move.load(db_path, sid)
    spot, iv = em.get("spot"), em.get("atm_iv")
    if not spot or not iv:
        return None

    strike = _atm_strike(db_path, sid)
    if strike is None:
        return None

    today_iso = now.date().isoformat()
    contract = _nearest_expiry(underlying, opt_type, today_iso, cfg.PAPER_MIN_DTE)
    if contract is None:
        return None
    expiry_iso, lot_size = contract
    dte = (date.fromisoformat(expiry_iso) - now.date()).days

    # Estimated ATM premium, scaled to the contract's holding horizon.
    prem_pct = expected_move.est_atm_premium_pct(iv, days=max(1, dte))
    if not prem_pct:
        return None
    entry = round(spot * prem_pct / 100.0, 2)
    if entry <= 0:
        return None

    # SL: the existing percentage-of-premium distance, capped so the rupee
    # loss at stop-out never exceeds PAPER_MAX_LOSS_RUPEES regardless of
    # lot_size (see engine/config.py for why — large-lot names used to turn
    # a routine 30% stop into a five-figure loss).
    sl_points_pct = entry * cfg.PAPER_SL_PCT
    sl_points_cap = cfg.PAPER_MAX_LOSS_RUPEES / lot_size
    sl_points = min(sl_points_pct, sl_points_cap)
    sl = round(max(entry - sl_points, 0.01), 2)

    # Target: this stock's OWN 1-day expected ATM-premium move (real ATM
    # IV, not a flat multiplier) — reuses the same Brenner-Subrahmanyam
    # helper already used for entry pricing, just at a 1-day horizon
    # instead of the full DTE, since the trade's actual holding horizon is
    # intraday (EOD square-off), not to expiry.
    today_prem_pct = expected_move.est_atm_premium_pct(iv, days=1)
    today_prem_move = spot * (today_prem_pct or 0.0) / 100.0
    target_gain = max(cfg.PAPER_TARGET_CAPTURE_PCT * today_prem_move,
                      sl_points * cfg.PAPER_MIN_RR)
    target = round(entry + target_gain, 2)

    trig = decision.trigger
    trigger_kind = getattr(trig, "kind", None)
    # Sub-strategy tag so the dashboard/EOD summary/ad-hoc queries can tell
    # e.g. ORB-triggered convex trades apart from SONAR_BAND-triggered ones,
    # instead of everything landing under one flat "Convex" tag.
    sub_strategy = f"{cfg.PAPER_STRATEGY_TAG}-{trigger_kind}" if trigger_kind else cfg.PAPER_STRATEGY_TAG

    attribution = {
        "engine": True,
        "grade": decision.grade,
        "score": round(decision.score, 2),
        "formula_ver": decision.formula_ver,
        "direction": decision.direction,
        "trigger_kind": trigger_kind,
        "trigger_quality": getattr(trig, "quality", None),
        "entry_estimated": True,
        "est_premium_pct": prem_pct,
        "spot_at_signal": spot,
        "atm_iv": iv,
        "sl_cap_rupees": cfg.PAPER_MAX_LOSS_RUPEES,
        "target_basis_pct": today_prem_pct,
        "breakdown": decision.breakdown,
        "why": decision.why,
    }

    return {
        "symbol": underlying,
        "security_id": sid,  # underlying id — the paper-trade monitor re-quotes
                              # via get_current_option_premium(security_id, ...),
                              # which fetches the option CHAIN for this id and
                              # then looks up strike/side inside it.
        "option_security_id": _option_sid(underlying, opt_type, expiry_iso, strike),
        "exchange_segment": "NSE_FNO",
        "side": side,
        "strike": float(strike),
        "expiry": expiry_iso,
        "entry": entry,
        "sl": sl,
        "t1": target,
        "t2": target,          # single-target book: full exit at target
        "lot_size": lot_size,
        "score": round(decision.score, 2),
        "iv": iv,
        "dte": dte,
        "strategy": sub_strategy,
        "skip_risk_cap": True,   # measurement book: affordability (E5-1) is separate
        "factors_json": json.dumps(attribution),
    }


# --------------------------------------------------------------------------- #
# Cycle hook
# --------------------------------------------------------------------------- #
def book_emitted(result: dict, db_path: str, now: datetime | None = None,
                 book=None) -> dict:
    """Book the top EMITTED A+/A decisions from one cycle into the paper book.

    No-op (nothing written) when ENGINE_PAPER_MODE=off. Returns a summary
    {mode, booked:[symbols], skipped, cap_left} for the runner digest."""
    if cfg.PAPER_MODE == "off":
        return {"mode": "off", "booked": [], "skipped": 0, "cap_left": 0}

    now = now or datetime.now()
    from paper_trader import PaperTradeBook, book_signal  # lazy: heavy path only in paper mode

    book = book or PaperTradeBook()
    today_iso = now.date().isoformat()
    # Prefix match, not exact: individual trades are now tagged with a
    # trigger-kind suffix (Convex-ORB, Convex-SONAR_BAND, ...), so an exact
    # match against PAPER_STRATEGY_TAG would never count anything and the
    # daily cap would silently become a no-op.
    already = sum(1 for t in book.all_trades(today_iso)
                  if str(t.get("strategy", "")).startswith(cfg.PAPER_STRATEGY_TAG))
    cap_left = cfg.PAPER_MAX_TRADES - already
    if cap_left <= 0:
        return {"mode": cfg.PAPER_MODE, "booked": [], "skipped": 0, "cap_left": 0}

    booked, skipped = [], 0
    for d in result.get("emitted", []):  # already sorted by score desc
        if d.grade not in cfg.PAPER_GRADES:
            continue
        sig = build_signal(d, db_path, now)
        if sig is None:
            skipped += 1
            continue
        try:
            done = book_signal(book, sig, now=now)
        except Exception:  # noqa: BLE001 — a bad row must not kill the cycle
            logger.exception("convex paper: book_signal failed for %s", d.symbol)
            done = None
        if done:
            booked.append(f"{sig['symbol']} {sig['strike']:.0f}{sig['side'][0]} {d.grade}")
            if len(booked) >= cap_left:
                break
        else:
            skipped += 1

    if booked:
        logger.info("convex paper: booked %d [%s]", len(booked), ", ".join(booked))
    return {"mode": cfg.PAPER_MODE, "booked": booked, "skipped": skipped,
            "cap_left": cap_left - len(booked)}
