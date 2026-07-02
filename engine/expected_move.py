# -*- coding: utf-8 -*-
"""Expected move — the option buyer's structural viability check (V2).

From ATM IV the market itself tells you how far it expects the underlying to
travel: 1-day 1-sigma move = spot x (IV/100) / sqrt(252). A buyer paying theta
in a name whose expected move is under ~0.8% is structurally beaten before
entry — movement cannot outrun decay. This module computes that number from
the iv_history snapshots the collector already writes (zero API calls) so the
gate stack can reject dead-volatility names.

Also exposes est_atm_premium_pct (~0.4 x expected move for an ATM option),
surfaced in the decision breakdown so the cockpit can show "market expects
±X%, ATM costs ~Y%".
"""

from __future__ import annotations

import logging
import math
import sqlite3

logger = logging.getLogger(__name__)

TRADING_DAYS = 252.0


# --------------------------------------------------------------------------- #
# Pure math (unit-tested)
# --------------------------------------------------------------------------- #
def em_pct(atm_iv: float | None, days: float = 1.0) -> float | None:
    """Expected move over `days`, as % of spot. None when IV missing/invalid."""
    if atm_iv is None or atm_iv <= 0 or days <= 0:
        return None
    return round(float(atm_iv) * math.sqrt(days / TRADING_DAYS), 3)


def est_atm_premium_pct(atm_iv: float | None, days: float = 1.0) -> float | None:
    """Rough ATM option price as % of spot (~0.4 x 1-sigma move) — Brenner-
    Subrahmanyam approximation. Good enough for a structural sanity number."""
    em = em_pct(atm_iv, days)
    return round(0.4 * em, 3) if em is not None else None


# --------------------------------------------------------------------------- #
# Fail-open loader — latest intraday snapshot per security
# --------------------------------------------------------------------------- #
def load(db_path: str, security_id: str) -> dict:
    """{spot, atm_iv, em_pct, est_premium_pct} from the latest intraday
    iv_history row. {} when unavailable — the gate then simply doesn't apply."""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """SELECT spot_price, atm_iv FROM iv_history
                   WHERE security_id = ? AND data_type = 'intraday'
                     AND atm_iv > 0
                   ORDER BY timestamp DESC LIMIT 1""",
                (str(security_id),)).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row or row[1] is None:
        return {}
    spot, iv = (float(row[0]) if row[0] else None), float(row[1])
    return {"spot": spot, "atm_iv": iv,
            "em_pct": em_pct(iv), "est_premium_pct": est_atm_premium_pct(iv)}
