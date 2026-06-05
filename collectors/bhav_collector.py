"""
Bhavcopy collector — downloads NSE's full security-wise bhavdata CSV
(sec_bhavdata_full) and persists delivery + OHLC data for F&O symbols into
the delivery_daily table.

NOTE: the plain BhavCopy_NSE_CM file has OHLC/volume but NO delivery columns —
delivery qty/% only live in sec_bhavdata_full_DDMMYYYY.csv, which is a plain
(non-zip) CSV. Columns: SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE,
HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY,
TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER (all space-padded).
"""

import io
import logging
import sqlite3
import time
from datetime import date as _Date

import pandas as pd
import requests

from collectors.notify import send_telegram

logger = logging.getLogger(__name__)


class BhavDataNotReady(Exception):
    """Raised when NSE serves the price bhavcopy but has not yet appended the
    delivery columns (DELIV_QTY/DELIV_PER). This is a transient, expected
    condition shortly after market close — the caller should retry later
    rather than treat it as a hard failure."""


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS delivery_daily (
    date      TEXT,
    symbol    TEXT,
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    volume    INTEGER,
    deliv_qty INTEGER,
    deliv_pct REAL,
    PRIMARY KEY (date, symbol)
)
"""

_BHAV_URL = (
    "https://nsearchives.nseindia.com/products/content/"
    "sec_bhavdata_full_{date}.csv"   # {date} = DDMMYYYY
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer":         "https://www.nseindia.com",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


class BhavCollector:

    def __init__(self, config):
        self._db_path = str(config.DATA_DIR / "iv_history.db")

    def _init_table(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(_CREATE_TABLE)
            conn.commit()
        finally:
            conn.close()

    def _make_session(self) -> requests.Session:
        # NSE archive endpoints 403 (or serve an HTML block page) for requests
        # that arrive without cookies — prime the session via the home page
        # first, same as the deals/vix collectors.
        sess = requests.Session()
        sess.headers.update(_HEADERS)
        sess.get("https://www.nseindia.com", timeout=15)
        time.sleep(2)
        return sess

    def fetch(self, trade_date: _Date) -> pd.DataFrame:
        url = _BHAV_URL.format(date=trade_date.strftime("%d%m%Y"))
        logger.info("BhavCollector.fetch: GET %s", url)

        sess = self._make_session()
        df = None
        last_err = None
        for attempt in range(3):
            try:
                resp = sess.get(url, timeout=30)
                resp.raise_for_status()
                # A blocked request can return HTTP 200 with an HTML body
                # instead of the CSV. Detect that explicitly so the error names
                # the real cause instead of a downstream parse failure.
                ctype = resp.headers.get("content-type", "")
                head  = resp.content[:200].lstrip()
                if "csv" not in ctype.lower() and (
                    head[:1] == b"<" or b"<html" in head.lower()
                ):
                    raise ValueError(
                        f"NSE returned a non-CSV response "
                        f"(content-type={ctype!r}, first bytes={resp.content[:120]!r}) "
                        f"— likely a block / holiday / not-ready page"
                    )
                df = pd.read_csv(io.BytesIO(resp.content))
                break
            except Exception as exc:
                last_err = exc
                logger.warning("BhavCollector.fetch: attempt %d/3 failed (%s)",
                               attempt + 1, exc)
                if attempt < 2:
                    time.sleep(3)
        if df is None:
            raise last_err

        df.columns = df.columns.str.strip()
        df = df.rename(columns={
            "SYMBOL":       "symbol",
            "SERIES":       "series",
            "OPEN_PRICE":   "open",
            "HIGH_PRICE":   "high",
            "LOW_PRICE":    "low",
            "CLOSE_PRICE":  "close",
            "TTL_TRD_QNTY": "volume",
            "DELIV_QTY":    "deliv_qty",
            "DELIV_PER":    "deliv_pct",
        })

        cols = set(df.columns)
        core_required = {"symbol", "series", "open", "high", "low", "close", "volume"}
        missing_core  = core_required - cols
        if missing_core:
            # Missing OHLC/volume means we didn't get the bhavcopy at all
            # (wrong file / mangled response) — a genuine failure.
            raise ValueError(
                f"BhavCopy CSV missing core columns {missing_core} "
                f"| got columns={sorted(cols)}"
            )

        missing_deliv = {"deliv_qty", "deliv_pct"} - cols
        if missing_deliv:
            # Price columns are present but delivery isn't — NSE appends
            # DELIV_QTY/DELIV_PER only after settlement, so the file just isn't
            # ready yet. Signal a retry rather than a hard failure.
            raise BhavDataNotReady(
                f"Delivery columns {missing_deliv} not yet published for "
                f"{trade_date.isoformat()} | got columns={sorted(cols)}"
            )

        # sec_bhavdata_full pads string fields with leading spaces.
        df["symbol"] = df["symbol"].astype(str).str.strip()
        df["series"] = df["series"].astype(str).str.strip()
        df = df[df["series"] == "EQ"].copy()

        try:
            from f_o_stocks_list import get_stock_futures
            fno = set(get_stock_futures())
            df = df[df["symbol"].isin(fno)]
        except Exception:
            logger.warning("BhavCollector: FNO symbol list unavailable — keeping all EQ rows")

        df = df[["symbol", "open", "high", "low", "close", "volume",
                  "deliv_qty", "deliv_pct"]].copy()

        for col in ["open", "high", "low", "close", "volume", "deliv_qty", "deliv_pct"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["open", "close"])
        df["date"] = trade_date.isoformat()

        logger.info("BhavCollector.fetch: %d rows parsed for %s",
                    len(df), trade_date.isoformat())
        return df

    def save(self, df: pd.DataFrame, trade_date: _Date) -> tuple[int, int]:
        if df.empty:
            logger.warning("BhavCollector.save: empty DataFrame for %s",
                           trade_date.isoformat())
            return 0, 0

        self._init_table()
        conn = sqlite3.connect(self._db_path)
        inserted = 0
        try:
            cur = conn.cursor()
            for _, row in df.iterrows():
                cur.execute("""
                    INSERT INTO delivery_daily
                        (date, symbol, open, high, low, close, volume, deliv_qty, deliv_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, symbol) DO NOTHING
                """, (
                    row["date"],
                    row["symbol"],
                    float(row["open"])      if pd.notna(row["open"])      else None,
                    float(row["high"])      if pd.notna(row["high"])      else None,
                    float(row["low"])       if pd.notna(row["low"])       else None,
                    float(row["close"])     if pd.notna(row["close"])     else None,
                    int(row["volume"])      if pd.notna(row["volume"])    else None,
                    int(row["deliv_qty"])   if pd.notna(row["deliv_qty"]) else None,
                    float(row["deliv_pct"]) if pd.notna(row["deliv_pct"]) else None,
                ))
                if cur.rowcount > 0:
                    inserted += 1
            conn.commit()
        except Exception:
            logger.exception("BhavCollector.save failed | date=%s", trade_date.isoformat())
        finally:
            conn.close()

        logger.info("BhavCollector.save: inserted=%d / total=%d | date=%s",
                    inserted, len(df), trade_date.isoformat())
        return inserted, len(df)

    def _already_saved(self, trade_date: _Date) -> bool:
        """True if delivery_daily already has rows for this date — lets the
        evening retry slots run harmlessly once a date is captured."""
        try:
            conn = sqlite3.connect(self._db_path)
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM delivery_daily WHERE date = ?",
                    (trade_date.isoformat(),),
                )
                return cur.fetchone()[0] > 0
            finally:
                conn.close()
        except sqlite3.OperationalError:
            # table not created yet — nothing saved
            return False

    def run(self, trade_date: _Date = None) -> None:
        if trade_date is None:
            trade_date = _Date.today()

        if self._already_saved(trade_date):
            logger.info("BhavCollector.run: already saved for %s — skipping",
                        trade_date.isoformat())
            return

        logger.info("BhavCollector.run: starting for %s", trade_date.isoformat())
        try:
            df = self.fetch(trade_date)
            inserted, total = self.save(df, trade_date)
            send_telegram(
                f"📦 <b>Bhavcopy</b> {trade_date.isoformat()}: "
                f"saved {inserted}/{total} rows → delivery_daily"
            )
        except BhavDataNotReady as exc:
            # Expected transient — a later retry slot will pick it up.
            logger.warning("BhavCollector.run: data not ready | date=%s | %s",
                           trade_date.isoformat(), exc)
            send_telegram(
                f"⏳ <b>Bhavcopy</b> {trade_date.isoformat()}: "
                f"delivery data not published yet — will retry later"
            )
        except Exception as exc:
            logger.exception("BhavCollector.run failed | date=%s", trade_date.isoformat())
            send_telegram(
                f"⚠️ <b>Bhavcopy</b> {trade_date.isoformat()} failed: {exc}"
            )
