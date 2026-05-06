import os
from typing import Dict, List
import pandas as pd
import sqlite3
from datetime import datetime
from dhanhq import marketfeed

SCRIP_MASTER_FILE = "data/api-scrip-master.csv"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
DB_FILE = "data/api-scrip-master.db"
TABLE_NAME = "scrip_master"
LAST_UPDATED_FILE = "data/scrip_master_last_updated.txt"

def download_scrip_master():
    """Download the scrip master CSV from Dhan and save locally."""
    print("Downloading scrip master CSV...")
    df = pd.read_csv(SCRIP_MASTER_URL, dtype=str, low_memory=False)
    os.makedirs(os.path.dirname(SCRIP_MASTER_FILE), exist_ok=True)
    df.to_csv(SCRIP_MASTER_FILE, index=False)
    return df

def load_scrip_master():
    """Load CSV from local, or download if missing."""
    if os.path.exists(SCRIP_MASTER_FILE):
        print("Loading scrip master from local file...")
        return pd.read_csv(SCRIP_MASTER_FILE, dtype=str, low_memory=False)
    else:
        return download_scrip_master()

def save_to_sqlite(df):
    """Save DataFrame to SQLite, replacing existing table."""
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    df.to_sql(TABLE_NAME, conn, if_exists="replace", index=False)
    conn.close()
    print(f"Scrip master saved to {DB_FILE} (table: {TABLE_NAME})")

