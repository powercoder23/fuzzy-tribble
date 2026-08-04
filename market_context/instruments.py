# -*- coding: utf-8 -*-
"""
market_context/instruments.py — resolve Upstox instrument keys.

Reads the local Upstox instrument master (``data/complete.db``, table
``instruments``) that the platform already refreshes daily. Zero broker calls.

Instrument-key formats confirmed against the live master (2026-08-03):

    index    NSE_INDEX|Nifty 50          NSE_INDEX|India VIX
    equity   NSE_EQ|INE009A01021         (ISIN-keyed)
    futures  NSE_FO|58072                (numeric Upstox token)

Note on ``instruments.expiry``: it is epoch **milliseconds**, not seconds and
not a date string. Getting this wrong silently yields 1970 expiries, which
would sort the futures chain backwards and pick the wrong contract.

Everything here fails soft: a missing master, a missing symbol, or a renamed
index returns None/[] rather than raising. The service degrades to fewer
instruments; it never dies at startup because one sector index was renamed.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
INSTRUMENT_DB = str(DATA_DIR / "complete.db")
IV_DB = str(DATA_DIR / "iv_history.db")

# --------------------------------------------------------------------------- #
# Tier 1 — the regime engine cannot run without these.
# --------------------------------------------------------------------------- #
NIFTY_INDEX = "NSE_INDEX|Nifty 50"
BANKNIFTY_INDEX = "NSE_INDEX|Nifty Bank"
INDIA_VIX = "NSE_INDEX|India VIX"

#: India VIX is the single highest-impact addition in Phase 1. Until now the
#: platform only had EOD VIX (collectors/vix_collector.py -> vix_daily), so
#: engine/regime.py's `VIX_RED = 22` no-trade gate was evaluating YESTERDAY's
#: close and could not fire on a day that spiked intraday.
TIER1_INDICES = (NIFTY_INDEX, BANKNIFTY_INDEX, INDIA_VIX)

#: Underlyings whose near + next futures we always want (basis + positioning).
TIER1_FUTURES_UNDERLYINGS = ("NIFTY", "BANKNIFTY")

# --------------------------------------------------------------------------- #
# Tier 2 — sector indices for relative strength + dispersion.
# Exact keys verified against the live master; any that disappear are dropped
# at resolve time rather than breaking startup.
# --------------------------------------------------------------------------- #
SECTOR_INDICES = (
    "NSE_INDEX|Nifty IT",
    "NSE_INDEX|Nifty Auto",
    "NSE_INDEX|Nifty Pharma",
    "NSE_INDEX|Nifty FMCG",
    "NSE_INDEX|Nifty Metal",
    "NSE_INDEX|Nifty Realty",
    "NSE_INDEX|Nifty Energy",
    "NSE_INDEX|Nifty Fin Service",
    "NSE_INDEX|Nifty PSU Bank",
    "NSE_INDEX|Nifty Pvt Bank",
    "NSE_INDEX|Nifty Media",
    "NSE_INDEX|Nifty Infra",
    "NSE_INDEX|NIFTY OIL AND GAS",
    "NSE_INDEX|NIFTY HEALTHCARE",
)


@dataclass(frozen=True)
class Instrument:
    """One resolved, subscribable instrument."""
    instrument_key: str
    symbol: str
    kind: str                     # index | vix | sector_index | futures | equity
    underlying: str | None = None
    expiry: str | None = None     # ISO date, futures only
    lot_size: int | None = None
    rank_metric: float | None = None

    def as_row(self, tier: int, mode: str) -> tuple:
        """Tuple shaped for the mc_instruments INSERT."""
        return (self.instrument_key, self.symbol, self.kind, self.underlying,
                self.expiry, tier, mode, self.lot_size, self.rank_metric)


# --------------------------------------------------------------------------- #
# Instrument master access
# --------------------------------------------------------------------------- #
def _connect(db_path: str | None = None) -> sqlite3.Connection | None:
    path = db_path or INSTRUMENT_DB
    if not os.path.exists(path):
        logger.warning("instrument master missing at %s", path)
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        logger.warning("could not open instrument master at %s", path, exc_info=True)
        return None


def _epoch_ms_to_date(value) -> str | None:
    """complete.db stores expiry as epoch MILLISECONDS."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def resolve_indices(keys, db_path: str | None = None) -> list[Instrument]:
    """Keep only index keys that actually exist in the master.

    Index names change (e.g. 'Nifty Consumer Durables' is absent while
    'Nifty Consumption' exists), so a hardcoded list must be validated rather
    than trusted.
    """
    conn = _connect(db_path)
    if conn is None:
        return []
    out: list[Instrument] = []
    try:
        for key in keys:
            row = conn.execute(
                "SELECT instrument_key, name, trading_symbol FROM instruments "
                "WHERE instrument_key = ? AND segment = 'NSE_INDEX' LIMIT 1",
                (key,),
            ).fetchone()
            if not row:
                logger.warning("index key not found in master, skipping: %s", key)
                continue
            kind = "vix" if "VIX" in row["instrument_key"].upper() else (
                "index" if row["instrument_key"] in TIER1_INDICES else "sector_index")
            out.append(Instrument(
                instrument_key=row["instrument_key"],
                symbol=(row["trading_symbol"] or row["name"] or "").strip(),
                kind=kind,
            ))
    except sqlite3.Error:
        logger.warning("resolve_indices failed", exc_info=True)
    finally:
        conn.close()
    return out


