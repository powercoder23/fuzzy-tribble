# -*- coding: utf-8 -*-
"""
contracts.py — option contract resolution for the backtest engine, sourced
from data/complete.db's `instruments` table (every contract Upstox has ever
listed) instead of a live option chain, since backtesting has no "live chain
as of that historical date" to query.

Mirrors the strike-gap-detection idea already used live in
vol_expansion_strategy.select_atm_option(), just against listed instruments
rather than a live chain payload.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

COMPLETE_DB = str(Path(os.path.dirname(__file__)).parent / "data" / "complete.db")

_expiry_cache: dict[tuple[str, str], list[tuple[str, int]]] = {}
_strike_cache: dict[tuple[str, str, str], list[float]] = {}


def _epoch_ms_to_date(epoch_ms) -> str | None:
    try:
        return datetime.utcfromtimestamp(int(epoch_ms) / 1000).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def list_expiries(underlying: str, opt_type: str = "CE",
                  db_path: str = COMPLETE_DB) -> list[tuple[str, int]]:
    """[(expiry_iso, lot_size), ...] for `underlying`, de-duplicated, sorted."""
    key = (underlying.upper(), opt_type)
    if key in _expiry_cache:
        return _expiry_cache[key]
    if not Path(db_path).exists():
        return []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT expiry, lot_size FROM instruments "
            "WHERE underlying_symbol=? AND instrument_type=?",
            (underlying.upper(), opt_type),
        ).fetchall()
    out = []
    for exp_raw, lot in rows:
        exp_iso = _epoch_ms_to_date(exp_raw)
        if exp_iso and lot:
            out.append((exp_iso, int(lot)))
    out.sort(key=lambda x: x[0])
    _expiry_cache[key] = out
    return out


def nearest_expiry(underlying: str, on_date: str, min_dte: int = 0,
                   opt_type: str = "CE") -> tuple[str, int] | None:
    """(expiry_iso, lot_size) for the nearest listed expiry >= on_date+min_dte."""
    from datetime import timedelta
    threshold = (date.fromisoformat(on_date) + timedelta(days=min_dte)).isoformat()
    for exp_iso, lot in list_expiries(underlying, opt_type):
        if exp_iso >= threshold:
            return exp_iso, lot
    return None


def strikes_for_expiry(underlying: str, expiry_iso: str, opt_type: str = "CE",
                       db_path: str = COMPLETE_DB) -> list[float]:
    key = (underlying.upper(), expiry_iso, opt_type)
    if key in _strike_cache:
        return _strike_cache[key]
    if not Path(db_path).exists():
        return []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT strike_price, expiry FROM instruments "
            "WHERE underlying_symbol=? AND instrument_type=?",
            (underlying.upper(), opt_type),
        ).fetchall()
    strikes = sorted({
        float(strike) for strike, exp_raw in rows
        if _epoch_ms_to_date(exp_raw) == expiry_iso and strike
    })
    _strike_cache[key] = strikes
    return strikes


def atm_strike(underlying: str, expiry_iso: str, spot: float, side: str,
               offset: int = 0) -> float | None:
    """ATM(+offset strikes toward `side`) strike from the actual listed grid
    for this underlying+expiry — avoids assuming a strike gap."""
    strikes = strikes_for_expiry(underlying, expiry_iso, side if side in ("CE", "PE") else "CE")
    if not strikes:
        return None
    atm = min(strikes, key=lambda s: abs(s - spot))
    if offset == 0:
        return atm
    idx = strikes.index(atm)
    step = offset if side == "CE" else -offset
    idx2 = min(max(idx + step, 0), len(strikes) - 1)
    return strikes[idx2]