def get_symbol_from_security_id(security_id: str) -> str:
    """Fetch trading symbol for a single security ID."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    query = f"""
        SELECT SEM_TRADING_SYMBOL
        FROM {TABLE_NAME}
        WHERE SEM_SMST_SECURITY_ID = ?
        LIMIT 1
    """
    cursor.execute(query, (security_id,))
    row = cursor.fetchone()
    conn.close()

    return row[0] if row else None

def get_instruments(stock_names, exchange="NSE"):
    """
    Fetch SEM_SMST_SECURITY_ID for multiple stock names
    and return them in the instruments format:
    [
        (marketfeed.NSE, "SECURITY_ID", marketfeed.Full),
        ...
    ]
    """
    if not stock_names:
        return []

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    placeholders = ",".join("?" for _ in stock_names)

    if exchange == "NSE" :
        query = f"""
            SELECT SEM_TRADING_SYMBOL, SEM_SMST_SECURITY_ID, SEM_EXM_EXCH_ID
            FROM {TABLE_NAME}
            WHERE SEM_TRADING_SYMBOL IN ({placeholders})
            AND SEM_EXM_EXCH_ID = 'NSE'
            AND SEM_SEGMENT = 'E'
        """
    elif exchange == "MCX":
        query = f"""
            SELECT
                s.SEM_TRADING_SYMBOL,
                s.SEM_SMST_SECURITY_ID,
                s.SEM_EXM_EXCH_ID
                FROM {TABLE_NAME} s
                WHERE s.SM_SYMBOL_NAME IN ({placeholders})
                AND s.SEM_EXM_EXCH_ID = "MCX"
                AND s.SEM_SEGMENT = "M"
                AND s.SEM_EXCH_INSTRUMENT_TYPE = "FUTCOM"
                AND s.SEM_EXPIRY_DATE = (
                    SELECT MIN(SEM_EXPIRY_DATE)
                    FROM {TABLE_NAME}
                    WHERE SM_SYMBOL_NAME = s.SM_SYMBOL_NAME
                        AND SEM_EXM_EXCH_ID = "MCX"
                        AND SEM_SEGMENT = "M"
                        AND SEM_EXCH_INSTRUMENT_TYPE = "FUTCOM"
                )
                ORDER BY s.SEM_EXPIRY_DATE ASC;
        """

    cursor.execute(query, stock_names)
    rows = cursor.fetchall()
    conn.close()

    instruments = []
    for symbol, sec_id, exch_id in rows:
        exch = getattr(marketfeed, exch_id)  # e.g. marketfeed.NSE
        instruments.append((exch, str(sec_id), marketfeed.Full))

    return instruments


def get_instruments_depth(stock_names: List[str], exchange: str = "NSE") -> List[Dict[str, str]]:
    """
    Fetch SEM_SMST_SECURITY_ID for multiple stock names and return them in Dhan subscribe format:
      [{"ExchangeSegment": "NSE_EQ", "SecurityId": "1333"}, ...]
    Keeps SQL logic similar to your original get_instruments function.

    Parameters
    ----------
    stock_names : list[str]
        List of trading symbols (for NSE) or symbol names (for MCX) — same as before.
    marketfeed : optional
        Ignored for return format, kept for compatibility with your existing callers.
    exchange : str
        "NSE" or "MCX" (keeps original behavior). Mapping to Dhan ExchangeSegment is applied.

    Returns
    -------
    List[Dict[str,str]]
        Example: [{"ExchangeSegment":"NSE_EQ","SecurityId":"1333"}, ...]
    """

    if not stock_names:
        return []

    # mapping from your DB exchange id to Dhan ExchangeSegment enum string (Annexure)
    exchange_segment_map = {
        "NSE": "NSE_EQ",
        "MCX": "MCX_COMM",
        "BSE": "BSE_EQ"
    }

    # fallback
    exchange_segment = exchange_segment_map.get(exchange, exchange)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    placeholders = ",".join("?" for _ in stock_names)

    if exchange == "NSE":
        query = f"""
            SELECT SEM_TRADING_SYMBOL, SEM_SMST_SECURITY_ID, SEM_EXM_EXCH_ID
            FROM {TABLE_NAME}
            WHERE SEM_TRADING_SYMBOL IN ({placeholders})
            AND SEM_EXM_EXCH_ID = 'NSE'
            AND SEM_SEGMENT = 'E'
        """
        params = stock_names
    elif exchange == "MCX":
        query = f"""
            SELECT
                s.SEM_TRADING_SYMBOL,
                s.SEM_SMST_SECURITY_ID,
                s.SEM_EXM_EXCH_ID
                FROM {TABLE_NAME} s
                WHERE s.SM_SYMBOL_NAME IN ({placeholders})
                AND s.SEM_EXM_EXCH_ID = "MCX"
                AND s.SEM_SEGMENT = "M"
                AND s.SEM_EXCH_INSTRUMENT_TYPE = "FUTCOM"
                AND s.SEM_EXPIRY_DATE = (
                    SELECT MIN(SEM_EXPIRY_DATE)
                    FROM {TABLE_NAME}
                    WHERE SM_SYMBOL_NAME = s.SM_SYMBOL_NAME
                        AND SEM_EXM_EXCH_ID = "MCX"
                        AND SEM_SEGMENT = "M"
                        AND SEM_EXCH_INSTRUMENT_TYPE = "FUTCOM"
                )
                ORDER BY s.SEM_EXPIRY_DATE ASC;
        """
        params = stock_names
    else:
        # generic fallback: try to fetch by trading symbol matching the provided names
        query = f"""
            SELECT SEM_TRADING_SYMBOL, SEM_SMST_SECURITY_ID, SEM_EXM_EXCH_ID
            FROM {TABLE_NAME}
            WHERE SEM_TRADING_SYMBOL IN ({placeholders})
        """
        params = stock_names

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    instruments = []
    for symbol, sec_id, exch_id in rows:
        # Map DB exchange -> Dhan ExchangeSegment (if possible)
        # Prefer the explicit mapping derived from 'exchange' param; otherwise try using exch_id
        if exchange in exchange_segment_map:
            exch_seg = exchange_segment_map[exchange]
        else:
            exch_seg = exchange_segment_map.get(exch_id, exch_id)

        instruments.append({
            "ExchangeSegment": exch_seg,
            "SecurityId": str(sec_id)
        })

    return instruments


def get_security_id_symbol_map(stock_names: List[str], exchange: str = "NSE") -> Dict[int, str]:
    """
    Resolve trading symbols to Dhan security IDs and return:
      {1333: "HDFCBANK", 1592: "RELIANCE", ...}
    """
    if not stock_names:
        return {}

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in stock_names)

    if exchange == "NSE":
        query = f"""
            SELECT DISTINCT SEM_TRADING_SYMBOL, SEM_SMST_SECURITY_ID
            FROM {TABLE_NAME}
            WHERE SEM_TRADING_SYMBOL IN ({placeholders})
              AND SEM_EXM_EXCH_ID = 'NSE'
              AND SEM_SEGMENT = 'E'
        """
        params = stock_names
    else:
        query = f"""
            SELECT DISTINCT SEM_TRADING_SYMBOL, SEM_SMST_SECURITY_ID
            FROM {TABLE_NAME}
            WHERE SEM_TRADING_SYMBOL IN ({placeholders})
        """
        params = stock_names

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    resolved: Dict[int, str] = {}
    for symbol, sec_id in rows:
        try:
            resolved[int(sec_id)] = str(symbol)
        except (TypeError, ValueError):
            continue

    return resolved
def update_scrip_master():
    """Main function to download and save scrip master to SQLite once per day."""
    today = datetime.today().date()

    # Check last updated date
    if os.path.exists(LAST_UPDATED_FILE):
        with open(LAST_UPDATED_FILE, "r") as f:
            last_updated = f.read().strip()
        if last_updated == str(today):
            print("Scrip master already updated today. Skipping download.")
            return

    # If not updated today → download a fresh copy and update the DB.
    df = download_scrip_master()
    save_to_sqlite(df)

    with open(LAST_UPDATED_FILE, "w") as f:
        f.write(str(today))

    print(f"Scrip master updated for {today}, rows saved: {len(df)}")
