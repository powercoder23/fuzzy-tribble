"""
Shared IV snapshot storage layer.

Single source of truth for reading and writing iv_history.db.
Both iv_collector_service and all strategy services import from here.
Swap DB_PATH to a PostgreSQL URL in the future without touching strategy code.
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# Lives inside the shared Docker volume (/app/data) so iv-collector and every
# strategy service read/write the same SQLite file.
DB_PATH = str(Path("data") / "iv_history.db")

_CREATE_IV_HISTORY = """
CREATE TABLE IF NOT EXISTS iv_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id     TEXT    NOT NULL,
    symbol          TEXT,
    timestamp       DATETIME NOT NULL,
    spot_price      REAL,
    atm_strike      REAL,
    atm_iv          REAL,
    atm_call_iv     REAL,
    atm_put_iv      REAL,
    atm_call_oi     REAL,
    atm_put_oi      REAL,
    total_call_oi   REAL,
    total_put_oi    REAL,
    total_call_volume REAL,
    total_put_volume  REAL,
    max_oi_strike_call REAL,
    max_oi_strike_put  REAL,
    data_type       TEXT    NOT NULL DEFAULT 'daily',
    UNIQUE(security_id, timestamp, data_type)
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_iv_security_time
ON iv_history(security_id, timestamp)
"""

_OPTIONAL_COLUMNS = {
    "atm_call_oi":         "REAL",
    "atm_put_oi":          "REAL",
    "total_call_oi":       "REAL",
    "total_put_oi":        "REAL",
    "total_call_volume":   "REAL",
    "total_put_volume":    "REAL",
    "max_oi_strike_call":  "REAL",
    "max_oi_strike_put":   "REAL",
}


def init_db() -> None:
    """Create tables and indexes. Idempotent — safe to call on every startup."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(_CREATE_IV_HISTORY)
    cur.execute(_CREATE_INDEX)
    _ensure_optional_columns(cur)
    conn.commit()
    conn.close()
    logger.info("iv_store: DB initialised at %s", DB_PATH)


def _ensure_optional_columns(cursor) -> None:
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(iv_history)").fetchall()}
    for col, col_type in _OPTIONAL_COLUMNS.items():
        if col not in existing:
            cursor.execute(f"ALTER TABLE iv_history ADD COLUMN {col} {col_type}")


def save_snapshot(
    *,
    security_id: str,
    symbol: str,
    timestamp: datetime,
    spot_price: float,
    atm_strike: float,
    atm_iv: float,
    atm_call_iv: float = None,
    atm_put_iv: float = None,
    atm_call_oi: float = None,
    atm_put_oi: float = None,
    total_call_oi: float = None,
    total_put_oi: float = None,
    total_call_volume: float = None,
    total_put_volume: float = None,
    max_oi_strike_call: float = None,
    max_oi_strike_put: float = None,
    data_type: str = "daily",
) -> bool:
    """
    Insert one IV snapshot row. Silently skips duplicates (same security_id +
    timestamp + data_type already exists). Returns True if inserted.
    """
    if atm_iv is None or atm_iv <= 0 or atm_iv < 1 or atm_iv > 200:
        return False

    ts_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        _ensure_optional_columns(cur)
        cur.execute("""
            INSERT INTO iv_history (
                security_id, symbol, timestamp,
                spot_price, atm_strike,
                atm_iv, atm_call_iv, atm_put_iv,
                atm_call_oi, atm_put_oi,
                total_call_oi, total_put_oi,
                total_call_volume, total_put_volume,
                max_oi_strike_call, max_oi_strike_put,
                data_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(security_id, timestamp, data_type) DO NOTHING
        """, (
            str(security_id), symbol, ts_str,
            spot_price, atm_strike,
            atm_iv, atm_call_iv, atm_put_iv,
            atm_call_oi, atm_put_oi,
            total_call_oi, total_put_oi,
            total_call_volume, total_put_volume,
            max_oi_strike_call, max_oi_strike_put,
            data_type,
        ))
        inserted = cur.rowcount > 0
        conn.commit()
        return inserted
    except Exception:
        logger.exception("iv_store.save_snapshot failed | security_id=%s ts=%s", security_id, ts_str)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_latest_snapshot(security_id: str) -> dict:
    """Return the most recent intraday snapshot for a security. Empty dict if none."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT security_id, symbol, timestamp, spot_price, atm_strike,
                   atm_iv, atm_call_iv, atm_put_iv,
                   total_call_oi, total_put_oi,
                   total_call_volume, total_put_volume,
                   max_oi_strike_call, max_oi_strike_put
            FROM   iv_history
            WHERE  security_id = ?
              AND  data_type   = 'intraday'
            ORDER  BY timestamp DESC
            LIMIT  1
        """, (str(security_id),))
        row = cur.fetchone()
        conn.close()
        if not row:
            return {}
        cols = [
            "security_id", "symbol", "timestamp", "spot_price", "atm_strike",
            "atm_iv", "atm_call_oi", "atm_put_oi",
            "total_call_oi", "total_put_oi",
            "total_call_volume", "total_put_volume",
            "max_oi_strike_call", "max_oi_strike_put",
        ]
        return dict(zip(cols, row))
    except Exception:
        logger.exception("iv_store.get_latest_snapshot failed | security_id=%s", security_id)
        return {}


