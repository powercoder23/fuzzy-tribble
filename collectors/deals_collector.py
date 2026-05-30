"""
Deals collector — fetches NSE bulk / block / short deals and persists
them into the deals table in iv_history.db.

Run twice daily: 10:35 (block window close) and 15:45 (after market close).
"""

import logging
import sqlite3
import time

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS deals (
    date       TEXT,
    symbol     TEXT,
    name       TEXT,
    deal_type  TEXT,
    client     TEXT,
    trade_type TEXT,
    quantity   REAL,
    price      REAL,
    value_cr   REAL,
    remarks    TEXT,
    PRIMARY KEY (date, symbol, deal_type, client)
)
"""

_NSE_DEALS_URL = (
    "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
)

_DEAL_SECTIONS = {
    "BULK_DEALS_DATA":  "BULK",
    "BLOCK_DEALS_DATA": "BLOCK",
    "SHORT_DEALS_DATA": "SHORT",
}


class DealsCollector:

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
        resp = sess.get(_NSE_DEALS_URL, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def parse(self, raw: dict) -> pd.DataFrame:
        rows = []
        for key, deal_type in _DEAL_SECTIONS.items():
            records = raw.get(key) or []
            for rec in records:
                symbol = rec.get("symbol", "")
                qty    = rec.get("qty")
                watp   = rec.get("watp")
                try:
                    qty_f  = float(qty)  if qty  is not None else None
                    watp_f = float(watp) if watp is not None else None
                    value_cr = (
                        round(qty_f * watp_f / 1e7, 4)
                        if qty_f is not None and watp_f is not None
                        else None
                    )
                except (TypeError, ValueError):
                    qty_f = watp_f = value_cr = None

                # Coerce None client to '' so idempotent ON CONFLICT works —
                # SQLite treats NULL != NULL in UNIQUE constraints.
                client = rec.get("clientName") or ""

                rows.append({
                    "date":       rec.get("date", ""),
                    "symbol":     symbol,
                    "name":       rec.get("name", ""),
                    "deal_type":  deal_type,
                    "client":     client,
                    "trade_type": rec.get("buySell", ""),
                    "quantity":   qty_f,
                    "price":      watp_f,
                    "value_cr":   value_cr,
                    "remarks":    rec.get("remarks", ""),
                })

        df = pd.DataFrame(rows)
        logger.info(
            "DealsCollector.parse: %d rows (BULK=%d BLOCK=%d SHORT=%d)",
            len(df),
            len(raw.get("BULK_DEALS_DATA") or []),
            len(raw.get("BLOCK_DEALS_DATA") or []),
            len(raw.get("SHORT_DEALS_DATA") or []),
        )
        return df

    def save(self, df: pd.DataFrame) -> None:
        if df.empty:
            logger.warning("DealsCollector.save: empty DataFrame — nothing to save")
            return

        self._init_table()
        conn = sqlite3.connect(self._db_path)
        inserted = 0
        try:
            cur = conn.cursor()
            for _, row in df.iterrows():
                cur.execute("""
                    INSERT INTO deals
                        (date, symbol, name, deal_type, client,
                         trade_type, quantity, price, value_cr, remarks)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, symbol, deal_type, client) DO NOTHING
                """, (
                    row["date"],
                    row["symbol"],
                    row["name"],
                    row["deal_type"],
                    row["client"],
                    row["trade_type"],
                    row["quantity"],
                    row["price"],
                    row["value_cr"],
                    row["remarks"],
                ))
                if cur.rowcount > 0:
                    inserted += 1
            conn.commit()
        except Exception:
            logger.exception("DealsCollector.save failed")
        finally:
            conn.close()

        logger.info("DealsCollector.save: inserted=%d / total=%d", inserted, len(df))

    def run(self) -> None:
        logger.info("DealsCollector.run: fetching NSE deals")
        try:
            raw = self.fetch()
            df  = self.parse(raw)
            self.save(df)
        except Exception:
            logger.exception("DealsCollector.run failed")
