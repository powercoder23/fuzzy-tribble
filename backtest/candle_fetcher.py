# -*- coding: utf-8 -*-
"""
candle_fetcher.py — get_candles() is the one function the rest of the
backtest engine calls for historical underlying OHLCV. It never talks to
Upstox directly if the answer is already local:

  1. backtest_candles.db cache (candle_store.py)
  2. candles_5m table in iv_history.db (already collected live by the
     convex-engine collector since 2026-07-03) — only for interval_min==5,
     copied into the cache on read so it becomes a superset over time
  3. Upstox historical_intraday_data() (new adapter method) for whatever
     date range is still missing, rate-limited; failures/empties are
     recorded in fetch_log so reruns don't hammer the API for dates that
     genuinely have no data (holidays, pre-listing, outside Upstox's
     history window).

upstox_client is a slow import (25-42s cold) and network calls are its own
cost — both are deferred until a live fetch is actually needed.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from backtest import candle_store

logger = logging.getLogger(__name__)

IV_DB = str(Path(os.path.dirname(__file__)).parent / "data" / "iv_history.db")

_SEGMENT_TO_INSTRUMENT_TYPE = {
    "IDX_I":   "INDEX",
    "NSE_EQ":  "EQUITY",
    "NSE_FNO": "EQUITY",
}

_adapter = None  # lazy singleton — first live fetch pays the import/token cost


def _get_adapter():
    global _adapter
    if _adapter is None:
        from upstox_adapter import UpstoxDhanAdapter
        from upstox_token_manager import load_upstox_token
        _adapter = UpstoxDhanAdapter(load_upstox_token())
    return _adapter


def _trading_days(start_date: str, end_date: str) -> list[str]:
    """Weekdays in [start_date, end_date]. Not NSE-holiday-aware — Upstox
    returning nothing for a holiday is handled the same as any other
    genuinely-empty day via fetch_log."""
    d0 = date.fromisoformat(start_date)
    d1 = date.fromisoformat(end_date)
    out = []
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _resample(df: pd.DataFrame, interval_min: int) -> pd.DataFrame:
    """1-minute bars -> interval_min bars. 09:15 (NSE open) falls exactly on
    every 5/15-min grid line from midnight (555 min = 37*15 = 111*5), so a
    plain resample (no origin/offset juggling) lands bars on the boundaries
    the strategies expect (09:15, 09:30, 09:45, ...)."""
    if df.empty or interval_min <= 1:
        return df
    agg = (
        df.set_index("ts")
        .resample(f"{interval_min}min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open"])
        .reset_index()
    )
    return agg


def _seed_from_candles_5m(security_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    if not Path(IV_DB).exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(IV_DB) as conn:
            df = pd.read_sql_query(
                "SELECT ts, open, high, low, close, volume FROM candles_5m "
                "WHERE security_id=? AND date(ts) BETWEEN ? AND ? ORDER BY ts ASC",
                conn, params=(str(security_id), start_date, end_date),
            )
    except sqlite3.OperationalError:
        return pd.DataFrame()
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"])
    return df


def get_candles(security_id: str, symbol: str, exchange_segment: str,
                interval_min: int, start_date: str, end_date: str,
                rate_limit_sec: float = 0.3) -> pd.DataFrame:
    """Historical OHLCV candles for one instrument over [start_date, end_date].

    Returns a DataFrame with columns ts/open/high/low/close/volume, sorted
    ascending. Empty DataFrame if nothing could be found or fetched.
    """
    security_id = str(security_id)

    if interval_min == 5:
        seed = _seed_from_candles_5m(security_id, start_date, end_date)
        if not seed.empty:
            candle_store.upsert_candles(security_id, interval_min, seed)

    cached = candle_store.get_cached(security_id, interval_min, start_date, end_date)
    have_dates = set(cached["ts"].dt.date.astype(str)) if not cached.empty else set()

    needed = [d for d in _trading_days(start_date, end_date) if d not in have_dates]
    if not needed:
        return cached

    already_tried = candle_store.attempted_dates(security_id, interval_min, start_date, end_date)
    missing = [d for d in needed if d not in already_tried]
    if not missing:
        return cached  # everything left was already tried and came back empty

    # Fetch the whole missing span in one ranged call (Upstox historical
    # endpoint takes from/to, not per-day) — cheaper than one call per day.
    # Always ask for 1-minute bars and resample locally to interval_min:
    # Upstox's v3 minutes-unit endpoint rejected interval=15 with a (mis-
    # labeled) "UDAPI1148 Invalid date range" error — 1-minute is the one
    # granularity every broker's minute-history endpoint is guaranteed to
    # support, so this sidesteps guessing which multiples are accepted.
    fetch_start, fetch_end = missing[0], missing[-1]
    resp = None
    try:
        adapter = _get_adapter()
        resp = adapter.historical_intraday_data(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=_SEGMENT_TO_INSTRUMENT_TYPE.get(exchange_segment, "EQUITY"),
            from_date=fetch_start,
            to_date=fetch_end,
            interval=1,
        )
    except Exception:
        # Could not even make the call (missing dep, network error, bad
        # token, ...) — an environment/transient problem, NOT evidence the
        # data doesn't exist. Deliberately do not touch fetch_log here: a
        # ModuleNotFoundError on a dev box (or a flaky network blip) must
        # never get permanently cached as "no data for this day," or fixing
        # the environment later wouldn't help without a manual cache purge.
        logger.exception("historical_intraday_data failed for %s (%s)", symbol, security_id)
    finally:
        time.sleep(rate_limit_sec)

    if resp is None:
        return cached  # couldn't ask Upstox at all this run — try again next time

    fetched = pd.DataFrame()
    if resp.get("status") == "success":
        data = resp.get("data") or {}
        if isinstance(data, dict) and data.get("timestamp"):
            # Upstox timestamps are ISO strings with a +05:30 offset. Store
            # everything in the cache as naive Asia/Kolkata local time so it
            # matches the (already-naive-local) candles_5m seed data — mixed
            # tz-aware/naive strings would silently double-key the same bar.
            ts = pd.to_datetime(data["timestamp"], utc=True).tz_convert("Asia/Kolkata").tz_localize(None)
            fetched = pd.DataFrame({
                "ts":     ts,
                "open":   data.get("open", []),
                "high":   data.get("high", []),
                "low":    data.get("low", []),
                "close":  data.get("close", []),
                "volume": data.get("volume", []),
            })
            fetched = _resample(fetched, interval_min)
    else:
        # Upstox answered, but with a failure status (bad instrument key,
        # rate limit, API error, ...) — still not a confirmed "no data"
        # verdict, so leave fetch_log untouched here too and retry later.
        logger.warning("historical_intraday_data returned failure for %s (%s): %s",
                       symbol, security_id, resp.get("remarks"))
        return cached

    if not fetched.empty:
        candle_store.upsert_candles(security_id, interval_min, fetched)

    got_dates = set(fetched["ts"].dt.date.astype(str)) if not fetched.empty else set()
    for d in missing:
        candle_store.mark_fetch_attempt(
            security_id, interval_min, d, "ok" if d in got_dates else "empty")

    return candle_store.get_cached(security_id, interval_min, start_date, end_date)
