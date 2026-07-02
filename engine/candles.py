# -*- coding: utf-8 -*-
"""Candle store — persisted 5-min candles + yesterday levels (V2 P2).

The sonar service already fetches 5-min candles for every F&O name each scan
(via DataProvider). This module gives those fetches a durable home so the
engine's triggers (ORB / VWAP / break-retest) can run zero-API, and provides
pure aggregation to 15-min.

Sole-writer: the sonar service writes candles_5m (through save_candles);
the engine only reads. Yesterday's high/low comes from delivery_daily
(bhav collector), not from a broker call.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime

logger = logging.getLogger(__name__)

TABLE = "candles_5m"


def _connect(db_path: str):
    try:
        from collectors import iv_store
        return iv_store.connect(db_path)
    except Exception:  # noqa: BLE001 — tests / bare envs
        return sqlite3.connect(db_path)


def ensure_table(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                security_id TEXT NOT NULL,
                symbol      TEXT,
                ts          DATETIME NOT NULL,   -- candle START time
                open REAL, high REAL, low REAL, close REAL,
                volume REAL,
                PRIMARY KEY (security_id, ts)
            )""")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_c5_sid_ts ON {TABLE}(security_id, ts)")
        conn.commit()


def save_candles(db_path: str, security_id: str, symbol: str, df) -> int:
    """Upsert a DataFrame of 5-min candles (needs timestamp/open/high/low/close/volume
    columns; timestamp column may be named 'timestamp' or 'ts' or be the index)."""
    if df is None or getattr(df, "empty", True):
        return 0
    ensure_table(db_path)
    d = df.reset_index()
    cols = {c.lower(): c for c in d.columns}
    tcol = cols.get("timestamp") or cols.get("ts") or cols.get("datetime") or cols.get("index")
    need = ("open", "high", "low", "close")
    if tcol is None or any(k not in cols for k in need):
        logger.debug("candles: unrecognized frame shape %s", list(d.columns))
        return 0
    vcol = cols.get("volume")
    n = 0
    with _connect(db_path) as conn:
        for _, r in d.iterrows():
            ts = str(r[tcol])[:19]
            cur = conn.execute(
                f"""INSERT OR IGNORE INTO {TABLE}
                    (security_id, symbol, ts, open, high, low, close, volume)
                    VALUES (?,?,?,?,?,?,?,?)""",
                (str(security_id), symbol, ts,
                 float(r[cols["open"]]), float(r[cols["high"]]),
                 float(r[cols["low"]]), float(r[cols["close"]]),
                 float(r[vcol]) if vcol is not None else 0.0))
            n += cur.rowcount
        conn.commit()
    return n


def load_today(db_path: str, security_id: str, day: str | None = None) -> list[dict]:
    """Today's 5-min candles, ascending. [] if table missing / no data."""
    day = day or date.today().isoformat()
    try:
        with _connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""SELECT ts, open, high, low, close, volume FROM {TABLE}
                    WHERE security_id = ? AND ts >= ? AND ts < ?
                    ORDER BY ts ASC""",
                (str(security_id), f"{day} 00:00:00", f"{day} 23:59:59")).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def yesterday_levels(db_path: str, symbol: str, day: str | None = None):
    """(prev_high, prev_low) from delivery_daily (bhav). (None, None) if absent."""
    day = day or date.today().isoformat()
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """SELECT high, low FROM delivery_daily
                   WHERE symbol = ? AND date < ?
                   ORDER BY date DESC LIMIT 1""",
                (symbol, day)).fetchone()
        return (float(row[0]), float(row[1])) if row and row[0] is not None else (None, None)
    except sqlite3.OperationalError:
        return (None, None)


# --------------------------------------------------------------------------- #
# Pure aggregation (unit-tested)
# --------------------------------------------------------------------------- #
def aggregate(candles: list[dict], group: int) -> list[dict]:
    """Aggregate consecutive candles into groups of `group` (5m x3 -> 15m).
    Only complete groups are returned — a partial last group is dropped, so
    triggers always evaluate COMPLETED candles."""
    out = []
    for i in range(0, len(candles) - len(candles) % group, group):
        chunk = candles[i:i + group]
        out.append({
            "ts": chunk[0]["ts"],
            "open": chunk[0]["open"],
            "high": max(c["high"] for c in chunk),
            "low": min(c["low"] for c in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(c.get("volume") or 0.0 for c in chunk),
        })
    return out


def session_vwap(candles: list[dict]) -> list[float]:
    """Cumulative VWAP per candle: sum(typical x vol) / sum(vol). Falls back to
    typical price when volume is absent."""
    vwap, pv, vv = [], 0.0, 0.0
    for c in candles:
        typ = (c["high"] + c["low"] + c["close"]) / 3.0
        v = c.get("volume") or 0.0
        pv += typ * v
        vv += v
        vwap.append(pv / vv if vv > 0 else typ)
    return vwap
