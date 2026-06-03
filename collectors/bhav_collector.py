"""
Bhavcopy collector — downloads NSE CM bhavcopy ZIP and persists
delivery + OHLC data for F&O symbols into delivery_daily table.
"""

import io
import logging
import sqlite3
import time
import zipfile
from datetime import date as _Date

import pandas as pd
import requests

from collectors.notify import send_telegram

logger = logging.getLogger(__name__)

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
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
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
        url = _BHAV_URL.format(date=trade_date.strftime("%Y%m%d"))
        logger.info("BhavCollector.fetch: GET %s", url)

        sess = self._make_session()
        df = None
        last_err = None
        for attempt in range(3):
            try:
                resp = sess.get(url, timeout=30)
                resp.raise_for_status()
                # A blocked request can return HTTP 200 with an HTML body
                # instead of the ZIP — ZipFile then raises BadZipFile, which
                # the retry below handles.
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    csv_name = next(n for n in zf.namelist() if n.endswith(".csv"))
                    with zf.open(csv_name) as f:
                        df = pd.read_csv(f)
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
            "TckrSymb":    "symbol",
            "SctySrs":     "series",
            "OpnPric":     "open",
            "HghPric":     "high",
            "LwPric":      "low",
            "ClsPric":     "close",
            "TtlTradgVol": "volume",
            "DlvryQty":    "deliv_qty",
            "DlvryPct":    "deliv_pct",
        })

        required = {"symbol", "series", "open", "high", "low", "close",
                    "volume", "deliv_qty", "deliv_pct"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"BhavCopy CSV missing expected columns: {missing}")

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

    def run(self, trade_date: _Date = None) -> None:
        if trade_date is None:
            trade_date = _Date.today()
        logger.info("BhavCollector.run: starting for %s", trade_date.isoformat())
        try:
            df = self.fetch(trade_date)
            inserted, total = self.save(df, trade_date)
            send_telegram(
                f"📦 <b>Bhavcopy</b> {trade_date.isoformat()}: "
                f"saved {inserted}/{total} rows → delivery_daily"
            )
        except Exception as exc:
            logger.exception("BhavCollector.run failed | date=%s", trade_date.isoformat())
            send_telegram(
                f"⚠️ <b>Bhavcopy</b> {trade_date.isoformat()} failed: {exc}"
            )