def futures_chain(underlying: str, count: int = 2, on_date=None,
                  db_path: str | None = None) -> list[Instrument]:
    """The nearest `count` unexpired futures for `underlying`, nearest first.

    Returning near AND next is what makes rollover measurable (OI migrating
    from near to far) and gives a second basis point for the term structure.
    """
    conn = _connect(db_path)
    if conn is None:
        return []
    today = (on_date or datetime.now().date())
    cutoff_ms = int(datetime(today.year, today.month, today.day).timestamp() * 1000)
    out: list[Instrument] = []
    try:
        rows = conn.execute(
            "SELECT instrument_key, trading_symbol, expiry, lot_size, underlying_symbol "
            "FROM instruments "
            "WHERE segment = 'NSE_FO' AND instrument_type = 'FUT' "
            "  AND underlying_symbol = ? AND expiry >= ? "
            "ORDER BY expiry ASC LIMIT ?",
            (underlying.upper(), cutoff_ms, int(count)),
        ).fetchall()
        for row in rows:
            out.append(Instrument(
                instrument_key=row["instrument_key"],
                symbol=(row["trading_symbol"] or "").strip(),
                kind="futures",
                underlying=row["underlying_symbol"],
                expiry=_epoch_ms_to_date(row["expiry"]),
                lot_size=int(row["lot_size"]) if row["lot_size"] else None,
            ))
    except sqlite3.Error:
        logger.warning("futures_chain(%s) failed", underlying, exc_info=True)
    finally:
        conn.close()
    if not out:
        logger.warning("no unexpired futures found for %s", underlying)
    return out


def equity_keys(symbols, db_path: str | None = None) -> list[Instrument]:
    """NSE_EQ|<ISIN> for each symbol, preserving the input order.

    Order matters: callers pass symbols already ranked by liquidity, and the
    subscription budget truncates from the end.
    """
    conn = _connect(db_path)
    if conn is None:
        return []
    out: list[Instrument] = []
    try:
        for symbol in symbols:
            row = conn.execute(
                "SELECT instrument_key, trading_symbol FROM instruments "
                "WHERE segment = 'NSE_EQ' AND instrument_type = 'EQ' "
                "  AND trading_symbol = ? LIMIT 1",
                (str(symbol).strip().upper(),),
            ).fetchone()
            if not row:
                logger.debug("no NSE_EQ instrument for %s", symbol)
                continue
            out.append(Instrument(
                instrument_key=row["instrument_key"],
                symbol=row["trading_symbol"],
                kind="equity",
            ))
    except sqlite3.Error:
        logger.warning("equity_keys failed", exc_info=True)
    finally:
        conn.close()
    return out