def get_iv_history(security_id: str, days: int = 252) -> list[float]:
    """Return a list of daily ATM IV values (oldest → newest) for IV Rank / Percentile."""
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql("""
            SELECT atm_iv FROM iv_history
            WHERE  security_id = ?
              AND  data_type   = 'daily'
              AND  atm_iv      BETWEEN 1.0 AND 200.0
            ORDER  BY timestamp ASC
        """, conn, params=(str(security_id),))
        conn.close()
        return df["atm_iv"].tail(days).tolist()
    except Exception:
        logger.exception("iv_store.get_iv_history failed | security_id=%s", security_id)
        return []


def get_bulk_latest_snapshots(security_ids: list) -> dict:
    """
    Return {security_id(int): {snapshot dict}} for all requested IDs in one query.
    Useful for AffordabilityFilter — replaces scanning the full table N times.
    """
    if not security_ids:
        return {}
    placeholders = ",".join("?" * len(security_ids))
    str_ids = [str(s) for s in security_ids]
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql(f"""
            SELECT security_id, symbol, spot_price, atm_iv, timestamp,
                   total_call_oi, total_put_oi, total_call_volume, total_put_volume
            FROM   iv_history
            WHERE  security_id IN ({placeholders})
              AND  data_type   = 'intraday'
              AND  timestamp   = (
                     SELECT MAX(i2.timestamp) FROM iv_history i2
                     WHERE  i2.security_id = iv_history.security_id
                       AND  i2.data_type   = 'intraday'
                   )
        """, conn, params=str_ids)
        conn.close()
        result = {}
        for _, row in df.iterrows():
            try:
                sid = int(row["security_id"])
                result[sid] = {
                    "security_id": sid,
                    "symbol":      row.get("symbol"),
                    "spot_price":  float(row["spot_price"]) if pd.notna(row["spot_price"]) else None,
                    "atm_iv":      float(row["atm_iv"])     if pd.notna(row["atm_iv"])     else None,
                    "total_call_oi": float(row["total_call_oi"]) if pd.notna(row["total_call_oi"]) else None,
                    "total_put_oi": float(row["total_put_oi"]) if pd.notna(row["total_put_oi"]) else None,
                    "total_call_volume": float(row["total_call_volume"]) if pd.notna(row["total_call_volume"]) else None,
                    "total_put_volume": float(row["total_put_volume"]) if pd.notna(row["total_put_volume"]) else None,
                    "timestamp":   row.get("timestamp"),
                }
            except Exception:
                continue
        return result
    except Exception:
        logger.exception("iv_store.get_bulk_latest_snapshots failed")
        return {}


def get_eod_stats(date_str: str = None) -> dict:
    """
    Return aggregated stats for the EOD Telegram summary.
    Queries today's collection counts and overall history depth.
    """
    if date_str is None:
        date_str = datetime.now().date().isoformat()
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()

        cur.execute("""
            SELECT COUNT(*), COUNT(DISTINCT security_id)
            FROM   iv_history
            WHERE  DATE(timestamp) = ? AND data_type = 'intraday'
        """, (date_str,))
        intraday_total, intraday_symbols = cur.fetchone()

        cur.execute("""
            SELECT COUNT(DISTINCT security_id)
            FROM   iv_history
            WHERE  DATE(timestamp) = ? AND data_type = 'daily'
        """, (date_str,))
        daily_symbols = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM iv_history WHERE data_type = 'intraday'")
        total_intraday_rows = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM iv_history WHERE data_type = 'daily'")
        total_daily_rows = cur.fetchone()[0]

        cur.execute("""
            SELECT security_id, symbol, COUNT(DISTINCT DATE(timestamp)) AS days
            FROM   iv_history
            WHERE  data_type = 'daily'
            GROUP  BY security_id
        """)
        hist_rows = cur.fetchall()
        conn.close()

        days_list  = [r[2] for r in hist_rows]
        avg_days   = round(sum(days_list) / len(days_list)) if days_list else 0
        min_days   = min(days_list) if days_list else 0
        min_symbol = next(
            (r[1] or str(r[0]) for r in hist_rows if r[2] == min_days), "—"
        )

        return {
            "date":                    date_str,
            "intraday_snapshots_today": intraday_total  or 0,
            "intraday_symbols_today":   intraday_symbols or 0,
            "daily_symbols_today":      daily_symbols    or 0,
            "total_intraday_rows":      total_intraday_rows or 0,
            "total_daily_rows":         total_daily_rows    or 0,
            "symbols_with_history":     len(hist_rows),
            "avg_history_days":         avg_days,
            "min_history_days":         min_days,
            "min_history_symbol":       min_symbol,
        }
    except Exception:
        logger.exception("iv_store.get_eod_stats failed")
        return {}


def daily_snapshot_exists_today(security_id: str) -> bool:
    """True if a daily snapshot has already been saved for today."""
    today = datetime.now().date().isoformat()
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT 1 FROM iv_history
            WHERE  security_id = ?
              AND  data_type   = 'daily'
              AND  DATE(timestamp) = ?
            LIMIT  1
        """, (str(security_id), today))
        exists = cur.fetchone() is not None
        conn.close()
        return exists
    except Exception:
        return False
