# -*- coding: utf-8 -*-
"""
strike_selector.py — feature-flagged bridge: after a strategy CONFIRMS a
direction on an underlying, ask the discount scanner for the cheapest /
best-value option on that underlying (in the confirmed direction) and trade
THAT strike, instead of the strategy's own blind ATM+offset pick.

Design goals
------------
* OFF by default. Controlled by the STRATEGY_STRIKE_VIA_DISCOUNT flag, resolved
  from the settings DB (Settings-page toggle) first, then the env var, then
  False. When off, callers keep their current behaviour.
* Zero new broker session. Callers pass their existing
  DiscountedPremiumScanner instance (B&B already holds one as
  `self._scanner_obj`); we only call its `scan_underlying`.
* Pure selection — this module never books, never sizes, never sets SL. The
  calling strategy keeps its OWN risk model (SL / target / lots) on the new
  premium. We only swap WHICH strike + entry premium is used.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def use_discount_strike() -> bool:
    """Master feature flag. Default OFF. Resolves from the settings DB (UI
    toggle) first, then the STRATEGY_STRIKE_VIA_DISCOUNT env var, then False —
    so a Settings-page toggle takes effect without a container restart."""
    try:
        import settings_store
        return settings_store.flag_bool("STRATEGY_STRIKE_VIA_DISCOUNT")
    except Exception:
        return os.getenv("STRATEGY_STRIKE_VIA_DISCOUNT", "false").strip().lower() in _TRUE


def _want_side(side) -> str:
    return "CALL" if str(side).upper() in ("CE", "CALL", "C") else "PUT"


def best_value_strike(scanner, symbol, security_id, side, expiry=None, *,
                      segment: str = "NSE_FNO", min_premium=None):
    """Return the discount scanner's best-value option on `symbol` in the
    confirmed direction, or None.

    Reuses the caller's DiscountedPremiumScanner (`scanner.scan_underlying`), so
    it applies the same IV-value scoring and liquidity gates the discount path
    uses. Returns the single highest-scored opportunity dict for the requested
    side (fields: strike, entry, iv, score, oi, volume, stop_loss, t1, t2, ...),
    or None if the scan is empty / errors (fail-open: caller keeps its own pick).
    """
    if scanner is None:
        return None
    want = _want_side(side)
    try:
        opts = scanner.scan_underlying(
            security_id=security_id,
            security_segment=segment,
            security_name=symbol,
            expiry=expiry,
        )
    except Exception:
        logger.exception("best_value_strike: scan_underlying failed for %s %s",
                         symbol, side)
        return None

    cands = [
        o for o in (opts or [])
        if str(o.get("type", "")).upper() == want
        and o.get("entry")
        and (min_premium is None or float(o.get("entry") or 0) >= min_premium)
    ]
    if not cands:
        logger.info("best_value_strike: no %s candidates for %s", want, symbol)
        return None

    cands.sort(key=lambda o: (o.get("score") or 0), reverse=True)
    best = cands[0]
    logger.info("best_value_strike: %s %s -> K%s @ %.2f (score %s) via discount",
                symbol, want, best.get("strike"), float(best.get("entry") or 0),
                best.get("score"))
    return best
