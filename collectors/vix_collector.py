"""
VIX collector — fetches India VIX EOD data from NSE allIndices endpoint
and persists it into the vix_daily table in iv_history.db.

Run at: 18:30 daily.
"""

import logging
import sqlite3
import time
from datetime import date as _Date

import requests

from collectors.notify import send_telegram

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS vix_daily (
    date       TEXT PRIMARY KEY,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    prev_close REAL,
    change     REAL,
    pct_change REAL
)
"""

_NSE_INDICES_URL = "https://www.nseindia.com/api/allIndices"


class VixCollector:

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
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer":         "https://www.nseindia.com",
            "Accept":          "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        sess.get("https://www.nseindia.com", timeout=15)
        time.sleep(2)
        return sess

    def fetch(self) -> dict:
        sess = self._make_session()
        resp = sess.get(_NSE_INDICES_URL, timeout=15)
        resp.raise_for_status()
        entries = resp.json().get("data", [])
        vix = next((e for e in entries if e.get("index") == "INDIA VIX"), None)
        if vix is None:
            raise ValueError("INDIA VIX entry not found in allIndices response")
        logger.info(
            "VixCollector.fetch: INDIA VIX last=%.2f prev_close=%.2f",
            vix.get("last", 0),
            vix.get("previousClose", 0),
        )
        return vix

    def save(self, record: dict) -> bool:
        today = _Date.today().isoformat()
        self._init_table()
        conn = sqlite3.connect(self._db_path)
        inserted = False
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO vix_daily
                    (date, open, high, low, close, prev_close, change, pct_change)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO NOTHING
            """, (
                today,
                record.get("open"),
                record.get("high"),
                record.get("low"),
                record.get("last"),
                record.get("previousClose"),
                record.get("change"),
                record.get("percentChange"),
            ))
            inserted = cur.rowcount > 0
            conn.commit()
            logger.info(
                "VixCollector.save: date=%s inserted=%s close=%.2f",
                today, inserted, record.get("last", 0),
            )
        except Exception:
            logger.exception("VixCollector.save failed | date=%s", today)
        finally:
            conn.close()
        return inserted

    def run(self) -> None:
        logger.info("VixCollector.run: fetching India VIX")
        try:
            record  = self.fetch()
            inserted = self.save(record)
            last    = record.get("last")
            pct     = record.get("percentChange")
            pct_str = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "n/a"
            tag     = "saved" if inserted else "already saved"
            send_telegram(
                f"📈 <b>India VIX</b> {_Date.today().isoformat()}: "
                f"{last} ({pct_str}) → vix_daily [{tag}]"
            )
        except Exception as exc:
            logger.exception("VixCollector.run failed")
            send_telegram(f"⚠️ <b>India VIX</b> collector failed: {exc}")