def stock_futures(symbols, on_date=None, db_path: str | None = None) -> list[Instrument]:
    """Near-month future for each symbol, input order preserved."""
    out: list[Instrument] = []
    for symbol in symbols:
        chain = futures_chain(symbol, count=1, on_date=on_date, db_path=db_path)
        if chain:
            out.append(chain[0])
    return out


# --------------------------------------------------------------------------- #
# Liquidity ranking — reuses the metric discount.py already uses for its
# top-120 trim, so tier-3/4 membership is consistent with the rest of the
# platform rather than a second, divergent definition of "liquid".
# --------------------------------------------------------------------------- #
#: Index names that appear in iv_history as if they were stocks. They must be
#: excluded from the BREADTH universe: they are already tier-1 instruments,
#: they are not constituents, and (being the highest-turnover names in the
#: table) they otherwise consume the top ranking slots silently.
_NON_STOCK_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "SENSEX", "BANKEX", "INDIA VIX", "INDIAVIX",
}

#: turnover -> price x option volume (rupee-weighted)
#: shares   -> OI x option volume (share-count weighted; discount.py's metric)
#:
#: Default is `turnover` and that is a DELIBERATE divergence from
#: discount.py's top-120 trim. Measured on live data (2026-08-04), the
#: share-count metric ranks IDEA (Rs 13), YESBANK (Rs 23) and SUZLON (Rs 49)
#: above RELIANCE and INFY, because a low-priced stock trades far more SHARES
#: for the same rupee interest. That is a reasonable universe for "which
#: option chains are worth scanning" (discount.py's question) but a poor one
#: for market breadth: it produces a speculative, low-price sample and calls
#: it the market. Rupee turnover yields M&M / RELIANCE / SBIN / INFY /
#: HDFCBANK instead.
LIQUIDITY_METRIC = os.getenv("MC_LIQUIDITY_METRIC", "turnover").strip().lower()

_METRIC_SQL = {
    "turnover": ("MAX(spot_price) * "
                 "(COALESCE(total_call_volume,0) + COALESCE(total_put_volume,0))"),
    "shares": ("(COALESCE(total_call_oi,0) + COALESCE(total_put_oi,0)) * "
               "(COALESCE(total_call_volume,0) + COALESCE(total_put_volume,0))"),
}


def liquid_symbols(limit: int, iv_db: str | None = None,
                   lookback_days: int = 5, metric: str | None = None) -> list[str]:
    """F&O STOCK symbols ranked by liquidity, most liquid first.

    Reads iv_history.db only — zero broker calls. Returns [] on any failure
    (caller then subscribes tiers 1-2 only) rather than raising.

    NOTE on coverage: iv_history carries roughly 119 of the ~208 F&O names
    with usable intraday rows, so this can only ever rank what the collector
    actually sees. Symbols missing from iv_history are invisible here — the
    absence is silent, which is why mc_breadth records universe_size.
    """
    path = iv_db or IV_DB
    if limit <= 0 or not os.path.exists(path):
        return []
    metric_sql = _METRIC_SQL.get((metric or LIQUIDITY_METRIC),
                                 _METRIC_SQL["turnover"])
    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    # Over-fetch, then drop indices, so excluding them does not shrink the result.
    fetch = int(limit) + len(_NON_STOCK_SYMBOLS)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            rows = conn.execute(
                f"""
                SELECT symbol, {metric_sql} AS metric
                FROM iv_history
                WHERE data_type = 'intraday'
                  AND timestamp >= ?
                  AND symbol IS NOT NULL
                GROUP BY symbol
                ORDER BY metric DESC
                LIMIT ?
                """,
                (since, fetch),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("liquid_symbols failed; tier 3/4 will be empty", exc_info=True)
        return []

    out = [r[0] for r in rows
           if r[0] and str(r[0]).strip().upper() not in _NON_STOCK_SYMBOLS]
    return out[:int(limit)]
