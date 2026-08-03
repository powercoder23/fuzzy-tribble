# -*- coding: utf-8 -*-
"""
candle_store.py — local SQLite cache of historical underlying candles.

Owns data/backtest_candles.db. Two tables:
  candles      — one row per (security_id, interval_min, ts).
  fetch_log    — one row per (security_id, interval_min, date) attempted
                 against Upstox, so a genuinely-empty day (holiday, no
                 listing yet, Upstox history window exceeded) isn't
                 re-fetched on every subsequent backtest run.

Zero business logic here — candle_fetcher.py decides what to fetch and when;
this module just persists and serves what's asked of it.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = str(Path(os.path.dirname(__file__)).parent / "data" / "backtest_candles.db")


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                security_id  TEXT NOT NULL,
                interval_min INTEGER NOT NULL,
                ts           TEXT NOT NULL,
                open         REAL,
                high         REAL,
                low          REAL,
                close        REAL,
                volume       REAL,
                PRIMARY KEY (security_id, interval_min, ts)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fetch_log (
                security_id  TEXT NOT NULL,
                interval_min INTEGER NOT NULL,
                date         TEXT NOT NULL,
                status       TEXT NOT NULL,
                fetched_at   TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (security_id, interval_min, date)
            )
        """)


def get_cached(security_id: str, interval_min: int, start_date: str, end_date: str,
              db_path: str = DB_PATH) -> pd.DataFrame:
    """Cached candles in [start_date, end_date] (dates inclusive, 'YYYY-MM-DD')."""
    init_db(db_path)
    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE security_id=? AND interval_min=? AND date(ts) BETWEEN ? AND ? "
            "ORDER BY ts ASC",
            conn, params=(str(security_id), interval_min, start_date, end_date),
        )
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"])
    return df


def upsert_candles(security_id: str, interval_min: int, df: pd.DataFrame,
                   db_path: str = DB_PATH) -> int:
    """Insert-or-replace candle rows. `df` needs ts/open/high/low/close/volume."""
    if df is None or df.empty:
        return 0
    init_db(db_path)
    rows = [
        (str(security_id), interval_min, str(r.ts), float(r.open), float(r.high),
         float(r.low), float(r.close), float(r.volume or 0))
        for r in df.itertuples(index=False)
    ]
    with _connect(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO candles "
            "(security_id, interval_min, ts, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)", rows,
        )
    return len(rows)


def mark_fetch_attempt(security_id: str, interval_min: int, date: str, status: str,
                       db_path: str = DB_PATH) -> None:
    """status: 'ok' (candles found) or 'empty' (asked Upstox, got nothing)."""
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log (security_id, interval_min, date, status) "
            "VALUES (?,?,?,?)", (str(security_id), interval_min, date, status),
        )


def attempted_dates(security_id: str, interval_min: int, start_date: str, end_date: str,
                    db_path: str = DB_PATH) -> set[str]:
    """Dates already attempted (regardless of outcome) in range — skip these on refetch."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT date FROM fetch_log WHERE security_id=? AND interval_min=? "
            "AND date BETWEEN ? AND ?",
            (str(security_id), interval_min, start_date, end_date),
        ).fetchall()
    return {r[0] for r in rows}
