# -*- coding: utf-8 -*-
"""
iv_lookup.py — historical volatility inputs for the backtest's Black-Scholes
premium reconstruction. Reads iv_history.db read-only (the backtest engine
is not the sole-writer of that DB and never writes to it).

Two inputs:
  daily_atm_iv  — the market's own historical ATM IV for a symbol/date
                  (implied vol input to BS pricing).
  realized_vol  — historical realized (close-to-close) vol computed from
                  daily spot closes, the HV side of discount.py's IV-vs-HV
                  edge score.
"""

from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path

IV_DB = str(Path(os.path.dirname(__file__)).parent / "data" / "iv_history.db")
TRADING_DAYS = 252.0


def daily_atm_iv(symbol: str, on_date: str, tolerance_days: int = 10,
                 db_path: str = IV_DB) -> float | None:
    """Nearest daily ATM IV row on or before `on_date` (YYYY-MM-DD), within
    `tolerance_days`. None if nothing found — callers must treat that as
    'can't price this trade', not silently fall back to a guessed number."""
    if not Path(db_path).exists():
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT atm_iv FROM iv_history "
                "WHERE symbol=? AND data_type='daily' AND atm_iv BETWEEN 1 AND 200 "
                "  AND date(timestamp) <= date(?) "
                "  AND date(timestamp) >= date(?, ?) "
                "ORDER BY timestamp DESC LIMIT 1",
                (symbol.upper(), on_date, on_date, f"-{tolerance_days} days"),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return float(row[0]) if row and row[0] else None


def _daily_closes(symbol: str, on_date: str, lookback_days: int,
                  db_path: str) -> list[float]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT spot_price FROM iv_history AS h "
            "WHERE h.symbol=? AND h.data_type='daily' AND h.spot_price > 0 "
            "  AND date(h.timestamp) <= date(?) "
            "  AND h.rowid = ("
            "        SELECT MAX(i2.rowid) FROM iv_history i2 "
            "        WHERE i2.symbol=h.symbol AND i2.data_type='daily' "
            "          AND DATE(i2.timestamp) = DATE(h.timestamp)) "
            "ORDER BY h.timestamp DESC LIMIT ?",
            (symbol.upper(), on_date, lookback_days + 1),
        ).fetchall()
    return [r[0] for r in rows][::-1]  # oldest -> newest


def realized_vol(symbol: str, on_date: str, lookback_days: int = 20,
                 db_path: str = IV_DB) -> float | None:
    """Annualized close-to-close realized volatility (%) over the
    `lookback_days` trading days ending on/before `on_date`. None if there
    aren't enough closes to compute a meaningful stdev."""
    if not Path(db_path).exists():
        return None
    try:
        closes = _daily_closes(symbol, on_date, lookback_days, db_path)
    except sqlite3.OperationalError:
        return None
    if len(closes) < 5:
        return None
    log_rets = [math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]
    if len(log_rets) < 4:
        return None
    mean = sum(log_rets) / len(log_rets)
    var = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
    daily_sigma = math.sqrt(var)
    return round(daily_sigma * math.sqrt(TRADING_DAYS) * 100, 2)
