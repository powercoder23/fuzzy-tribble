#!/usr/bin/env python3
"""
Download the Upstox complete instruments file and load it into complete.db.
Run once per day (or on container startup before trading) to keep the
instruments DB fresh.

Usage:
    python complete_json_tosqlite.py
"""
import gzip
import json
import os
import sqlite3

import requests

INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "complete.db")


def fetch_instruments() -> list:
    gz_path = os.path.join(os.path.dirname(__file__), "data", "complete.json.gz")
    os.makedirs(os.path.dirname(gz_path), exist_ok=True)

    print(f"Downloading {INSTRUMENTS_URL} ...")
    resp = requests.get(INSTRUMENTS_URL, timeout=30)
    resp.raise_for_status()

    with open(gz_path, "wb") as f:
        f.write(resp.content)

    with gzip.open(gz_path, "rt", encoding="utf-8") as gz:
        data = json.load(gz)

    os.remove(gz_path)
    print(f"Downloaded {len(data)} instruments.")
    return data


def update_sqlite(data: list, db_path: str = DB_PATH) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS instruments (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            weekly           TEXT,
            segment          TEXT,
            name             TEXT,
            exchange         TEXT,
            expiry           INTEGER,
            instrument_type  TEXT,
            asset_symbol     TEXT,
            underlying_symbol TEXT,
            instrument_key   TEXT UNIQUE,
            lot_size         INTEGER,
            freeze_quantity  REAL,
            exchange_token   TEXT,
            minimum_lot      INTEGER,
            tick_size        REAL,
            asset_type       TEXT,
            underlying_type  TEXT,
            trading_symbol   TEXT,
            strike_price     REAL,
            qty_multiplier   REAL,
            isin             TEXT,
            asset_key        TEXT,
            underlying_key   TEXT
        )
    """)

    cur.execute("DELETE FROM instruments")

    cur.executemany("""
        INSERT OR REPLACE INTO instruments (
            weekly, segment, name, exchange, expiry, instrument_type,
            asset_symbol, underlying_symbol, instrument_key, lot_size,
            freeze_quantity, exchange_token, minimum_lot, tick_size,
            asset_type, underlying_type, trading_symbol, strike_price,
            qty_multiplier, isin, asset_key, underlying_key
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [
        (
            r.get("weekly"), r.get("segment"), r.get("name"), r.get("exchange"),
            r.get("expiry"), r.get("instrument_type"), r.get("asset_symbol"),
            r.get("underlying_symbol"), r.get("instrument_key"), r.get("lot_size"),
            r.get("freeze_quantity"), r.get("exchange_token"), r.get("minimum_lot"),
            r.get("tick_size"), r.get("asset_type"), r.get("underlying_type"),
            r.get("trading_symbol"), r.get("strike_price"), r.get("qty_multiplier"),
            r.get("isin"), r.get("asset_key"), r.get("underlying_key"),
        )
        for r in data
    ])

    # Indexes for fast option lookups
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_option_lookup
        ON instruments (underlying_symbol, strike_price, expiry, instrument_type)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_trading_symbol
        ON instruments (trading_symbol)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_underlying_expiry
        ON instruments (underlying_symbol, expiry, instrument_type)
    """)

    conn.commit()
    cur.execute("SELECT COUNT(*) FROM instruments")
    print(f"Loaded {cur.fetchone()[0]} instruments into {db_path}")
    conn.close()


def run():
    data = fetch_instruments()
    update_sqlite(data)


if __name__ == "__main__":
    run()
