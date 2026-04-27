import os
import logging
import math
import sqlite3
from pathlib import Path

import pandas as pd
import numpy as np
import requests
from dhanhq import dhanhq, DhanContext
from dotenv import load_dotenv
from datetime import datetime, timedelta
import time
from scipy import stats
import warnings
from f_o_stocks_list import get_stock_futures
from load_scrip_master_sqlite import update_scrip_master, get_security_id_symbol_map
warnings.filterwarnings('ignore')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

IV_HISTORY_FILE = Path("iv_history.csv")
DB_PATH = "iv_history.db"
EXPIRED_OPTIONS_CACHE_DIR = Path("data/expired_options_cache")
MIN_IV_SAMPLES = 30
DEFAULT_FNO_STOCKS = {
    13: "NIFTY",
    14: "BANKNIFTY",
    1333: "HDFCBANK",
    1592: "RELIANCE",
    1610: "ICICIBANK",
    1523: "INFY",
    1394: "TCS",
    1510: "HINDUNILVR",
    1633: "SBIN",
}
IV_HISTORY_COLUMNS = [
    "snapshot_date",
    "snapshot_time",
    "security_id",
    "symbol",
    "spot_price",
    "atm_strike",
    "atm_iv",
    "atm_call_iv",
    "atm_put_iv",
]
IV_HISTORY_OPTIONAL_COLUMNS = {
    "atm_call_oi": "REAL",
    "atm_put_oi": "REAL",
    "total_call_oi": "REAL",
    "total_put_oi": "REAL",
    "total_call_volume": "REAL",
    "total_put_volume": "REAL",
    "max_oi_strike_call": "REAL",
    "max_oi_strike_put": "REAL",
}


def ensure_iv_history_schema(cursor):
    existing_columns = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(iv_history)").fetchall()
    }
    for column_name, column_type in IV_HISTORY_OPTIONAL_COLUMNS.items():
        if column_name not in existing_columns:
            cursor.execute(
                f"ALTER TABLE iv_history ADD COLUMN {column_name} {column_type}"
            )


def init_iv_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS iv_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        security_id TEXT,
        symbol TEXT,
        timestamp DATETIME,
        spot_price REAL,
        atm_strike REAL,
        atm_iv REAL,
        atm_call_iv REAL,
        atm_put_iv REAL,
        data_type TEXT,
        UNIQUE(security_id, timestamp, data_type)
    )
    """)
    ensure_iv_history_schema(cursor)

    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_iv_security_time
    ON iv_history(security_id, timestamp)
    """)

    conn.commit()
    conn.close()


def migrate_csv_to_sqlite():
    if os.path.exists("iv_migrated.flag"):
        return

    if not os.path.exists("iv_history.csv"):
        return

    df = pd.read_csv("iv_history.csv")

    if df.empty:
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    ensure_iv_history_schema(cursor)

    for _, row in df.iterrows():
        timestamp = f"{row['snapshot_date']} {row.get('snapshot_time', '00:00:00')}"
        security_id = str(row["security_id"])
        atm_iv = row.get("atm_iv")
        data_type = "daily"

        if pd.isna(security_id) or pd.isna(timestamp) or pd.isna(atm_iv):
            continue

        cursor.execute("""
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
            security_id,
            row.get("symbol"),
            timestamp,
            row.get("spot_price"),
            row.get("atm_strike"),
            atm_iv,
            row.get("atm_call_iv"),
            row.get("atm_put_iv"),
            row.get("atm_call_oi"),
            row.get("atm_put_oi"),
            row.get("total_call_oi"),
            row.get("total_put_oi"),
            row.get("total_call_volume"),
            row.get("total_put_volume"),
            row.get("max_oi_strike_call"),
            row.get("max_oi_strike_put"),
            data_type,
        ))

    conn.commit()
    conn.close()

    with open("iv_migrated.flag", "w") as f:
        f.write("done")

def normalize_expiry_value(value):
    """Convert Dhan expiry payload values into YYYY-MM-DD strings when possible."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def get_trading_days_to_expiry(expiry_str):
    expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    today = datetime.now().date()
    if expiry_date < today:
        return 0

    trading_days = 0
    current_date = today
    while current_date <= expiry_date:
        if current_date.weekday() < 5:
            trading_days += 1
        current_date += timedelta(days=1)
    return trading_days


def unwrap_dhan_payload(payload):
    """Return the innermost data dict from Dhan's nested success payloads."""
    current = payload
    while isinstance(current, dict) and isinstance(current.get("data"), dict):
        current = current["data"]
    return current if isinstance(current, dict) else {}


def clip_score(value, floor=0.0, ceiling=100.0):
    return max(floor, min(ceiling, value))


def native_number(value):
    if value is None or pd.isna(value):
        return None
    return float(value)


def classify_iv_regime(iv_rank, iv_percentile):
    """Classify historical IV state from IV Rank and IV Percentile."""
    if iv_rank is None or iv_percentile is None:
        return "MID"
    if iv_rank < 30 and iv_percentile < 30:
        return "LOW"
    if iv_rank > 60 or iv_percentile > 70:
        return "HIGH"
    if 30 <= iv_rank <= 60:
        return "MID"
    return "MID"


def format_expiry_label(expiry_value):
    """Format expiry values as 'DD MON' for Telegram output."""
    if expiry_value is None or pd.isna(expiry_value):
        return "N/A"
    expiry_dt = pd.to_datetime(expiry_value, errors="coerce")
    if pd.isna(expiry_dt):
        return str(expiry_value)
    return expiry_dt.strftime("%d %b").upper()


def reduce_to_one_per_symbol_expiry(df):
    """
    Keep only the highest-scoring trade per (symbol, expiry).

    Args:
        df: DataFrame from scan_all_fno_stocks

    Returns:
        pd.DataFrame: Best row per symbol and expiry
    """
    if df is None:
        return pd.DataFrame()
    if df.empty:
        return df

    reduced_df = df.copy()
    reduced_df["expiry"] = pd.to_datetime(reduced_df.get("expiry"), errors="coerce")
    reduced_df = reduced_df.dropna(subset=["symbol", "expiry"])
    if reduced_df.empty:
        return reduced_df

    reduced_df = reduced_df.sort_values("score", ascending=False)
    reduced_df = reduced_df.drop_duplicates(subset=["symbol", "expiry"], keep="first")
    return reduced_df.reset_index(drop=True)


def split_message(msg, chunk_size=4000):
    return [msg[i:i + chunk_size] for i in range(0, len(msg), chunk_size)]


NEAR_WALL_STRIKE_DISTANCE = 50.0

class DiscountedPremiumScanner:
    """
    Scanner to identify options trading at discounted premiums
    Using Dhan API with proper authentication pattern
    """
    
    _shared_runtime_state = None

    def __init__(self, hardtoken, client_id="1104878989", store_intraday=False):
        """
        Initialize scanner with Dhan API credentials
        
        Args:
            hardtoken: JWT token from Dhan
            client_id: Your Dhan client ID
        """
        if not client_id or not hardtoken:
            raise ValueError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required.")

        self.client_id = client_id
        self.access_token = hardtoken
        self.context = DhanContext(client_id, hardtoken)
        self.dhan = dhanhq(self.context)
        self.risk_free_rate = 0.065  # 6.5% - update from RBI periodically
        self.iv_history_file = IV_HISTORY_FILE
        self.expired_options_cache_dir = EXPIRED_OPTIONS_CACHE_DIR
        self.store_intraday = store_intraday
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.expired_data_cache = {}
        self._scan_quality_stats = {"pre_quality": 0, "post_quality": 0}
        self.expired_options_cache_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_state = self._ensure_runtime_state()
        self.fno_stocks = self.load_fno_stocks()

    def _ensure_runtime_state(self):
        today = datetime.now().date().isoformat()
        if DiscountedPremiumScanner._shared_runtime_state is None:
            DiscountedPremiumScanner._shared_runtime_state = {
                "cache_day": today,
                "fno_symbols": None,
                "fno_symbols_day": None,
                "expiries": {},
                "option_chain": {},
                "metrics": {
                    "total_calls": 0,
                    "cache_hits": 0,
                    "failures": 0,
                    "iv_snapshots": 0,
                },
                "last_api_call_ts": 0.0,
                "previous_state": {},
            }

        state = DiscountedPremiumScanner._shared_runtime_state
        if state.get("cache_day") != today:
            state["cache_day"] = today
            state["fno_symbols"] = None
            state["fno_symbols_day"] = None
            state["expiries"] = {}
            state["option_chain"] = {}
            state["metrics"] = {
                "total_calls": 0,
                "cache_hits": 0,
                "failures": 0,
                "iv_snapshots": 0,
            }
            state["last_api_call_ts"] = 0.0
            state["previous_state"] = {}
            logger.info("Reset scanner runtime caches for %s", today)
        return state

    def rate_limited_call(self, operation_name, func, *args, **kwargs):
        min_interval = 1.5
        elapsed = time.monotonic() - self.runtime_state["last_api_call_ts"]
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        response = func(*args, **kwargs)
        self.runtime_state["last_api_call_ts"] = time.monotonic()
        self.runtime_state["metrics"]["total_calls"] += 1
        logger.debug("API call completed for %s", operation_name)
        return response

    def fetch_with_retry(self, operation_name, fetcher, validator=None, max_attempts=5):
        last_error = None
        for attempt in range(max_attempts):
            try:
                response = self.rate_limited_call(operation_name, fetcher)
                if validator and not validator(response):
                    if isinstance(response, dict) and "data" in response:
                        return response
                    raise ValueError(f"{operation_name} returned invalid data")
                return response
            except Exception as exc:
                last_error = exc
                self.runtime_state["metrics"]["failures"] += 1
                if attempt < max_attempts - 1:
                    backoff = 2 ** attempt
                    logger.warning(
                        "Retry %s for %s after error: %s",
                        attempt + 1,
                        operation_name,
                        exc,
                    )
                    time.sleep(backoff)
                else:
                    logger.warning("Exhausted retries for %s: %s", operation_name, exc)
        raise RuntimeError(f"{operation_name} failed after {max_attempts} attempts") from last_error

    def get_previous_state_store(self):
        return self.runtime_state.setdefault("previous_state", {})

    def get_cached_or_fetch(self, cache, key, fetcher, cache_label, validator=None):
        if key in cache:
            self.runtime_state["metrics"]["cache_hits"] += 1
            logger.info("Cache hit for %s", cache_label)
            return cache[key]

        logger.info("Cache miss for %s; calling API", cache_label)
        value = self.fetch_with_retry(cache_label, fetcher, validator=validator)
        cache[key] = value
        return value

    def get_warmup_metrics(self):
        return dict(self.runtime_state["metrics"])
        
    # ==================== 1. DATA FETCHING METHODS ====================

    def load_fno_stocks(self):
        """
        Build the F&O universe dynamically by combining:
        1. NSE's live stock-futures symbols
        2. Dhan scrip-master security-id resolution
        """
        if (
            self.runtime_state.get("fno_symbols") is not None
            and self.runtime_state.get("fno_symbols_day") == self.runtime_state.get("cache_day")
        ):
            self.runtime_state["metrics"]["cache_hits"] += 1
            logger.info("Cache hit for F&O universe")
            return dict(self.runtime_state["fno_symbols"])

        reserved_indices = {
            13: "NIFTY",
            14: "BANKNIFTY",
        }
        resolved = {}

        try:
            update_scrip_master()
            symbols = [
                symbol for symbol in get_stock_futures()
                if symbol and "NSETEST" not in symbol.upper()
            ]
            symbol_map = get_security_id_symbol_map(symbols, exchange="NSE")
            if symbol_map:
                symbol_map = {
                    sec_id: symbol
                    for sec_id, symbol in symbol_map.items()
                    if "NSETEST" not in str(symbol).upper()
                }
                resolved.update(symbol_map)
                logger.info("Loaded %s stock F&O symbols from NSE + scrip master", len(symbol_map))
            else:
                logger.warning("No stock F&O symbols were resolved from scrip master; using fallback defaults")
        except Exception:
            logger.exception("Failed to build dynamic F&O stock universe; using fallback defaults")
            resolved.update({
                sec_id: symbol for sec_id, symbol in DEFAULT_FNO_STOCKS.items()
                if sec_id not in reserved_indices
            })

        resolved.update(reserved_indices)
        fallback_missing = {
            sec_id: symbol
            for sec_id, symbol in DEFAULT_FNO_STOCKS.items()
            if sec_id not in resolved and symbol not in resolved.values()
        }
        resolved.update(fallback_missing)
        ordered = dict(sorted(resolved.items(), key=lambda item: item[1]))
        self.runtime_state["fno_symbols"] = ordered
        self.runtime_state["fno_symbols_day"] = self.runtime_state["cache_day"]
        return dict(ordered)

    def send_clean_telegram(self, df):
        """Send a concise trader-friendly Telegram summary."""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            logger.info("Telegram alert skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
            return

        if df is None or df.empty:
            message = "📊 Options Scanner Summary\n\nNo qualifying opportunities found."
            message = message.replace("None", "N/A")
            messages = split_message(message[:4000])
            logger.info("Telegram summary trades total=%s filtered=%s message_length=%s", 0, 0, len(message))
        else:
            total_trades = len(df)
            telegram_df = df.sort_values("score", ascending=False).head(10).reset_index(drop=True)
            lines = []
            for _, row in telegram_df.iterrows():
                expiry_label = pd.to_datetime(row["expiry"], errors="coerce").strftime("%d %b").upper() if pd.notna(row.get("expiry")) else ""

                buildup = str(row.get("buildup_type", "NA"))

                line1 = f"{row['symbol']} {expiry_label} {int(row['strike'])}{row['type'][0]} | {buildup}"

                rr = row.get("risk_reward")
                rr_text = f"{rr:.1f}" if rr is not None else "N/A"

                line2 = (
                    f"E:{row['entry']:.1f} SL:{row['stop_loss']:.1f} "
                    f"T:{row['target']:.1f} RR:{rr_text}"
                )

                lines.append(line1)
                lines.append(line2)
                lines.append("")

            message = "\n".join(lines).strip()
            if len(message) > 3800:
                message = message[:3800] + "\n...truncated"
            message = message.replace("None", "N/A")
            logger.info(
                "Telegram summary trades total=%s filtered=%s message_length=%s",
                total_trades,
                len(telegram_df),
                len(message),
            )
            messages = split_message(message)

        try:
            for chunk in messages:
                response = requests.post(
                    f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage",
                    json={
                        "chat_id": self.telegram_chat_id,
                        "text": chunk,
                        "parse_mode": None,
                    },
                    timeout=15,
                )
                response.raise_for_status()
            logger.info("Clean Telegram summary sent")
        except Exception:
            logger.exception("Failed to send clean Telegram summary")

    def send_telegram_summary(self, opportunities_df):
        """Backward-compatible wrapper for the clean Telegram sender."""
        self.send_clean_telegram(opportunities_df)
    
    def get_option_chain(self, underlying_security_id, underlying_segment, expiry):
        """
        Fetch real-time option chain for a specific expiry
        
        Args:
            underlying_security_id: Security ID (e.g., 13 for NIFTY)
            underlying_segment: "IDX_I" for indices, "NSE_FNO" for stocks
            expiry: Expiry date in "YYYY-MM-DD" format
        
        Returns:
            dict: Option chain data
        """
        cache_key = (str(underlying_security_id), underlying_segment, expiry)
        cache_label = f"{underlying_security_id} option chain {expiry}"

        def fetcher():
            return self.dhan.option_chain(
                under_security_id=underlying_security_id,
                under_exchange_segment=underlying_segment,
                expiry=expiry,
            )

        def validator(response):
            return isinstance(response, dict) and "data" in response

        try:
            return self.get_cached_or_fetch(
                self.runtime_state["option_chain"],
                cache_key,
                fetcher,
                cache_label,
                validator=validator,
            )
        except Exception:
            logger.exception(
                "Failed to fetch option chain for %s (%s), expiry %s",
                underlying_security_id,
                underlying_segment,
                expiry,
            )
            return {"status": "failure", "data": {}}
    
    def get_expiry_list(self, underlying_security_id, underlying_segment):
        """
        Get all available expiries for an underlying
        
        Args:
            underlying_security_id: Security ID
            underlying_segment: "IDX_I" or "NSE_FNO"
        
        Returns:
            list: List of expiry dates
        """
        cache_key = (str(underlying_security_id), underlying_segment)
        cache_label = f"{underlying_security_id} expiry list"

        def fetcher():
            return self.dhan.expiry_list(
                under_security_id=underlying_security_id,
                under_exchange_segment=underlying_segment,
            )

        def parse_expiries(response):
            raw_data = response.get("data", [])
            expiries = []

            if isinstance(raw_data, list):
                for item in raw_data:
                    normalized = normalize_expiry_value(item)
                    if normalized:
                        expiries.append(normalized)
                    elif isinstance(item, dict):
                        for value in item.values():
                            normalized = normalize_expiry_value(value)
                            if normalized:
                                expiries.append(normalized)
                                break
            elif isinstance(raw_data, dict):
                for key in ("data", "expiryList", "expiries", "results", "result"):
                    value = raw_data.get(key)
                    if isinstance(value, list):
                        for item in value:
                            normalized = normalize_expiry_value(item)
                            if normalized:
                                expiries.append(normalized)
                            elif isinstance(item, dict):
                                for nested_value in item.values():
                                    normalized = normalize_expiry_value(nested_value)
                                    if normalized:
                                        expiries.append(normalized)
                                        break
                        break
                if not expiries:
                    for value in raw_data.values():
                        normalized = normalize_expiry_value(value)
                        if normalized:
                            expiries.append(normalized)

            return sorted(set(expiries))

        def validator(response):
            return isinstance(response, dict) and "data" in response

        try:
            response = self.get_cached_or_fetch(
                self.runtime_state["expiries"],
                cache_key,
                fetcher,
                cache_label,
                validator=validator,
            )
        except Exception:
            logger.exception(
                "Failed to fetch expiries for %s (%s)",
                underlying_security_id,
                underlying_segment,
            )
            return []

        expiries = parse_expiries(response)
        if not expiries:
            logger.info(f"No active expiries for {underlying_security_id} ({underlying_segment})")
            return []
        logger.info(
            "Available expiries for %s (%s): %s",
            underlying_security_id,
            underlying_segment,
            expiries,
        )
        return expiries
    
    def fetch_historical_prices(self, security_id, exchange_segment, from_date, to_date):
        """
        Fetch historical OHLC data for HV calculation
        Using Dhan daily historical data API
        
        Args:
            security_id: Security ID
            exchange_segment: Exchange segment (e.g., "NSE_EQ", "IDX_I")
            from_date: Start date "YYYY-MM-DD"
            to_date: End date "YYYY-MM-DD"
        
        Returns:
            pd.DataFrame: Historical price data
        """
        history_exchange_segment = "IDX_I" if exchange_segment == "IDX_I" else "NSE_EQ"
        instrument_type = "INDEX" if history_exchange_segment == "IDX_I" else "EQUITY"

        try:
            response = self.dhan.historical_daily_data(
                security_id=security_id,
                exchange_segment=history_exchange_segment,
                instrument_type=instrument_type,
                from_date=from_date,
                to_date=to_date,
                oi=True
            )
        except Exception:
            logger.exception(
                "Error fetching historical daily prices for %s from %s to %s",
                security_id,
                from_date,
                to_date,
            )
            return pd.DataFrame()

        if response.get("status") != "success":
            logger.warning(
                "Historical daily price fetch failed for %s from %s to %s: %s",
                security_id,
                from_date,
                to_date,
                response,
            )
            return pd.DataFrame()

        payload = unwrap_dhan_payload(response.get("data") or {})
        candles = payload if isinstance(payload, list) else response.get("data")
        if not candles:
            logger.warning(
                "Historical daily price fetch returned no candles for %s from %s to %s",
                security_id,
                from_date,
                to_date,
            )
            return pd.DataFrame()

        df = pd.DataFrame(candles)
        if df.empty:
            return df

        timestamp_col = None
        for candidate in ("timestamp", "start_Time", "start_time", "date", "Date"):
            if candidate in df.columns:
                timestamp_col = candidate
                break

        if timestamp_col:
            series = df[timestamp_col]
            if pd.api.types.is_numeric_dtype(series):
                df["date"] = pd.to_datetime(series, unit="s", errors="coerce")
            else:
                df["date"] = pd.to_datetime(series, errors="coerce")
        else:
            logger.warning(
                "Historical daily price payload missing timestamp/date columns for %s: columns=%s",
                security_id,
                list(df.columns),
            )
            return pd.DataFrame()

        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
        return df
    
    def fetch_historical_iv(self, security_id, exchange_segment, lookback_days=252):
        """
        Load persisted ATM IV history for IV Rank / IV Percentile calculations.
        
        Args:
            security_id: Security ID
            exchange_segment: Exchange segment
            lookback_days: Number of days to look back
        
        Returns:
            list: Historical ATM IV values
        """
        try:
            conn = sqlite3.connect(DB_PATH)
            query = """
            SELECT atm_iv, timestamp
            FROM iv_history
            WHERE security_id = ?
            AND data_type = 'daily'
            ORDER BY timestamp ASC
            """
            df = pd.read_sql(query, conn, params=(str(security_id),))
        except Exception:
            logger.exception("Failed to read IV history database: %s", DB_PATH)
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if df.empty:
            return []

        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df["atm_iv"] = pd.to_numeric(df["atm_iv"], errors="coerce")
        df = df.dropna(subset=["timestamp", "atm_iv"])
        df = df[
            (df["atm_iv"] >= 1.0) &
            (df["atm_iv"] <= 200.0)
        ]
        df = df.sort_values(["timestamp"]).tail(lookback_days)
        return df["atm_iv"].tolist()

    def _expired_options_cache_path(self, security_id, exchange_segment, option_type, strike):
        filename = f"{security_id}_{exchange_segment}_{option_type}_{str(strike).replace('/', '_')}.csv"
        return self.expired_options_cache_dir / filename

    def _load_expired_option_cache(self, cache_path):
        if not cache_path.exists():
            return pd.DataFrame(columns=["timestamp", "iv", "close", "volume", "spot"])

        try:
            df = pd.read_csv(cache_path)
        except Exception:
            logger.exception("Failed to read expired options cache: %s", cache_path)
            return pd.DataFrame(columns=["timestamp", "iv", "close", "volume", "spot"])

        if df.empty:
            return pd.DataFrame(columns=["timestamp", "iv", "close", "volume", "spot"])

        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        else:
            df["timestamp"] = pd.NaT

        for column in ("iv", "close", "volume", "spot"):
            df[column] = pd.to_numeric(df.get(column), errors="coerce")

        df = df[["timestamp", "iv", "close", "volume", "spot"]]
        df = df.dropna(subset=["timestamp", "iv", "close"]).sort_values("timestamp").drop_duplicates(
            subset=["timestamp"],
            keep="last",
        ).reset_index(drop=True)
        return df

    def _save_expired_option_cache(self, cache_path, df):
        cache_df = df.copy()
        if cache_df.empty:
            cache_df = pd.DataFrame(columns=["timestamp", "iv", "close", "volume", "spot"])
        else:
            cache_df = cache_df[["timestamp", "iv", "close", "volume", "spot"]].copy()
            cache_df["timestamp"] = pd.to_datetime(cache_df["timestamp"], errors="coerce")
            cache_df = cache_df.dropna(subset=["timestamp", "iv", "close"]).sort_values("timestamp").drop_duplicates(
                subset=["timestamp"],
                keep="last",
            ).reset_index(drop=True)

        try:
            cache_df.to_csv(cache_path, index=False)
        except Exception:
            logger.exception("Failed to persist expired options cache: %s", cache_path)

    def _merge_expired_option_frames(self, existing_df, new_df):
        frames = []
        if existing_df is not None and not existing_df.empty:
            frames.append(existing_df[["timestamp", "iv", "close", "volume", "spot"]].copy())
        if new_df is not None and not new_df.empty:
            frames.append(new_df[["timestamp", "iv", "close", "volume", "spot"]].copy())

        if not frames:
            return pd.DataFrame(columns=["timestamp", "iv", "close", "volume", "spot"])

        merged = pd.concat(frames, ignore_index=True)
        merged["timestamp"] = pd.to_datetime(merged["timestamp"], errors="coerce")
        for column in ("iv", "close", "volume", "spot"):
            merged[column] = pd.to_numeric(merged[column], errors="coerce")
        merged = merged.dropna(subset=["timestamp", "iv", "close"]).sort_values("timestamp").drop_duplicates(
            subset=["timestamp"],
            keep="last",
        ).reset_index(drop=True)
        return merged

    def fetch_expired_option_data(
        self,
        security_id,
        exchange_segment,
        option_type="CALL",
        strike="ATM",
        from_date=None,
        to_date=None
    ):
        """
        Fetch expired/rolling ATM option data to evaluate how similar low-IV regimes behaved.
        """
        end_date = pd.to_datetime(to_date).date() if to_date else datetime.now().date()
        start_date = pd.to_datetime(from_date).date() if from_date else (end_date - timedelta(days=30))
        cache_key = (
            str(security_id),
            exchange_segment,
            option_type,
            strike,
            start_date.isoformat(),
            end_date.isoformat(),
        )
        if cache_key in self.expired_data_cache:
            logger.info(
                "Using cached expired option data for %s (%s) %s %s",
                security_id,
                exchange_segment,
                option_type,
                strike,
            )
            return self.expired_data_cache[cache_key].copy()

        cache_path = self._expired_options_cache_path(security_id, exchange_segment, option_type, strike)
        persisted_df = self._load_expired_option_cache(cache_path)
        if not persisted_df.empty:
            logger.info(
                "Loaded persisted expired option cache for %s (%s) %s %s: %s rows",
                security_id,
                exchange_segment,
                option_type,
                strike,
                len(persisted_df),
            )

        fetch_from_date = start_date
        if not persisted_df.empty:
            last_cached_timestamp = persisted_df["timestamp"].max()
            if pd.notna(last_cached_timestamp):
                fetch_from_date = max(start_date, last_cached_timestamp.date())

        if not persisted_df.empty and fetch_from_date >= end_date:
            filtered_df = persisted_df[
                (persisted_df["timestamp"].dt.date >= start_date) &
                (persisted_df["timestamp"].dt.date <= end_date)
            ].reset_index(drop=True)
            self.expired_data_cache[cache_key] = filtered_df.copy()
            logger.info(
                "Using persisted expired option cache without API call for %s (%s) %s %s",
                security_id,
                exchange_segment,
                option_type,
                strike,
            )
            return filtered_df.copy()

        instrument_type = "OPTIDX" if exchange_segment == "IDX_I" else "OPTSTK"
        required_data = ["close", "iv", "volume", "spot"]

        try:
            expired_method = getattr(self.dhan, "expired_options_data", None)
            if callable(expired_method):
                response = expired_method(
                    security_id=security_id,
                    exchange_segment=exchange_segment,
                    instrument_type=instrument_type,
                    expiry_flag="MONTH",
                    expiry_code=1,
                    strike=strike,
                    drv_option_type=option_type,
                    required_data=required_data,
                    from_date=fetch_from_date.isoformat(),
                    to_date=end_date.isoformat(),
                    interval=15,
                )
            else:
                response = requests.post(
                    "https://api.dhan.co/v2/charts/rollingoption",
                    headers={
                        "access-token": self.access_token,
                        "Content-Type": "application/json",
                    },
                    json={
                        "securityId": security_id,
                        "exchangeSegment": exchange_segment,
                        "instrument": instrument_type,
                        "expiryFlag": "MONTH",
                        "expiryCode": 1,
                        "strike": strike,
                        "drvOptionType": option_type,
                        "requiredData": required_data,
                        "fromDate": fetch_from_date.isoformat(),
                        "toDate": end_date.isoformat(),
                        "interval": 15,
                    },
                    timeout=20,
                ).json()
        except Exception:
            logger.exception(
                "Failed to fetch expired option data for %s (%s) %s",
                security_id,
                exchange_segment,
                option_type,
            )
            fallback_df = persisted_df[
                (persisted_df["timestamp"].dt.date >= start_date) &
                (persisted_df["timestamp"].dt.date <= end_date)
            ].reset_index(drop=True) if not persisted_df.empty else pd.DataFrame(columns=["timestamp", "iv", "close", "volume", "spot"])
            self.expired_data_cache[cache_key] = fallback_df.copy()
            return fallback_df

        if response.get("status") != "success":
            logger.warning(
                "Expired option data fetch failed for %s (%s) %s: %s",
                security_id,
                exchange_segment,
                option_type,
                response,
            )
            fallback_df = persisted_df[
                (persisted_df["timestamp"].dt.date >= start_date) &
                (persisted_df["timestamp"].dt.date <= end_date)
            ].reset_index(drop=True) if not persisted_df.empty else pd.DataFrame(columns=["timestamp", "iv", "close", "volume", "spot"])
            self.expired_data_cache[cache_key] = fallback_df.copy()
            return fallback_df

        payload = unwrap_dhan_payload(response.get("data") or {})
        side_key = "ce" if option_type == "CALL" else "pe"
        option_payload = payload.get(side_key) if isinstance(payload, dict) else None

        if isinstance(option_payload, dict):
            target_columns = ["timestamp", "iv", "close", "volume", "spot"]
            normalized_payload = {
                column: option_payload.get(column, [])
                for column in target_columns
                if isinstance(option_payload.get(column, []), list) and len(option_payload.get(column, [])) > 0
            }
            df = pd.DataFrame(normalized_payload)
        elif isinstance(payload, dict):
            df = pd.DataFrame(payload)
        elif isinstance(response.get("data"), list):
            df = pd.DataFrame(response.get("data"))
        else:
            df = pd.DataFrame()

        if df.empty:
            merged_df = persisted_df.copy()
            filtered_df = merged_df[
                (merged_df["timestamp"].dt.date >= start_date) &
                (merged_df["timestamp"].dt.date <= end_date)
            ].reset_index(drop=True) if not merged_df.empty else pd.DataFrame(columns=["timestamp", "iv", "close", "volume", "spot"])
            self.expired_data_cache[cache_key] = filtered_df.copy()
            if filtered_df.empty:
                logger.info(
                    "Expired option data returned no rows for %s (%s) %s",
                    security_id,
                    exchange_segment,
                    option_type,
                )
            else:
                logger.info(
                    "API returned no new rows; using persisted expired option cache for %s (%s) %s: %s rows",
                    security_id,
                    exchange_segment,
                    option_type,
                    len(filtered_df),
                )
            return filtered_df

        timestamp_col = None
        for candidate in ("timestamp", "start_Time", "start_time", "date", "Date"):
            if candidate in df.columns:
                timestamp_col = candidate
                break

        if timestamp_col:
            series = df[timestamp_col]
            if pd.api.types.is_numeric_dtype(series):
                df["timestamp"] = pd.to_datetime(series, unit="s", errors="coerce")
            else:
                df["timestamp"] = pd.to_datetime(series, errors="coerce")
        else:
            df["timestamp"] = pd.NaT

        for column in ("iv", "close", "volume", "spot"):
            df[column] = pd.to_numeric(df.get(column), errors="coerce")

        df = df[["timestamp", "iv", "close", "volume", "spot"]]
        df = df.dropna(subset=["timestamp", "iv", "close"]).sort_values("timestamp").reset_index(drop=True)
        merged_df = self._merge_expired_option_frames(persisted_df, df)
        self._save_expired_option_cache(cache_path, merged_df)
        filtered_df = merged_df[
            (merged_df["timestamp"].dt.date >= start_date) &
            (merged_df["timestamp"].dt.date <= end_date)
        ].reset_index(drop=True)
        self.expired_data_cache[cache_key] = filtered_df.copy()
        logger.info(
            "Fetched expired option data for %s (%s) %s: %s new rows | %s cached rows from %s to %s",
            security_id,
            exchange_segment,
            option_type,
            len(df),
            len(filtered_df),
            start_date.isoformat(),
            end_date.isoformat(),
        )
        return filtered_df

    def compute_iv_behavior_metrics(self, df):
        """
        Measure whether similar low-IV states historically led to expansion in option prices.
        """
        if df is None or df.empty or "iv" not in df.columns or "close" not in df.columns:
            return None

        metrics_df = df.copy()
        metrics_df["iv"] = pd.to_numeric(metrics_df["iv"], errors="coerce")
        metrics_df["close"] = pd.to_numeric(metrics_df["close"], errors="coerce")
        metrics_df = metrics_df.dropna(subset=["iv", "close"]).reset_index(drop=True)
        if len(metrics_df) < 10:
            return None

        metrics_df["forward_return_1"] = (metrics_df["close"].shift(-1) - metrics_df["close"]) / metrics_df["close"]
        metrics_df["forward_return_3"] = (metrics_df["close"].shift(-3) - metrics_df["close"]) / metrics_df["close"]
        low_iv_threshold = float(np.percentile(metrics_df["iv"], 20))
        low_iv_rows = metrics_df[metrics_df["iv"] < low_iv_threshold]
        avg_move_after_low_iv = float(low_iv_rows["forward_return_3"].dropna().mean()) if not low_iv_rows.empty else None
        current_iv = float(metrics_df["iv"].iloc[-1])
        iv_percentile = float((metrics_df["iv"] < current_iv).mean() * 100)

        return {
            "iv_percentile": iv_percentile,
            "avg_move_after_low_iv": avg_move_after_low_iv,
            "low_iv_threshold": low_iv_threshold,
        }
    
    # ==================== 2. VOLATILITY CALCULATIONS ====================
    
    def calculate_historical_volatility(self, price_df, window=20):
        """
        Calculate historical/realized volatility
        
        Args:
            price_df: DataFrame with 'close' prices
            window: Lookback window in days
        
        Returns:
            float: Annualized volatility percentage
        """
        if price_df.empty or len(price_df) < window:
            return None
        
        # Calculate daily log returns
        log_returns = np.log(price_df['close'] / price_df['close'].shift(1))
        
        # Rolling volatility
        rolling_std = log_returns.rolling(window=window).std()
        
        # Annualize (252 trading days)
        hist_vol = rolling_std.iloc[-1] * np.sqrt(252) * 100
        
        return hist_vol

    def calculate_hv_metrics(self, price_df):
        """Build a multi-window HV view to reduce single-window noise."""
        hv10 = self.calculate_historical_volatility(price_df, window=10)
        hv20 = self.calculate_historical_volatility(price_df, window=20)
        hv60 = self.calculate_historical_volatility(price_df, window=60)

        available = [value for value in [hv10, hv20, hv60] if value is not None and not pd.isna(value)]
        weighted_hv = None
        if available:
            weights = {"hv10": 0.3, "hv20": 0.4, "hv60": 0.3}
            total_weight = 0.0
            weighted_sum = 0.0
            for key, value in {"hv10": hv10, "hv20": hv20, "hv60": hv60}.items():
                if value is None or pd.isna(value):
                    continue
                weighted_sum += value * weights[key]
                total_weight += weights[key]
            weighted_hv = weighted_sum / total_weight if total_weight else np.mean(available)

        return {
            "hv10": hv10,
            "hv20": hv20,
            "hv60": hv60,
            "weighted_hv": weighted_hv,
        }
    
    def calculate_iv_percentile(self, current_iv, historical_ivs):
        """
        Calculate IV percentile from historical data
        
        Args:
            current_iv: Current implied volatility
            historical_ivs: List of historical IV values
        
        Returns:
            float: Percentile (0-100)
        """
        if not historical_ivs:
            return 50  # Default if no history
        historical = np.array(historical_ivs, dtype=float)
        return (historical < current_iv).mean() * 100
    
    def calculate_iv_rank(self, current_iv, historical_ivs):
        """
        Calculate IV Rank: (current - min) / (max - min) * 100
        
        Args:
            current_iv: Current implied volatility
            historical_ivs: List of historical IV values
        
        Returns:
            float: IV Rank (0-100)
        """
        if not historical_ivs:
            return 50
        
        min_iv = min(historical_ivs)
        max_iv = max(historical_ivs)
        
        if max_iv == min_iv:
            return 50
        
        iv_rank = (current_iv - min_iv) / (max_iv - min_iv) * 100
        return clip_score(iv_rank)

    def determine_trend_context(self, price_df):
        """Simple market context based on EMA structure."""
        if price_df.empty or "close" not in price_df.columns or len(price_df) < 50:
            return {"trend": "neutral", "ema20": None, "ema50": None}

        closes = price_df["close"].astype(float)
        ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = closes.ewm(span=50, adjust=False).mean().iloc[-1]
        last_close = closes.iloc[-1]

        if last_close > ema20 > ema50:
            trend = "bullish"
        elif last_close < ema20 < ema50:
            trend = "bearish"
        else:
            trend = "neutral"

        return {"trend": trend, "ema20": ema20, "ema50": ema50, "last_close": last_close}

    def days_to_expiry(self, expiry):
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        return max((expiry_date - datetime.now().date()).days, 1)

    def compute_expected_move(self, spot_price, reference_iv, dte):
        if spot_price is None or reference_iv is None or reference_iv <= 0 or dte <= 0:
            return None
        return spot_price * (reference_iv / 100.0) * math.sqrt(dte / 365.0)

    def extract_chain_metrics(self, option_chain):
        metrics = {
            "total_call_oi": 0.0,
            "total_put_oi": 0.0,
            "total_oi": 0.0,
            "total_call_volume": 0.0,
            "total_put_volume": 0.0,
            "max_oi_strike_call": None,
            "max_oi_strike_put": None,
        }
        max_call_oi = -1.0
        max_put_oi = -1.0

        for strike_key, strike_data in (option_chain or {}).items():
            if not isinstance(strike_data, dict):
                continue

            strike_price = pd.to_numeric(strike_key, errors="coerce")
            if pd.isna(strike_price):
                continue
            strike_price = float(strike_price)

            call_opt = strike_data.get("ce") or {}
            put_opt = strike_data.get("pe") or {}

            call_oi = pd.to_numeric(call_opt.get("oi"), errors="coerce")
            put_oi = pd.to_numeric(put_opt.get("oi"), errors="coerce")
            call_volume = pd.to_numeric(call_opt.get("volume"), errors="coerce")
            put_volume = pd.to_numeric(put_opt.get("volume"), errors="coerce")

            if pd.notna(call_oi) and call_oi > 0:
                call_oi = float(call_oi)
                metrics["total_call_oi"] += call_oi
                metrics["total_oi"] += call_oi
                if call_oi > max_call_oi:
                    max_call_oi = call_oi
                    metrics["max_oi_strike_call"] = strike_price
            if pd.notna(put_oi) and put_oi > 0:
                put_oi = float(put_oi)
                metrics["total_put_oi"] += put_oi
                metrics["total_oi"] += put_oi
                if put_oi > max_put_oi:
                    max_put_oi = put_oi
                    metrics["max_oi_strike_put"] = strike_price
            if pd.notna(call_volume) and call_volume > 0:
                metrics["total_call_volume"] += float(call_volume)
            if pd.notna(put_volume) and put_volume > 0:
                metrics["total_put_volume"] += float(put_volume)

        oi_walls = self.find_oi_walls(option_chain)
        metrics["call_walls"] = oi_walls["call_walls"]
        metrics["put_walls"] = oi_walls["put_walls"]
        metrics["call_wall_threshold"] = oi_walls.get("call_threshold")
        metrics["put_wall_threshold"] = oi_walls.get("put_threshold")
        return metrics

    def find_oi_walls(self, option_chain):
        ordered_rows = []
        call_oi_list = []
        put_oi_list = []
        for strike_key, strike_data in (option_chain or {}).items():
            if not isinstance(strike_data, dict):
                continue
            strike_price = pd.to_numeric(strike_key, errors="coerce")
            if pd.isna(strike_price):
                continue
            call_oi = native_number(pd.to_numeric((strike_data.get("ce") or {}).get("oi"), errors="coerce")) or 0.0
            put_oi = native_number(pd.to_numeric((strike_data.get("pe") or {}).get("oi"), errors="coerce")) or 0.0
            ordered_rows.append({
                "strike": float(strike_price),
                "call_oi": call_oi,
                "put_oi": put_oi,
            })
            call_oi_list.append(call_oi)
            put_oi_list.append(put_oi)

        ordered_rows.sort(key=lambda item: item["strike"])
        call_threshold = float(np.percentile(call_oi_list, 90)) if call_oi_list else None
        put_threshold = float(np.percentile(put_oi_list, 90)) if put_oi_list else None
        call_walls = []
        put_walls = []
        for current_row in ordered_rows:
            if call_threshold is not None and current_row["call_oi"] > 0 and current_row["call_oi"] >= call_threshold:
                call_walls.append({
                    "strike": current_row["strike"],
                    "oi": current_row["call_oi"],
                })
            if put_threshold is not None and current_row["put_oi"] > 0 and current_row["put_oi"] >= put_threshold:
                put_walls.append({
                    "strike": current_row["strike"],
                    "oi": current_row["put_oi"],
                })

        return {
            "call_walls": call_walls,
            "put_walls": put_walls,
            "call_threshold": native_number(call_threshold),
            "put_threshold": native_number(put_threshold),
        }

    def compute_buildup_from_state(self, security_id, spot_price, chain_metrics):
        previous_state = self.get_previous_state_store()
        state_key = str(security_id)
        current_total_oi = native_number(chain_metrics.get("total_oi")) or 0.0
        current_spot = native_number(spot_price)
        now_iso = datetime.now().isoformat()
        previous = previous_state.get(state_key)

        result = {
            "type": "NEUTRAL",
            "strength": 0.0,
            "price_change": None,
            "price_change_pct": None,
            "oi_change": None,
            "oi_change_pct": None,
            "previous_spot": native_number((previous or {}).get("spot")),
            "previous_total_oi": native_number((previous or {}).get("total_oi")),
            "timestamp": now_iso,
        }

        if previous is not None and current_spot is not None:
            previous_spot = pd.to_numeric(previous.get("spot"), errors="coerce")
            previous_total_oi = pd.to_numeric(previous.get("total_oi"), errors="coerce")
            if pd.notna(previous_spot) and pd.notna(previous_total_oi):
                price_change = float(current_spot - previous_spot)
                oi_change = float(current_total_oi - previous_total_oi)
                price_change_pct = ((price_change / previous_spot) * 100.0) if previous_spot else 0.0
                oi_change_pct = ((oi_change / previous_total_oi) * 100.0) if previous_total_oi else 0.0

                if price_change > 0 and oi_change > 0:
                    buildup_type = "LONG_BUILDUP"
                elif price_change < 0 and oi_change > 0:
                    buildup_type = "SHORT_BUILDUP"
                elif price_change > 0 and oi_change < 0:
                    buildup_type = "SHORT_COVERING"
                elif price_change < 0 and oi_change < 0:
                    buildup_type = "LONG_UNWINDING"
                else:
                    buildup_type = "NEUTRAL"

                strength = clip_score((abs(price_change_pct) * 12.0) + (abs(oi_change_pct) * 8.0), floor=0.0, ceiling=100.0)
                result.update({
                    "type": buildup_type,
                    "strength": round(strength, 2),
                    "price_change": native_number(price_change),
                    "price_change_pct": native_number(price_change_pct),
                    "oi_change": native_number(oi_change),
                    "oi_change_pct": native_number(oi_change_pct),
                })

        previous_state[state_key] = {
            "spot": current_spot,
            "total_oi": current_total_oi,
            "timestamp": now_iso,
        }
        return result

    def compute_buildup_from_option(self, opt):
        oi = native_number(pd.to_numeric((opt or {}).get("oi"), errors="coerce")) or 0.0
        previous_oi = native_number(pd.to_numeric((opt or {}).get("previous_oi"), errors="coerce")) or 0.0
        last_price = native_number(pd.to_numeric((opt or {}).get("last_price"), errors="coerce")) or 0.0
        previous_close_price = native_number(pd.to_numeric((opt or {}).get("previous_close_price"), errors="coerce")) or 0.0

        oi_change = oi - previous_oi
        price_change = last_price - previous_close_price

        if price_change > 0 and oi_change > 0:
            buildup_type = "LONG_BUILDUP"
        elif price_change < 0 and oi_change > 0:
            buildup_type = "SHORT_BUILDUP"
        elif price_change > 0 and oi_change < 0:
            buildup_type = "SHORT_COVERING"
        elif price_change < 0 and oi_change < 0:
            buildup_type = "LONG_UNWINDING"
        else:
            buildup_type = "NEUTRAL"

        price_change_pct = (price_change / previous_close_price * 100.0) if previous_close_price else 0.0
        oi_change_pct = (oi_change / previous_oi * 100.0) if previous_oi else 0.0
        strength = clip_score((abs(price_change_pct) * 12.0) + (abs(oi_change_pct) * 8.0), floor=0.0, ceiling=100.0)

        return {
            "type": buildup_type,
            "strength": round(strength, 2),
            "price_change": native_number(price_change),
            "price_change_pct": native_number(price_change_pct),
            "oi_change": native_number(oi_change),
            "oi_change_pct": native_number(oi_change_pct),
            "previous_oi": native_number(previous_oi),
            "previous_close_price": native_number(previous_close_price),
        }

    def build_buildup_distribution(self, option_chain):
        distribution = {}
        for strike_data in (option_chain or {}).values():
            if not isinstance(strike_data, dict):
                continue
            for option_type in ("ce", "pe"):
                if option_type not in strike_data:
                    continue
                buildup_type = self.compute_buildup_from_option(strike_data[option_type]).get("type", "NEUTRAL")
                distribution[buildup_type] = distribution.get(buildup_type, 0) + 1
        return distribution

    def find_nearest_oi_wall(self, spot_price, walls):
        if spot_price is None or not walls:
            return None
        nearest = min(
            walls,
            key=lambda wall: abs((native_number(wall.get("strike")) or 0.0) - spot_price),
        )
        strike = native_number(nearest.get("strike"))
        if strike is None:
            return None
        return {
            "strike": strike,
            "oi": native_number(nearest.get("oi")),
            "distance": abs(strike - spot_price),
        }

    def is_near_oi_wall(self, strike_price, walls, max_distance=NEAR_WALL_STRIKE_DISTANCE):
        if strike_price is None or not walls:
            return False, None
        nearest = self.find_nearest_oi_wall(strike_price, walls)
        if not nearest:
            return False, None
        return nearest["distance"] <= max_distance, nearest

    def extract_atm_reference_ivs(self, option_chain, spot_price):
        if not option_chain:
            return {
                "atm_strike": None,
                "atm_call_iv": None,
                "atm_put_iv": None,
                "atm_iv": None,
                "atm_call_oi": None,
                "atm_put_oi": None,
            }

        atm_strike = min(option_chain.keys(), key=lambda strike: abs(float(strike) - spot_price))
        atm_data = option_chain.get(atm_strike, {})
        atm_call_iv = (atm_data.get("ce") or {}).get("implied_volatility") or None
        atm_put_iv = (atm_data.get("pe") or {}).get("implied_volatility") or None
        atm_call_oi = pd.to_numeric((atm_data.get("ce") or {}).get("oi"), errors="coerce")
        atm_put_oi = pd.to_numeric((atm_data.get("pe") or {}).get("oi"), errors="coerce")
        valid = [value for value in [atm_call_iv, atm_put_iv] if value and value > 0]
        atm_iv = float(np.mean(valid)) if valid else None
        return {
            "atm_strike": float(atm_strike),
            "atm_call_iv": atm_call_iv,
            "atm_put_iv": atm_put_iv,
            "atm_iv": atm_iv,
            "atm_call_oi": native_number(atm_call_oi),
            "atm_put_oi": native_number(atm_put_oi),
        }

    def persist_iv_snapshot(self, security_id, exchange_segment, security_name, expiry, spot_price, atm_context,
                            chain_metrics=None, store_intraday=None):
        """Persist one ATM IV snapshot per day to build IV rank / percentile history."""
        atm_iv = atm_context.get("atm_iv")
        if atm_iv is None or atm_iv <= 0 or atm_iv < 1 or atm_iv > 200:
            return

        store_intraday = self.store_intraday if store_intraday is None else store_intraday
        snapshot_dt = datetime.now()
        data_type = "intraday" if store_intraday else "daily"
        chain_metrics = chain_metrics or {}

        snapshot = pd.DataFrame([{
            "snapshot_date": snapshot_dt.date().isoformat(),
            "snapshot_time": snapshot_dt.strftime("%H:%M:%S"),
            "security_id": str(security_id),
            "symbol": security_name,
            "spot_price": spot_price,
            "atm_strike": atm_context.get("atm_strike"),
            "atm_iv": atm_iv,
            "atm_call_iv": atm_context.get("atm_call_iv"),
            "atm_put_iv": atm_context.get("atm_put_iv"),
            "atm_call_oi": atm_context.get("atm_call_oi"),
            "atm_put_oi": atm_context.get("atm_put_oi"),
            "total_call_oi": chain_metrics.get("total_call_oi"),
            "total_put_oi": chain_metrics.get("total_put_oi"),
            "total_call_volume": chain_metrics.get("total_call_volume"),
            "total_put_volume": chain_metrics.get("total_put_volume"),
            "max_oi_strike_call": chain_metrics.get("max_oi_strike_call"),
            "max_oi_strike_put": chain_metrics.get("max_oi_strike_put"),
        }])
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            ensure_iv_history_schema(cursor)
            cursor.execute("""
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
                str(security_id),
                security_name,
                f"{snapshot_dt.date().isoformat()} {snapshot_dt.strftime('%H:%M:%S')}",
                spot_price,
                atm_context.get("atm_strike"),
                atm_iv,
                atm_context.get("atm_call_iv"),
                atm_context.get("atm_put_iv"),
                atm_context.get("atm_call_oi"),
                atm_context.get("atm_put_oi"),
                chain_metrics.get("total_call_oi"),
                chain_metrics.get("total_put_oi"),
                chain_metrics.get("total_call_volume"),
                chain_metrics.get("total_put_volume"),
                chain_metrics.get("max_oi_strike_call"),
                chain_metrics.get("max_oi_strike_put"),
                data_type,
            ))
            conn.commit()
            self.runtime_state["metrics"]["iv_snapshots"] += 1
        except Exception:
            logger.exception("Failed to persist IV snapshot to SQLite: %s", DB_PATH)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_intraday_snapshots(self, security_id, limit=5):
        try:
            conn = sqlite3.connect(DB_PATH)
            query = """
            SELECT
                security_id, symbol, timestamp, spot_price, atm_strike, atm_iv,
                atm_call_iv, atm_put_iv, atm_call_oi, atm_put_oi,
                total_call_oi, total_put_oi, total_call_volume, total_put_volume,
                max_oi_strike_call, max_oi_strike_put
            FROM iv_history
            WHERE security_id = ?
            AND data_type = 'intraday'
            AND DATE(timestamp) = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """
            df = pd.read_sql(
                query,
                conn,
                params=(str(security_id), datetime.now().date().isoformat(), int(limit)),
            )
        except Exception:
            logger.exception("Failed to read intraday IV history for %s", security_id)
            return pd.DataFrame()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if df.empty:
            return df

        numeric_cols = [
            "spot_price", "atm_strike", "atm_iv", "atm_call_iv", "atm_put_iv",
            "atm_call_oi", "atm_put_oi", "total_call_oi", "total_put_oi",
            "total_call_volume", "total_put_volume", "max_oi_strike_call", "max_oi_strike_put",
        ]
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        for column in numeric_cols:
            if column in df.columns:
                df[column] = pd.to_numeric(df[column], errors="coerce")
        df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        return df

    def get_oi_buildup(self, security_id):
        previous_state = self.get_previous_state_store().get(str(security_id)) or {}
        return {
            "type": str(previous_state.get("type") or "NEUTRAL"),
            "strength": native_number(previous_state.get("strength")) or 0.0,
            "price_change": native_number(previous_state.get("price_change")),
            "price_change_pct": native_number(previous_state.get("price_change_pct")),
            "oi_change": native_number(previous_state.get("oi_change")),
            "oi_change_pct": native_number(previous_state.get("oi_change_pct")),
            "previous_spot": native_number(previous_state.get("previous_spot")),
            "previous_total_oi": native_number(previous_state.get("previous_total_oi")),
            "timestamp": previous_state.get("timestamp"),
        }

    def get_pcr_trend(self, security_id):
        snapshots = self.get_intraday_snapshots(security_id, limit=5)
        if snapshots.empty:
            return {"current_pcr": None, "trend": "neutral"}

        pcr_series = []
        for _, row in snapshots.iterrows():
            call_oi = pd.to_numeric(row.get("total_call_oi"), errors="coerce")
            put_oi = pd.to_numeric(row.get("total_put_oi"), errors="coerce")
            if pd.notna(call_oi) and call_oi > 0 and pd.notna(put_oi):
                pcr_series.append(float(put_oi / call_oi))

        if not pcr_series:
            return {"current_pcr": None, "trend": "neutral"}

        current_pcr = pcr_series[-1]
        slope = float(np.polyfit(np.arange(len(pcr_series), dtype=float), np.array(pcr_series, dtype=float), 1)[0]) if len(pcr_series) >= 2 else 0.0

        if current_pcr >= 1.05 and slope > 0.01:
            trend = "bullish"
        elif current_pcr <= 0.95 and slope < -0.01:
            trend = "bearish"
        else:
            trend = "neutral"

        return {
            "current_pcr": round(current_pcr, 3),
            "trend": trend,
            "slope": round(slope, 4),
        }

    def detect_oi_shift(self, security_id):
        snapshots = self.get_intraday_snapshots(security_id, limit=2)
        if len(snapshots) < 2:
            return {"call_shift": "same", "put_shift": "same"}

        previous = snapshots.iloc[-2]
        latest = snapshots.iloc[-1]

        def compare_shift(previous_value, latest_value):
            previous_value = pd.to_numeric(previous_value, errors="coerce")
            latest_value = pd.to_numeric(latest_value, errors="coerce")
            if pd.isna(previous_value) or pd.isna(latest_value):
                return "same"
            if latest_value > previous_value:
                return "up"
            if latest_value < previous_value:
                return "down"
            return "same"

        return {
            "call_shift": compare_shift(previous.get("max_oi_strike_call"), latest.get("max_oi_strike_call")),
            "put_shift": compare_shift(previous.get("max_oi_strike_put"), latest.get("max_oi_strike_put")),
            "previous_call_strike": native_number(previous.get("max_oi_strike_call")),
            "latest_call_strike": native_number(latest.get("max_oi_strike_call")),
            "previous_put_strike": native_number(previous.get("max_oi_strike_put")),
            "latest_put_strike": native_number(latest.get("max_oi_strike_put")),
        }

    def detect_volume_spike(self, security_id):
        snapshots = self.get_intraday_snapshots(security_id, limit=4)
        if len(snapshots) < 2:
            return {"spike": False, "ratio": None, "direction": "neutral"}

        latest = snapshots.iloc[-1]
        history = snapshots.iloc[:-1]
        latest_total = 0.0
        for column in ("total_call_volume", "total_put_volume"):
            value = pd.to_numeric(latest.get(column), errors="coerce")
            if pd.notna(value):
                latest_total += float(value)
        historical_totals = []
        for _, row in history.iterrows():
            total = 0.0
            for column in ("total_call_volume", "total_put_volume"):
                value = pd.to_numeric(row.get(column), errors="coerce")
                if pd.notna(value):
                    total += float(value)
            if total > 0:
                historical_totals.append(total)

        if not historical_totals:
            return {"spike": False, "ratio": None, "direction": "neutral"}

        avg_total = float(np.mean(historical_totals))
        ratio = (latest_total / avg_total) if avg_total > 0 else None
        call_volume = pd.to_numeric(latest.get("total_call_volume"), errors="coerce")
        put_volume = pd.to_numeric(latest.get("total_put_volume"), errors="coerce")
        if pd.notna(call_volume) and pd.notna(put_volume):
            direction = "bullish" if put_volume > call_volume else "bearish" if call_volume > put_volume else "neutral"
        else:
            direction = "neutral"

        return {
            "spike": bool(ratio is not None and ratio > 1.5),
            "ratio": round(ratio, 2) if ratio is not None else None,
            "direction": direction,
        }

    def build_market_signal(self, security_id, spot_price=None, chain_metrics=None, buildup=None):
        buildup = buildup or {"type": "NEUTRAL", "strength": 0.0}
        pcr_trend = self.get_pcr_trend(security_id)
        oi_shift = self.detect_oi_shift(security_id)
        volume_spike = self.detect_volume_spike(security_id)
        chain_metrics = chain_metrics or {}
        nearest_put_wall = self.find_nearest_oi_wall(spot_price, chain_metrics.get("put_walls") or [])
        nearest_call_wall = self.find_nearest_oi_wall(spot_price, chain_metrics.get("call_walls") or [])

        bullish_score = 0.0
        bearish_score = 0.0

        buildup_type = buildup.get("type")
        if buildup_type in {"LONG_BUILDUP", "SHORT_COVERING"}:
            bullish_score += 1.5 + (buildup.get("strength", 0.0) / 100.0)
        elif buildup_type in {"SHORT_BUILDUP", "LONG_UNWINDING"}:
            bearish_score += 1.5 + (buildup.get("strength", 0.0) / 100.0)

        if pcr_trend.get("trend") == "bullish":
            bullish_score += 1.25
        elif pcr_trend.get("trend") == "bearish":
            bearish_score += 1.25

        if oi_shift.get("call_shift") == "up":
            bearish_score += 0.75
        elif oi_shift.get("call_shift") == "down":
            bullish_score += 0.4
        if oi_shift.get("put_shift") == "up":
            bullish_score += 0.75
        elif oi_shift.get("put_shift") == "down":
            bearish_score += 0.4

        if volume_spike.get("spike"):
            if volume_spike.get("direction") == "bullish":
                bullish_score += 0.8
            elif volume_spike.get("direction") == "bearish":
                bearish_score += 0.8

        bias_from_wall = "neutral"
        near_put_wall = False
        near_call_wall = False
        wall_threshold = None
        if spot_price is not None:
            wall_threshold = NEAR_WALL_STRIKE_DISTANCE
        if nearest_put_wall and wall_threshold is not None and nearest_put_wall["distance"] <= wall_threshold:
            bullish_score += 1.5
            near_put_wall = True
            bias_from_wall = "bullish"
        if nearest_call_wall and wall_threshold is not None and nearest_call_wall["distance"] <= wall_threshold:
            bearish_score += 1.5
            near_call_wall = True
            bias_from_wall = "bearish" if bias_from_wall == "neutral" else bias_from_wall

        market_strength = "NEUTRAL"
        if buildup_type == "LONG_BUILDUP" and near_put_wall:
            bullish_score += 2.0
            market_strength = "STRONG_BULLISH"
        elif buildup_type == "SHORT_BUILDUP" and near_call_wall:
            bearish_score += 2.0
            market_strength = "STRONG_BEARISH"

        score_gap = bullish_score - bearish_score
        if score_gap > 0.75:
            direction = "bullish"
        elif score_gap < -0.75:
            direction = "bearish"
        else:
            direction = "neutral"

        if market_strength == "NEUTRAL":
            if direction == "bullish":
                market_strength = "BULLISH"
            elif direction == "bearish":
                market_strength = "BEARISH"

        confidence = clip_score(50 + abs(score_gap) * 18, floor=35.0, ceiling=95.0)
        return {
            "direction": direction,
            "confidence": round(confidence, 1),
            "strength": market_strength if direction != "neutral" else "NEUTRAL",
            "components": {
                "buildup": buildup,
                "pcr_trend": pcr_trend,
                "oi_shift": oi_shift,
                "volume_spike": volume_spike,
                "oi_walls": {
                    "call_walls": chain_metrics.get("call_walls") or [],
                    "put_walls": chain_metrics.get("put_walls") or [],
                    "nearest_call_wall": nearest_call_wall,
                    "nearest_put_wall": nearest_put_wall,
                    "bias_from_wall": bias_from_wall,
                    "near_call_wall": near_call_wall,
                    "near_put_wall": near_put_wall,
                },
            },
        }

    def build_premarket_context(self, security_id):
        try:
            df = self.get_intraday_snapshots(security_id, limit=12)
            if df.empty or len(df) < 2:
                return None

            iv_series = pd.to_numeric(df["atm_iv"], errors="coerce").dropna().tolist()
            if len(iv_series) < 2:
                return None

            opening_iv = df.iloc[0]["atm_iv"]
            current_iv = df.iloc[-1]["atm_iv"]
            iv_trend = float(np.polyfit(np.arange(len(iv_series), dtype=float), np.array(iv_series, dtype=float), 1)[0])

            return {
                # Warmup context now captures both absolute change and the intraday IV slope.
                "iv_change": current_iv - opening_iv,
                "iv_trend": iv_trend,
                "current_pcr": native_number((self.get_pcr_trend(security_id) or {}).get("current_pcr")),
            }
        except Exception:
            return None

    def log_option_rejection(self, strike_price, option_type, reason, **details):
        """Emit readable filter diagnostics for rejected options."""
        clean_details = {
            key: value for key, value in details.items()
            if value is not None and not (isinstance(value, float) and pd.isna(value))
        }
        detail_suffix = ""
        if clean_details:
            detail_suffix = " | " + " | ".join(f"{key}={value}" for key, value in clean_details.items())
        logger.info(
            "Rejected option | strike=%.2f | type=%s | reason=%s%s",
            float(strike_price),
            str(option_type).upper(),
            reason,
            detail_suffix,
        )

    def get_execution_prices(self, opt):
        """Build realistic entry/exit references from the quoted spread."""
        last_price = native_number(opt.get("last_price", 0)) or 0.0
        raw_bid = native_number(opt.get("top_bid_price", 0))
        raw_ask = native_number(opt.get("top_ask_price", 0))

        bid = raw_bid if raw_bid and raw_bid > 0 else None
        ask = raw_ask if raw_ask and raw_ask > 0 else None
        entry_price = ask if ask is not None else last_price
        exit_price = bid if bid is not None else last_price

        if ask is not None and bid is not None:
            mid_price = (ask * 0.7) + (bid * 0.3)
        elif ask is not None:
            mid_price = ask
        elif bid is not None:
            mid_price = bid
        else:
            mid_price = last_price

        return {
            "bid": bid if bid is not None else last_price,
            "ask": ask if ask is not None else last_price,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "mid_price": mid_price,
        }

    def get_neighboring_ivs(self, option_chain, strike_price, option_type):
        """Read adjacent-strike IVs from the in-memory chain only."""
        strike_keys = sorted(float(key) for key in option_chain.keys())
        try:
            strike_index = strike_keys.index(float(strike_price))
        except ValueError:
            return []

        neighboring_ivs = []
        for neighbor_index in [strike_index - 1, strike_index + 1]:
            if neighbor_index < 0 or neighbor_index >= len(strike_keys):
                continue
            neighbor_strike = strike_keys[neighbor_index]
            neighbor_data = option_chain.get(str(neighbor_strike), option_chain.get(neighbor_strike, {}))
            neighbor_option = (neighbor_data or {}).get(option_type) or {}
            neighbor_iv = pd.to_numeric(neighbor_option.get("implied_volatility"), errors="coerce")
            if pd.notna(neighbor_iv) and neighbor_iv > 0:
                neighboring_ivs.append(float(neighbor_iv))
        return neighboring_ivs

    def is_iv_stable(self, option_chain, strike_price, option_type, current_iv):
        """Check whether IV is aligned with neighboring strikes within a 10% band."""
        neighboring_ivs = self.get_neighboring_ivs(option_chain, strike_price, option_type)
        if not neighboring_ivs:
            return True, None

        neighbor_reference = float(np.mean(neighboring_ivs))
        if neighbor_reference <= 0:
            return True, neighbor_reference

        deviation = abs(float(current_iv) - neighbor_reference) / neighbor_reference
        return deviation <= 0.10, deviation

    def classify_trade_type(self, iv_rank, skew_discount, iv_trend, trend, abs_delta, expected_move_ratio):
        """Keep directional and volatility trade definitions strictly separated."""
        is_volatility_trade = (
            ((iv_rank is not None and iv_rank < 40) or (skew_discount is not None and skew_discount > 0.1)) and
            (iv_trend is None or iv_trend <= 0.05)
        )
        is_directional_trade = (
            trend != "neutral" and
            0.05 <= abs_delta <= 0.55 and
            expected_move_ratio <= 1.5
        )

        if is_volatility_trade:
            return "volatility"
        if is_directional_trade:
            return "directional"
        return None

    def select_top_trades(self, opportunities, limit=500, max_per_direction=260):
        """Pick the highest-conviction trades with a per-direction cap."""
        if isinstance(opportunities, pd.DataFrame):
            rows = opportunities.sort_values("score", ascending=False).to_dict("records")
        else:
            rows = sorted(opportunities, key=lambda item: item["score"], reverse=True)

        selected = []
        direction_counts = {}
        for row in rows:
            direction = row.get("type")
            if direction_counts.get(direction, 0) >= max_per_direction:
                continue
            selected.append(row)
            direction_counts[direction] = direction_counts.get(direction, 0) + 1
            if len(selected) >= limit:
                break
        return selected

    # ==================== 3. DISCOUNTED PREMIUM DETECTION ====================

    def build_strategy_plan(self, option_type, strike_price, spot_price, mid_price, option_chain,
                            expected_move, trend, score, entry_price=None, exit_price=None):
        """Create tradable strategy suggestions from a shortlisted option."""
        strike_keys = sorted(float(key) for key in option_chain.keys())
        if option_type == "CALL":
            candidate_shorts = [strike for strike in strike_keys if strike > strike_price]
            short_strike = candidate_shorts[0] if candidate_shorts else None
        else:
            candidate_shorts = [strike for strike in strike_keys if strike < strike_price]
            short_strike = candidate_shorts[-1] if candidate_shorts else None

        reference_entry = entry_price if entry_price is not None else mid_price
        reference_exit = exit_price if exit_price is not None else mid_price

        entry = reference_entry
        stop_loss = reference_exit * 0.65 if reference_exit else 0
        target = reference_entry * 1.8 if reference_entry else 0
        risk_reward = None
        if entry and stop_loss and target and entry != stop_loss:
            risk_reward = (target - entry) / (entry - stop_loss)

        if option_type == "CALL" and trend == "bullish":
            strategy = "Call Debit Spread"
            if expected_move and short_strike is not None:
                cap_strike = min(
                    [strike for strike in candidate_shorts if strike <= strike_price + expected_move] or [short_strike]
                )
                short_strike = cap_strike
        elif option_type == "PUT" and trend == "bearish":
            strategy = "Bear Put Spread"
            if expected_move and short_strike is not None:
                floor_strike = max(
                    [strike for strike in candidate_shorts if strike >= strike_price - expected_move] or [short_strike]
                )
                short_strike = floor_strike
        else:
            strategy = "Volatility Expansion Play"
            short_strike = None

        return {
            "strategy": strategy,
            "short_strike": short_strike,
            "entry": round(entry, 2) if entry else 0.0,
            "stop_loss": round(stop_loss, 2) if stop_loss else 0.0,
            "target": round(target, 2) if target else 0.0,
            "risk_reward": round(risk_reward, 2) if risk_reward is not None else None,
        }

    def score_option(self, current_iv, weighted_hv, delta, vega, oi, volume, skew_discount,
                     expected_move_ratio, iv_rank=None, iv_percentile=None, vol_mode="skew",
                     skew_z=None, trade_type="volatility"):
        """Score options using only the core decision factors."""
        hv_score = 50.0
        if weighted_hv and weighted_hv > 0:
            hv_edge_pct = ((weighted_hv - current_iv) / weighted_hv) * 100
            hv_score = clip_score(50 + hv_edge_pct * 2)

        abs_delta = abs(delta)
        if 0.15 <= abs_delta <= 0.40:
            delta_score = 100.0
        elif 0.10 <= abs_delta < 0.15 or 0.40 < abs_delta <= 0.55:
            delta_score = 70.0
        else:
            delta_score = 25.0

        liquidity_score = clip_score((math.log1p(max(oi, 0)) * 12) + (math.log1p(max(volume, 0)) * 8))
        skew_score = clip_score(50 + skew_discount * 8) if skew_discount is not None else 50.0
        iv_regime = classify_iv_regime(iv_rank, iv_percentile)
        if iv_regime == "LOW":
            iv_regime_bonus = 10.0
        elif iv_regime == "HIGH":
            iv_regime_bonus = -15.0
        else:
            iv_regime_bonus = 0.0
        iv_rank_penalty = 20.0 if iv_rank is not None and iv_rank > 60 else 0.0

        if expected_move_ratio <= 1.0:
            relevance_score = clip_score(92 - (abs(expected_move_ratio - 0.75) / 0.55) * 42)
        elif expected_move_ratio <= 1.5:
            relevance_score = clip_score(78 - ((expected_move_ratio - 1.0) / 0.5) * 18)
        elif expected_move_ratio <= 2.5:
            relevance_score = clip_score(60 - ((expected_move_ratio - 1.5) / 1.0) * 22)
        else:
            relevance_score = 30.0

        if trade_type == "directional":
            raw_score = (
                hv_score * 0.25 +
                delta_score * 0.35 +
                liquidity_score * 0.10 +
                skew_score * 0.15 +
                relevance_score * 0.25
            )
            component_scores = {
                "iv_vs_hv": native_number(round(hv_score, 2)),
                "delta": native_number(round(delta_score, 2)),
                "liquidity": native_number(round(liquidity_score, 2)),
                "skew": native_number(round(skew_score, 2)),
                "strike_relevance": native_number(round(relevance_score, 2)),
                "iv_regime_bonus": native_number(round(iv_regime_bonus, 2)),
                "iv_rank_penalty": native_number(round(iv_rank_penalty, 2)),
            }
        else:
            raw_score = (
                hv_score * 0.30 +
                skew_score * 0.40 +
                delta_score * 0.10 +
                liquidity_score * 0.10 +
                relevance_score * 0.20
            )
            component_scores = {
                "iv_vs_hv": native_number(round(hv_score, 2)),
                "skew": native_number(round(skew_score, 2)),
                "delta": native_number(round(delta_score, 2)),
                "liquidity": native_number(round(liquidity_score, 2)),
                "strike_relevance": native_number(round(relevance_score, 2)),
                "iv_regime_bonus": native_number(round(iv_regime_bonus, 2)),
                "iv_rank_penalty": native_number(round(iv_rank_penalty, 2)),
            }

        final_score = clip_score(
            40 + (raw_score * 0.55) + iv_regime_bonus - iv_rank_penalty,
            floor=0.0,
            ceiling=95.0,
        )
        return {
            "score": round(final_score, 2),
            "component_scores": component_scores,
            "iv_regime": iv_regime,
        }
    
    def scan_single_strike(self, strike_data, strike_price, spot_price, option_chain,
                          historical_ivs=None, hv_metrics=None, atm_context=None,
                          expected_move=None, dte=None, trend="neutral", hedging_mode=False,
                          has_iv_history=False, call_mean=None, call_std=None,
                          put_mean=None, put_std=None, call_ivs=None, put_ivs=None,
                          call_avg_volume=None, put_avg_volume=None, iv_behavior=None,
                          premarket_ctx=None, pcr_value=None, sentiment_bias="neutral",
                          market_signal=None, expiry=None):
        """
        Analyze a single strike using quantitative volatility, probability, and structure filters.
        
        Args:
            strike_data: Option data for a strike (contains ce and/or pe)
            strike_price: Strike price
            spot_price: Current underlying price
            historical_ivs: Historical IV values for IV Rank/IV Percentile
            hv_metrics: Multi-window historical volatility context
            atm_context: ATM strike IV reference for skew-aware comparison
            expected_move: Expected move in points for the expiry
            dte: Days to expiry
            trend: Market context trend
            hedging_mode: Whether to allow very low delta options
        
        Returns:
            list: Structured trade candidates
        """
        discounted = []
        weighted_hv = (hv_metrics or {}).get("weighted_hv")
        market_signal = market_signal or {"direction": "neutral", "confidence": 50.0, "components": {}}
        market_direction = market_signal.get("direction", "neutral")
        market_components = market_signal.get("components") or {}
        buildup = market_components.get("buildup") or {}
        pcr_signal = market_components.get("pcr_trend") or {}
        oi_shift = market_components.get("oi_shift") or {}
        volume_spike = market_components.get("volume_spike") or {}
        oi_wall_signal = market_components.get("oi_walls") or {}
        nearest_put_wall = oi_wall_signal.get("nearest_put_wall")
        nearest_call_wall = oi_wall_signal.get("nearest_call_wall")
        put_walls = oi_wall_signal.get("put_walls") or []
        call_walls = oi_wall_signal.get("call_walls") or []
        market_strength = market_signal.get("strength", "NEUTRAL")
        call_iv = pd.to_numeric((strike_data.get("ce") or {}).get("implied_volatility"), errors="coerce")
        put_iv = pd.to_numeric((strike_data.get("pe") or {}).get("implied_volatility"), errors="coerce")
        relative_skew = None
        if pd.notna(call_iv) and pd.notna(put_iv):
            # Cross-side skew lets us compare the call and put IV at the same strike.
            relative_skew = float(call_iv - put_iv)
        
        for option_type in ['ce', 'pe']:
            if option_type not in strike_data:
                continue
            
            opt = strike_data[option_type]
            option_label = 'CALL' if option_type == 'ce' else 'PUT'
            option_buildup = self.compute_buildup_from_option(opt)
            
            oi = opt.get('oi', 0)
            volume = opt.get('volume', 0)
            delta = opt.get('greeks', {}).get('delta', 0)
            vega = opt.get('greeks', {}).get('vega', 0)
            abs_delta = abs(delta)

            # if volume <= 0:
            #     self.log_option_rejection(strike_price, option_label, "Rejected due to zero volume", oi=oi, volume=volume)
            #     continue
            if not hedging_mode and abs_delta < 0.05:
                continue

            current_iv = pd.to_numeric(opt.get('implied_volatility', 0), errors="coerce")
            if pd.isna(current_iv) or current_iv <= 0:
                self.log_option_rejection(strike_price, option_label, "Rejected due to missing IV")
                continue
            current_iv = float(current_iv)
            atm_iv = atm_context.get("atm_iv") if atm_context else None
            if atm_iv and current_iv and abs(current_iv - atm_iv) > 20:
                logger.debug(f"IV mismatch | strike_iv={current_iv} | atm_iv={atm_iv}")

            if option_type == "ce":
                reference_iv = call_mean
                skew_std = call_std
                peer_ivs = call_ivs or []
                avg_peer_volume = call_avg_volume
            else:
                reference_iv = put_mean
                skew_std = put_std
                peer_ivs = put_ivs or []
                avg_peer_volume = put_avg_volume

            pricing = self.get_execution_prices(opt)
            bid = pricing["bid"]
            ask = pricing["ask"]
            entry_price = pricing["entry_price"]
            exit_price = pricing["exit_price"]
            mid_price = pricing["mid_price"]

            if ask <= 0:
                self.log_option_rejection(strike_price, option_label, "Rejected due to invalid ask price", ask=ask, bid=bid)
                continue
            spread_pct = (ask - bid) / ask if ask else 1.0
            if spread_pct >= 0.60:
                self.log_option_rejection(
                    strike_price,
                    option_label,
                    "Rejected due to unusably wide spread",
                    bid=round(bid, 2),
                    ask=round(ask, 2),
                    spread_pct=round(spread_pct, 4),
                )
                continue

            iv_is_stable, iv_deviation = self.is_iv_stable(option_chain, strike_price, option_type, current_iv)
            if not iv_is_stable:
                self.log_option_rejection(
                    strike_price,
                    option_label,
                    "Rejected due to unstable IV versus neighboring strikes",
                    iv=round(current_iv, 2),
                    iv_deviation=round(iv_deviation, 4) if iv_deviation is not None else None,
                )
                continue

            skew_z = 0.0
            if reference_iv is not None and skew_std is not None and skew_std > 0:
                skew_z = (current_iv - reference_iv) / skew_std
            skew_discount = -skew_z
            iv_context = "below_chain_mean" if skew_z < 0 else "above_chain_mean"

            distance_from_spot = abs(strike_price - spot_price)
            expected_move_ratio = (distance_from_spot / expected_move) if expected_move and expected_move > 0 else 0
            if expected_move_ratio > 2.5:
                self.log_option_rejection(
                    strike_price,
                    option_label,
                    "Rejected due to expected move ratio above relaxed ceiling",
                    expected_move_ratio=round(expected_move_ratio, 3),
                )
                continue
            vol_mode = "historical" if has_iv_history else "skew"
            iv_rank = self.calculate_iv_rank(current_iv, historical_ivs) if has_iv_history else None
            iv_percentile = self.calculate_iv_percentile(current_iv, historical_ivs) if has_iv_history else None
            iv_regime = classify_iv_regime(iv_rank, iv_percentile)
            iv_trend = (premarket_ctx or {}).get("iv_trend")
            trade_type = self.classify_trade_type(
                iv_rank=iv_rank,
                skew_discount=skew_discount,
                iv_trend=iv_trend,
                trend=trend,
                abs_delta=abs_delta,
                expected_move_ratio=expected_move_ratio,
            )
            if trade_type is None:
                self.log_option_rejection(
                    strike_price,
                    option_label,
                    "Rejected due to trade type mismatch",
                    trend=trend,
                    iv_rank=round(iv_rank, 2) if iv_rank is not None else None,
                    skew_discount=round(skew_discount, 2) if skew_discount is not None else None,
                    iv_trend=round(iv_trend, 4) if iv_trend is not None else None,
                    abs_delta=round(abs_delta, 3),
                    expected_move_ratio=round(expected_move_ratio, 3),
                )
                continue

            option_iv_behavior = iv_behavior
            if isinstance(iv_behavior, dict) and ("ce" in iv_behavior or "pe" in iv_behavior):
                option_iv_behavior = iv_behavior.get(option_type)

            if iv_rank is None or iv_percentile is None:
                self.log_option_rejection(
                    strike_price,
                    option_label,
                    "Rejected due to IV regime",
                    iv_rank=round(iv_rank, 2) if iv_rank is not None else None,
                    iv_percentile=round(iv_percentile, 2) if iv_percentile is not None else None,
                    iv_regime=iv_regime,
                    trade_type=trade_type,
                    detail="missing historical IV context",
                )
                continue

            if trade_type == "volatility" and iv_rank > 60:
                self.log_option_rejection(
                    strike_price,
                    option_label,
                    "Rejected due to IV regime",
                    iv_rank=round(iv_rank, 2),
                    iv_percentile=round(iv_percentile, 2),
                    iv_regime=iv_regime,
                    trade_type=trade_type,
                    detail="volatility trade rejected above IV Rank 60",
                )
                continue

            if trade_type == "directional" and iv_rank > 70 and skew_discount <= 0:
                self.log_option_rejection(
                    strike_price,
                    option_label,
                    "Rejected due to IV regime",
                    iv_rank=round(iv_rank, 2),
                    iv_percentile=round(iv_percentile, 2),
                    iv_regime=iv_regime,
                    trade_type=trade_type,
                    skew_discount=round(skew_discount, 2),
                    detail="directional trade rejected with rich IV and no skew discount",
                )
                continue

            if skew_discount <= 0:
                self.log_option_rejection(
                    strike_price,
                    option_label,
                    "Rejected due to IV regime",
                    iv_rank=round(iv_rank, 2),
                    iv_percentile=round(iv_percentile, 2),
                    iv_regime=iv_regime,
                    trade_type=trade_type,
                    skew_discount=round(skew_discount, 2),
                    detail="requires positive skew discount",
                )
                continue

            if iv_rank >= 50:
                self.log_option_rejection(
                    strike_price,
                    option_label,
                    "Rejected due to IV regime",
                    iv_rank=round(iv_rank, 2),
                    iv_percentile=round(iv_percentile, 2),
                    iv_regime=iv_regime,
                    trade_type=trade_type,
                    skew_discount=round(skew_discount, 2),
                    detail="positive skew requires IV Rank below 50",
                )
                continue

            if option_iv_behavior and option_iv_behavior.get("low_iv_threshold") is not None:
                low_iv_threshold = option_iv_behavior["low_iv_threshold"]
                avg_move_after_low_iv = option_iv_behavior.get("avg_move_after_low_iv")
                if current_iv < low_iv_threshold and (
                    avg_move_after_low_iv is None or avg_move_after_low_iv < 0.01
                ):
                    self.log_option_rejection(
                        strike_price,
                        option_label,
                        "Rejected due to IV regime",
                        iv_rank=round(iv_rank, 2),
                        iv_percentile=round(iv_percentile, 2),
                        iv_regime=iv_regime,
                        current_iv=round(current_iv, 2),
                        low_iv_threshold=round(low_iv_threshold, 2),
                        avg_move_after_low_iv=round(avg_move_after_low_iv, 4) if avg_move_after_low_iv is not None else None,
                        detail="low IV has not historically produced enough follow-through",
                    )
                    continue

            quality_stats = getattr(self, "_scan_quality_stats", None)
            if isinstance(quality_stats, dict):
                quality_stats["pre_quality"] = quality_stats.get("pre_quality", 0) + 1

            quality_score = 0
            if skew_discount and skew_discount > 0:
                quality_score += 1
            if 0.15 <= abs_delta <= 0.45:
                quality_score += 1
            if expected_move_ratio <= 1.5:
                quality_score += 1
            if volume > 1000:
                quality_score += 1
            if oi >= 1000:
                quality_score += 1

            if quality_score < 1:
                self.log_option_rejection(strike_price, option_label, "Rejected due to low quality score", quality_score=quality_score)
                continue

            if isinstance(quality_stats, dict):
                quality_stats["post_quality"] = quality_stats.get("post_quality", 0) + 1

            score_details = self.score_option(
                current_iv=current_iv,
                weighted_hv=weighted_hv,
                delta=delta,
                vega=vega,
                oi=oi,
                volume=volume,
                skew_discount=skew_discount,
                expected_move_ratio=expected_move_ratio,
                iv_rank=iv_rank,
                iv_percentile=iv_percentile,
                vol_mode=vol_mode,
                skew_z=skew_z,
                trade_type=trade_type,
            )

            score = score_details["score"]
            base_discount_score = score
            context_adjustment = 0
            score_adjustment = 0.0

            if premarket_ctx:
                iv_change = premarket_ctx.get("iv_change")

                if iv_change is not None:
                    if iv_change < -2:
                        context_adjustment += 8
                    elif iv_change > 2:
                        context_adjustment -= 10
                if iv_trend is not None:
                    if iv_trend < 0:
                        context_adjustment += 8
                    elif iv_trend > 0:
                        context_adjustment -= 10

            score += context_adjustment

            if volume < 200:
                score_adjustment -= 8.0
            elif volume < 500:
                score_adjustment -= 4.0
            elif volume > 1500:
                score_adjustment += 3.0

            if oi < 1000:
                score_adjustment -= 6.0
            elif oi > 8000:
                score_adjustment += 3.0

            if spread_pct > 0.20:
                score_adjustment -= min(12.0, (spread_pct - 0.20) * 45.0)
            elif spread_pct < 0.08:
                score_adjustment += 2.5

            if expected_move_ratio > 1.0:
                if expected_move_ratio <= 2.5:
                    score_adjustment -= min(15.0, (expected_move_ratio - 1.0) * 8.0)
            elif expected_move_ratio < 0.35:
                score_adjustment -= min(8.0, (0.35 - expected_move_ratio) * 15.0)

            chain_percentile = None
            if peer_ivs:
                peer_iv_array = np.array(peer_ivs, dtype=float)
                if len(peer_iv_array) > 0:
                    chain_percentile = float((peer_iv_array < current_iv).mean() * 100)

            if option_iv_behavior and option_iv_behavior.get("low_iv_threshold") is not None:
                low_iv_threshold = option_iv_behavior["low_iv_threshold"]
                avg_move_after_low_iv = option_iv_behavior.get("avg_move_after_low_iv")
                if current_iv < low_iv_threshold:
                    if avg_move_after_low_iv is not None and avg_move_after_low_iv > 0.01:
                        logger.info(
                            "IV behavior context | strike=%.2f | type=%s | current_iv=%.2f | low_iv_threshold=%.2f | avg_move_after_low_iv=%.4f",
                            strike_price,
                            option_type.upper(),
                            current_iv,
                            low_iv_threshold,
                            avg_move_after_low_iv,
                        )
                    else:
                        logger.info(
                            "IV behavior context | strike=%.2f | type=%s | current_iv=%.2f | low_iv_threshold=%.2f | avg_move_after_low_iv=%s",
                            strike_price,
                            option_type.upper(),
                            current_iv,
                            low_iv_threshold,
                            f"{avg_move_after_low_iv:.4f}" if avg_move_after_low_iv is not None else "None",
                        )

            option_direction = "bullish" if option_label == "CALL" else "bearish"
            near_put_wall, strike_put_wall = self.is_near_oi_wall(strike_price, put_walls)
            near_call_wall, strike_call_wall = self.is_near_oi_wall(strike_price, call_walls)
            nearest_put_wall = strike_put_wall or nearest_put_wall
            nearest_call_wall = strike_call_wall or nearest_call_wall

            if option_label == "CALL" and near_put_wall:
                score_adjustment += 10.0
            if option_label == "PUT" and near_call_wall:
                score_adjustment += 10.0
            if option_label == "CALL" and option_buildup.get("type") == "LONG_BUILDUP":
                score_adjustment += 8.0
            if option_label == "PUT" and option_buildup.get("type") == "SHORT_BUILDUP":
                score_adjustment += 8.0

            if market_direction == option_direction:
                confidence_bonus = 10.0 + ((market_signal.get("confidence", 50.0) - 50.0) / 4.0)
                score_adjustment += min(20.0, max(10.0, confidence_bonus))
            elif market_direction != "neutral":
                score_adjustment -= 10.0

            if volume_spike.get("spike") and option_buildup.get("type") != "NEUTRAL":
                score_adjustment += 5.0
            if market_strength == "STRONG_BULLISH" and option_label == "CALL":
                score_adjustment += 6.0
            if market_strength == "STRONG_BEARISH" and option_label == "PUT":
                score_adjustment += 6.0

            score = round(clip_score(score + score_adjustment, floor=0.0, ceiling=95.0), 2)
            score_breakdown = {
                "base": native_number(round(base_discount_score, 2)),
                "context": native_number(round(context_adjustment, 2)),
                "adjustment": native_number(round(score_adjustment, 2)),
                "iv_regime": iv_regime,
                "near_put_wall": near_put_wall,
                "near_call_wall": near_call_wall,
                "buildup": option_buildup.get("type", "NEUTRAL"),
            }
            debug_candidates = getattr(self, "_score_debug_candidates", None)
            if isinstance(debug_candidates, list):
                debug_candidates.append({
                    "strike": native_number(strike_price),
                    "type": option_label,
                    "score": native_number(score),
                    "score_breakdown": score_breakdown,
                    "iv": native_number(current_iv),
                    "oi": native_number(oi),
                    "volume": native_number(volume),
                })

            if score < 40:
                self.log_option_rejection(
                    strike_price,
                    option_label,
                    "Rejected due to score below minimum",
                    score=score,
                    score_breakdown=score_breakdown,
                )
                continue

            hv_gap = weighted_hv - current_iv if weighted_hv else None
            moneyness = ((strike_price - spot_price) / spot_price * 100) if option_type == 'ce' else ((spot_price - strike_price) / spot_price * 100)

            strategy_plan = self.build_strategy_plan(
                option_type=option_label,
                strike_price=strike_price,
                spot_price=spot_price,
                mid_price=mid_price,
                option_chain=option_chain,
                expected_move=expected_move,
                trend=trend,
                score=score,
                entry_price=entry_price,
                exit_price=exit_price,
            )

            if iv_rank < 40 and skew_discount > 0 and expected_move_ratio <= 1.5:
                conviction = "HIGH"
            elif iv_rank < 50:
                conviction = "MEDIUM"
            else:
                conviction = "LOW"

            reasons = []
            reasons.append(f"IV regime is {iv_regime}")
            if has_iv_history and iv_rank is not None and iv_rank < 30:
                reasons.append(f"IV Rank is compressed at {iv_rank:.1f}")
            if has_iv_history and iv_percentile is not None and iv_percentile <= 35:
                reasons.append(f"IV Percentile is low at {iv_percentile:.1f}")
            if hv_gap and hv_gap > 0:
                reasons.append(f"IV is {hv_gap:.2f} points below weighted HV")
            if 0.15 <= abs_delta <= 0.45:
                reasons.append(f"Delta {delta:.2f} sits in the preferred directional range")
            if skew_discount > 0:
                reasons.append(f"Strike IV is {skew_discount:.2f} std below same-side chain mean")
            if relative_skew is not None:
                if relative_skew < 0:
                    reasons.append(f"Relative skew {relative_skew:.2f}: calls are cheaper than puts")
                elif relative_skew > 0:
                    reasons.append(f"Relative skew {relative_skew:.2f}: puts are cheaper than calls")
                else:
                    reasons.append("Relative skew is neutral between calls and puts")
            reasons.append(f"IV context is {iv_context}")
            if expected_move and 0.5 <= expected_move_ratio <= 1.0:
                reasons.append("Strike is inside the 1x expected move envelope")
            elif expected_move and expected_move_ratio <= 2.5:
                reasons.append(f"Strike remains inside the relaxed {expected_move_ratio:.2f}x expected move envelope")
            if chain_percentile is not None and chain_percentile < 20:
                reasons.append(f"Chain IV percentile is cheap at {chain_percentile:.1f}")
            elif chain_percentile is not None and chain_percentile > 80:
                reasons.append(f"Chain IV percentile is rich at {chain_percentile:.1f}")
            if avg_peer_volume is not None and volume > avg_peer_volume:
                reasons.append("Volume is above same-side chain average")
            elif volume < 200:
                reasons.append("Volume is below 200, so liquidity score was penalized instead of rejected")
            if oi < 1000:
                reasons.append("Open interest is below 1000, so score was penalized instead of rejected")
            if spread_pct > 0.20:
                reasons.append(f"Spread is wide at {spread_pct:.1%}, so score was penalized")
            if option_iv_behavior and option_iv_behavior.get("low_iv_threshold") is not None and current_iv < option_iv_behavior["low_iv_threshold"]:
                avg_move_after_low_iv = option_iv_behavior.get("avg_move_after_low_iv")
                if avg_move_after_low_iv is not None and avg_move_after_low_iv > 0.01:
                    reasons.append("Historical IV expansion observed after similar low IV levels")
                else:
                    reasons.append("Historically low IV does not lead to strong moves")
            if pcr_value is not None:
                reasons.append(f"PCR is {pcr_value:.2f}, which reads as {sentiment_bias}")
            if premarket_ctx and premarket_ctx.get("iv_trend") is not None:
                iv_trend = premarket_ctx["iv_trend"]
                if iv_trend < 0:
                    reasons.append(f"Warmup IV trend is compressing at slope {iv_trend:.3f}")
                elif iv_trend > 0:
                    reasons.append(f"Warmup IV trend is expanding at slope {iv_trend:.3f}")
            if oi > 10000 and volume > 1000:
                reasons.append("Liquidity is strong in both OI and volume")
            if pcr_signal.get("trend") and pcr_signal.get("trend") != "neutral":
                reasons.append(f"PCR trend is {pcr_signal['trend']} at {pcr_signal.get('current_pcr')}")
            if option_buildup.get("type") and option_buildup.get("type") != "NEUTRAL":
                reasons.append(f"OI buildup reads as {option_buildup['type']} ({option_buildup.get('strength', 0):.0f})")
            if oi_shift.get("call_shift") != "same" or oi_shift.get("put_shift") != "same":
                reasons.append(f"OI shift call={oi_shift.get('call_shift')} put={oi_shift.get('put_shift')}")
            if volume_spike.get("spike"):
                reasons.append(f"Volume spike detected at {volume_spike.get('ratio')}x recent average")
            if nearest_put_wall:
                reasons.append(f"Nearest put wall sits at {nearest_put_wall.get('strike'):.0f}")
            if nearest_call_wall:
                reasons.append(f"Nearest call wall sits at {nearest_call_wall.get('strike'):.0f}")
            reasons.append(f"Market signal is {market_direction} with {market_signal.get('confidence', 50.0):.1f} confidence")

            discounted.append({
                "symbol": None,
                "expiry": expiry,
                "strategy": strategy_plan["strategy"],
                "strike": native_number(strike_price),
                "short_strike": native_number(strategy_plan["short_strike"]),
                "type": 'CALL' if option_type == 'ce' else 'PUT',
                "trade_type": trade_type,
                "vol_mode": vol_mode,
                "iv_context": iv_context,
                "iv": native_number(current_iv),
                "iv_rank": native_number(iv_rank),
                "iv_percentile": native_number(iv_percentile),
                "iv_regime": iv_regime,
                "conviction": conviction,
                "hv": native_number(weighted_hv),
                "hv10": native_number((hv_metrics or {}).get("hv10")),
                "hv20": native_number((hv_metrics or {}).get("hv20")),
                "hv60": native_number((hv_metrics or {}).get("hv60")),
                "delta": native_number(delta),
                "vega": native_number(vega),
                "theta": native_number(opt.get('greeks', {}).get('theta', 0)),
                "score": native_number(score),
                "entry": strategy_plan["entry"],
                "stop_loss": strategy_plan["stop_loss"],
                "target": strategy_plan["target"],
                "risk_reward": strategy_plan["risk_reward"],
                "reason": reasons,
                "mid_price": native_number(mid_price),
                "bid": native_number(bid),
                "ask": native_number(ask),
                "entry_price": native_number(entry_price),
                "exit_price": native_number(exit_price),
                "spot": native_number(spot_price),
                "moneyness": native_number(moneyness),
                "oi": oi,
                "volume": volume,
                "expected_move": native_number(expected_move),
                "expected_move_ratio": native_number(expected_move_ratio),
                "quality_score": quality_score,
                "pcr_value": native_number(pcr_value),
                "sentiment_bias": sentiment_bias,
                "pcr_trend": pcr_signal.get("trend", "neutral"),
                "atm_iv": native_number((atm_context or {}).get("atm_iv")),
                "atm_reference_iv": native_number(reference_iv),
                "skew_discount": native_number(skew_discount),
                "relative_skew": native_number(relative_skew),
                "iv_change": native_number((premarket_ctx or {}).get("iv_change")),
                "iv_trend": native_number((premarket_ctx or {}).get("iv_trend")),
                "buildup_type": option_buildup.get("type", "NEUTRAL"),
                "buildup_strength": native_number(option_buildup.get("strength")),
                "oi_change": native_number(option_buildup.get("oi_change")),
                "oi_change_pct": native_number(option_buildup.get("oi_change_pct")),
                "price_change": native_number(option_buildup.get("price_change")),
                "price_change_pct": native_number(option_buildup.get("price_change_pct")),
                "oi_shift_call": oi_shift.get("call_shift", "same"),
                "oi_shift_put": oi_shift.get("put_shift", "same"),
                "volume_spike": bool(volume_spike.get("spike")),
                "volume_spike_ratio": native_number(volume_spike.get("ratio")),
                "market_direction": market_direction,
                "market_strength": market_strength,
                "market_confidence": native_number(market_signal.get("confidence")),
                "nearest_call_wall": native_number((nearest_call_wall or {}).get("strike")),
                "nearest_put_wall": native_number((nearest_put_wall or {}).get("strike")),
                "oi_support_side": "PUT_WALL" if option_label == "CALL" and near_put_wall else "CALL_WALL" if option_label == "PUT" and near_call_wall else None,
                "trend": trend,
                "dte": dte,
                "recommended_position_size": "2% capital",
                "max_trades_per_day": 2,
                "risk_per_trade": native_number(max((strategy_plan["entry"] or 0) - (strategy_plan["stop_loss"] or 0), 0)),
                "component_scores": score_details["component_scores"],
                "score_breakdown": score_breakdown,
            })
        
        return discounted
    
    def scan_underlying(self, security_id, security_segment, security_name, 
                        expiry=None, use_hv=True):
        """
        Scan all strikes of an underlying for discounted premiums
        
        Args:
            security_id: Security ID
            security_segment: Exchange segment
            security_name: Name for display
            expiry: Specific expiry to scan (None for nearest)
            use_hv: Whether to calculate and use HV
        
        Returns:
            list: Discounted options across all strikes
        """
        logger.info("%s", "=" * 60)
        logger.info("Scanning %s (ID: %s)", security_name, security_id)
        logger.info("%s", "=" * 60)
        
        # Get expiry list if not specified
        if expiry is None:
            expiries = self.get_expiry_list(security_id, security_segment)
            if not expiries:
                logger.warning("No expiries found for %s (%s)", security_name, security_segment)
                return []
            expiry = expiries[0]

        dte = get_trading_days_to_expiry(expiry)
        logger.info(f"Selected expiry: {expiry} (DTE: {dte})")
        
        # Fetch option chain
        chain_response = self.get_option_chain(security_id, security_segment, expiry)
        
        if chain_response.get('status') != 'success':
            return []

        chain_data = unwrap_dhan_payload(chain_response.get("data") or {})
        spot_price = chain_data.get("last_price")
        option_chain = chain_data.get("oc")
        if spot_price is None:
            logger.error("Option chain missing last_price for %s: %s", security_name, chain_response)
            return []
        if not isinstance(option_chain, dict):
            logger.error("Option chain missing oc data for %s: %s", security_name, chain_response)
            return []

        logger.info("Spot Price: %.2f", spot_price)
        logger.info("Expiry: %s", expiry)
        call_ivs = []
        put_ivs = []
        call_volumes = []
        put_volumes = []
        chain_metrics = self.extract_chain_metrics(option_chain)
        buildup_distribution = self.build_buildup_distribution(option_chain)
        chain_metrics["buildup_distribution"] = buildup_distribution
        directional_buildups = {key: value for key, value in buildup_distribution.items() if key != "NEUTRAL"}
        dominant_buildup_type = max(directional_buildups, key=directional_buildups.get) if directional_buildups else "NEUTRAL"
        dominant_buildup_count = directional_buildups.get(dominant_buildup_type, 0) if directional_buildups else 0
        dominant_buildup = {
            "type": dominant_buildup_type,
            "strength": clip_score(dominant_buildup_count * 100.0 / max(sum(buildup_distribution.values()), 1)),
        }
        logger.info("Total strikes: %s", len(option_chain))
        logger.info(
            "OI walls: call=%s put=%s | thresholds call=%s put=%s",
            len(chain_metrics.get("call_walls") or []),
            len(chain_metrics.get("put_walls") or []),
            f"{chain_metrics.get('call_wall_threshold'):.0f}" if chain_metrics.get("call_wall_threshold") is not None else "N/A",
            f"{chain_metrics.get('put_wall_threshold'):.0f}" if chain_metrics.get("put_wall_threshold") is not None else "N/A",
        )
        logger.info("Buildup distribution: %s", buildup_distribution)
        for strike_data in option_chain.values():
            if not isinstance(strike_data, dict):
                continue
            call_opt = strike_data.get("ce") or {}
            put_opt = strike_data.get("pe") or {}

            call_iv = pd.to_numeric(call_opt.get("implied_volatility"), errors="coerce")
            put_iv = pd.to_numeric(put_opt.get("implied_volatility"), errors="coerce")
            call_volume = pd.to_numeric(call_opt.get("volume"), errors="coerce")
            put_volume = pd.to_numeric(put_opt.get("volume"), errors="coerce")
            call_oi = pd.to_numeric(call_opt.get("oi"), errors="coerce")
            put_oi = pd.to_numeric(put_opt.get("oi"), errors="coerce")

            if pd.notna(call_iv) and call_iv > 0:
                call_ivs.append(float(call_iv))
            if pd.notna(put_iv) and put_iv > 0:
                put_ivs.append(float(put_iv))
            if pd.notna(call_volume) and call_volume > 0:
                call_volumes.append(float(call_volume))
            if pd.notna(put_volume) and put_volume > 0:
                put_volumes.append(float(put_volume))

        call_mean = float(np.mean(call_ivs)) if call_ivs else None
        put_mean = float(np.mean(put_ivs)) if put_ivs else None
        call_std = float(np.std(call_ivs)) if len(call_ivs) > 1 else 0.0
        put_std = float(np.std(put_ivs)) if len(put_ivs) > 1 else 0.0
        call_avg_volume = float(np.mean(call_volumes)) if call_volumes else None
        put_avg_volume = float(np.mean(put_volumes)) if put_volumes else None
        total_call_oi = chain_metrics.get("total_call_oi") or 0.0
        total_put_oi = chain_metrics.get("total_put_oi") or 0.0
        pcr_value = (total_put_oi / total_call_oi) if total_call_oi > 0 else None
        if pcr_value is None:
            sentiment_bias = "neutral"
        elif pcr_value > 1.2:
            sentiment_bias = "bullish"
        elif pcr_value < 0.8:
            sentiment_bias = "bearish"
        else:
            sentiment_bias = "neutral"

        atm_context = self.extract_atm_reference_ivs(option_chain, spot_price)
        premarket_ctx = self.build_premarket_context(security_id)
        historical_ivs = self.fetch_historical_iv(security_id, security_segment)
        has_iv_history = len(historical_ivs) >= MIN_IV_SAMPLES
        iv_rank_atm = self.calculate_iv_rank(atm_context.get("atm_iv") or 0, historical_ivs) if atm_context.get("atm_iv") and has_iv_history else None
        iv_percentile_atm = self.calculate_iv_percentile(atm_context.get("atm_iv") or 0, historical_ivs) if atm_context.get("atm_iv") and has_iv_history else None
        self.persist_iv_snapshot(security_id, security_segment, security_name, expiry, spot_price, atm_context, chain_metrics=chain_metrics)
        market_signal = self.build_market_signal(
            security_id,
            spot_price=spot_price,
            chain_metrics=chain_metrics,
            buildup=dominant_buildup,
        )
        dte = self.days_to_expiry(expiry)
        expected_move = self.compute_expected_move(spot_price, atm_context.get("atm_iv"), dte)
        atm_context["expected_move"] = expected_move
        iv_behavior = None
        historical_option_df = self.fetch_expired_option_data(
            security_id=security_id,
            exchange_segment=security_segment,
            option_type="CALL",
            strike="ATM",
        )
        if not historical_option_df.empty:
            iv_behavior = self.compute_iv_behavior_metrics(historical_option_df)
            if iv_behavior:
                logger.info(
                    "Historical IV behavior for %s: percentile=%.2f | avg_move_after_low_iv=%.4f | low_iv_threshold=%.2f",
                    security_name,
                    iv_behavior["iv_percentile"],
                    iv_behavior["avg_move_after_low_iv"] if iv_behavior["avg_move_after_low_iv"] is not None else float("nan"),
                    iv_behavior["low_iv_threshold"],
                )
        logger.info("Volatility Mode: %s", "IV_HISTORY" if has_iv_history else "SKEW")
        logger.info("IV Samples Available: %s", len(historical_ivs))
        if atm_context.get("atm_iv"):
            logger.info("ATM IV: %.2f", atm_context["atm_iv"])
        if iv_rank_atm is not None:
            logger.info("ATM IV Rank / Percentile: %.2f / %.2f", iv_rank_atm, iv_percentile_atm)
        if expected_move is not None:
            logger.info("Expected Move (%.0f DTE): %.2f points", dte, expected_move)
        if pcr_value is not None:
            logger.info("PCR: %.2f | Sentiment Bias: %s", pcr_value, sentiment_bias)
        else:
            logger.info("PCR: N/A | Sentiment Bias: %s", sentiment_bias)
        logger.info(
            "Market signal: %s (confidence %.1f) | buildup=%s | pcr_trend=%s | oi_shift=%s/%s | volume_spike=%s",
            market_signal.get("direction", "neutral"),
            market_signal.get("confidence", 50.0),
            (market_signal.get("components") or {}).get("buildup", {}).get("type", "neutral"),
            (market_signal.get("components") or {}).get("pcr_trend", {}).get("trend", "neutral"),
            (market_signal.get("components") or {}).get("oi_shift", {}).get("call_shift", "same"),
            (market_signal.get("components") or {}).get("oi_shift", {}).get("put_shift", "same"),
            (market_signal.get("components") or {}).get("volume_spike", {}).get("spike", False),
        )
        
        # Calculate historical volatility if requested
        hv_metrics = {"hv10": None, "hv20": None, "hv60": None, "weighted_hv": None}
        trend_context = {"trend": "neutral"}
        hist_prices = pd.DataFrame()
        if use_hv:
            logger.info("Calculating historical volatility...")
            # Fetch historical prices for HV calculation
            end_date = datetime.now()
            start_date = end_date - timedelta(days=252)  # 1 year
            
            hist_prices = self.fetch_historical_prices(
                security_id=security_id,
                exchange_segment=security_segment,
                from_date=start_date.strftime("%Y-%m-%d"),
                to_date=end_date.strftime("%Y-%m-%d")
            )
            
            if not hist_prices.empty:
                hv_metrics = self.calculate_hv_metrics(hist_prices)
                trend_context = self.determine_trend_context(hist_prices)
                weighted_hv = hv_metrics.get("weighted_hv")
                if weighted_hv is not None and not pd.isna(weighted_hv):
                    logger.info(
                        "HV Benchmark: weighted=%.2f%% | hv10=%.2f | hv20=%.2f | hv60=%.2f",
                        weighted_hv,
                        hv_metrics.get("hv10") or float("nan"),
                        hv_metrics.get("hv20") or float("nan"),
                        hv_metrics.get("hv60") or float("nan"),
                    )
                    logger.info("Trend Context: %s", trend_context.get("trend"))
                else:
                    logger.warning("Historical volatility calculation returned no usable value")
            else:
                logger.warning("Could not calculate HV for %s", security_name)
        elif hist_prices.empty:
            historical_ivs = self.fetch_historical_iv(security_id, security_segment)

        if historical_ivs:
            logger.info("Historical IV samples: %s", len(historical_ivs))

        atm_strike = atm_context.get("atm_strike")
        expected_move = atm_context.get("expected_move")
        if atm_strike and expected_move:
            strike_range = expected_move * 2
        else:
            strike_range = spot_price * 0.1

        filtered_option_chain = {
            strike: data
            for strike, data in option_chain.items()
            if atm_strike is not None and abs(float(strike) - atm_strike) <= strike_range
        }
        if not filtered_option_chain:
            filtered_option_chain = dict(option_chain)
        
        # Scan each strike
        self._scan_quality_stats = {"pre_quality": 0, "post_quality": 0}
        self._score_debug_candidates = []
        all_discounted = []
        
        for strike_str, strike_data in filtered_option_chain.items():
            strike_price = float(strike_str)
            
            discounted = self.scan_single_strike(
                strike_data=strike_data,
                strike_price=strike_price,
                spot_price=spot_price,
                option_chain=option_chain,
                historical_ivs=historical_ivs,
                hv_metrics=hv_metrics,
                atm_context=atm_context,
                expected_move=expected_move,
                dte=dte,
                trend=trend_context.get("trend", "neutral"),
                has_iv_history=has_iv_history,
                call_mean=call_mean,
                call_std=call_std,
                put_mean=put_mean,
                put_std=put_std,
                call_ivs=call_ivs,
                put_ivs=put_ivs,
                call_avg_volume=call_avg_volume,
                put_avg_volume=put_avg_volume,
                iv_behavior=iv_behavior,
                premarket_ctx=premarket_ctx,
                pcr_value=pcr_value,
                sentiment_bias=sentiment_bias,
                market_signal=market_signal,
                expiry=expiry,
            )
            
            all_discounted.extend(discounted)
        
        logger.info(
            "Underlying %s opportunities | before_quality_gate=%s | after_quality_gate=%s",
            security_name,
            self._scan_quality_stats.get("pre_quality", 0),
            self._scan_quality_stats.get("post_quality", 0),
        )
        logger.info("Total discounted options: %s", len(all_discounted))

        before_underlying_cap = len(all_discounted)
        if all_discounted:
            for item in all_discounted:
                final_rank_score = (
                    item["score"] +
                    math.log((item.get("volume") or 0) + 1) * 2 +
                    math.log((item.get("oi") or 0) + 1) * 1.5
                )
                delta_value = abs(item.get("delta") or 0)
                if 0.2 <= delta_value <= 0.4:
                    final_rank_score += 5
                if item.get("market_direction") == ("bullish" if item.get("type") == "CALL" else "bearish"):
                    market_confidence = item.get("market_confidence")
                    market_confidence = market_confidence if market_confidence is not None else 50.0
                    final_rank_score += min(8.0, max(0.0, (market_confidence - 50.0) / 5.0))
                item["final_rank_score"] = round(final_rank_score, 2)

            all_discounted = sorted(
                all_discounted,
                key=lambda item: item.get("final_rank_score", item.get("score", 0)),
                reverse=True,
            )[:8]
            logger.info("Selected top %s trades for %s", len(all_discounted), security_name)
        else:
            all_discounted = []

        logger.info(
            "Underlying %s opportunities | before_stock_cap=%s | after_stock_cap=%s",
            security_name,
            before_underlying_cap,
            len(all_discounted),
        )
        logger.info("Final selected count: %s", len(all_discounted))
        if len(all_discounted) == 0:
            top_candidates = sorted(
                getattr(self, "_score_debug_candidates", []),
                key=lambda item: item.get("score") or 0,
                reverse=True,
            )[:5]
            for candidate in top_candidates:
                logger.info(
                    "Top rejected candidate | strike=%s type=%s score=%s breakdown=%s iv=%s oi=%s volume=%s",
                    candidate.get("strike"),
                    candidate.get("type"),
                    candidate.get("score"),
                    candidate.get("score_breakdown"),
                    candidate.get("iv"),
                    candidate.get("oi"),
                    candidate.get("volume"),
                )
        self._score_debug_candidates = []
        logger.info("Expiry %s -> trades found: %s", expiry, len(all_discounted))
        logger.info("Completed scan for %s with %s discounted opportunities", security_name, len(all_discounted))
        
        return all_discounted
    
    # ==================== 4. MULTI-STOCK SCANNER ====================
    
    def scan_all_fno_stocks(self, security_ids=None, expiry=None, min_discount_score=40):
        """
        Scan all FNO stocks for discounted premiums
        
        Args:
            security_ids: Dict of security IDs to scan (None for all)
            expiry: Specific expiry (None for nearest)
            min_discount_score: Minimum score to include
        
        Returns:
            pd.DataFrame: All discounted opportunities
        """
        if security_ids is None:
            security_ids = self.fno_stocks
        
        all_opportunities = []
        
        for sec_id, sec_name in security_ids.items():
            try:
                # Determine segment
                if sec_name in ['NIFTY', 'BANKNIFTY']:
                    segment = "IDX_I"
                else:
                    segment = "NSE_FNO"

                expiries = [expiry] if expiry else self.get_expiry_list(sec_id, segment)
                if not expiries:
                    logger.warning("No expiries found for %s (%s)", sec_name, segment)
                    continue

                for current_expiry in expiries:
                    discounted = self.scan_underlying(
                        security_id=sec_id,
                        security_segment=segment,
                        security_name=sec_name,
                        expiry=current_expiry,
                        use_hv=True
                    )

                    # Add stock info and filter
                    for opt in discounted:
                        if opt['score'] >= min_discount_score:
                            opt['symbol'] = sec_name
                            opt['security_id'] = sec_id
                            opt['expiry'] = opt.get('expiry') or current_expiry
                            all_opportunities.append(opt)

                    # Rate limiting
                    time.sleep(1)
                
            except Exception:
                logger.exception("Error scanning %s", sec_name)
        
        # Convert to DataFrame
        if all_opportunities:
            df = pd.DataFrame(all_opportunities)
            df = df.sort_values('score', ascending=False)
            logger.info("Global opportunities before_cap=%s", len(df))
            df = pd.DataFrame(self.select_top_trades(df, limit=500, max_per_direction=260))
            logger.info("Global opportunities after_cap=%s", len(df))
            return df
        else:
            return pd.DataFrame()
    
    # ==================== 5. REPORTING ====================
    
    def generate_report(self, opportunities_df):
        """
        Generate a formatted report of discounted premiums
        
        Args:
            opportunities_df: DataFrame from scan_all_fno_stocks
        """
        if opportunities_df.empty:
            logger.info("No discounted premiums found matching criteria")
            return
        
        logger.info("%s", "=" * 100)
        logger.info("DISCOUNTED PREMIUM OPPORTUNITIES REPORT")
        logger.info("%s", "=" * 100)
        
        for _, row in opportunities_df.iterrows():
            hv_text = f"{row['hv']:.2f}%" if pd.notna(row['hv']) else "N/A"
            skew_text = f"{row['skew_discount']:.2f}%" if pd.notna(row['skew_discount']) else "N/A"
            expected_move_text = f"{row['expected_move']:.2f}" if pd.notna(row['expected_move']) else "N/A"
            pcr_text = f"{row['pcr_value']:.2f}" if pd.notna(row.get('pcr_value')) else "N/A"
            relative_skew_text = f"{row['relative_skew']:.2f}" if pd.notna(row.get('relative_skew')) else "N/A"

            logger.info("%s - %s @ Strike %.2f", row['symbol'], row['strategy'], row['strike'])
            logger.info("%s", "-" * 50)
            logger.info(
                "Score: %.2f/100 | Type: %s | Trade Type: %s | Vol Mode: %s",
                row['score'],
                row['type'],
                str(row.get('trade_type', 'volatility')).title(),
                row['vol_mode'],
            )
            if pd.notna(row['iv_rank']) and pd.notna(row['iv_percentile']):
                logger.info("IV: %.2f%% | IV Rank: %.2f | IV Percentile: %.2f", row['iv'], row['iv_rank'], row['iv_percentile'])
            else:
                logger.info("IV: %.2f%% | IV Context: %s", row['iv'], row['iv_context'])
            logger.info(
                "IV Regime: %s | Conviction: %s",
                row.get('iv_regime', 'MID'),
                row.get('conviction', 'LOW'),
            )
            logger.info("HV Benchmark: %s | Skew Discount vs ATM: %s", hv_text, skew_text)
            logger.info(
                "PCR: %s | Bias: %s | Relative Skew: %s",
                pcr_text,
                str(row.get('sentiment_bias', 'neutral')).title(),
                relative_skew_text,
            )
            logger.info("Moneyness: %.1f%% | Expected Move: %s | EM Ratio: %.2f", row['moneyness'], expected_move_text, row['expected_move_ratio'])
            logger.info(
                "Mid Price: %.2f (Bid: %.2f / Ask: %.2f)",
                row['mid_price'],
                row['bid'],
                row['ask'],
            )
            logger.info(
                "Entry: %.2f | Stop: %.2f | Target: %.2f | R/R: %s",
                row['entry'],
                row['stop_loss'],
                row['target'],
                row['risk_reward'] if pd.notna(row['risk_reward']) else "N/A",
            )
            logger.info("OI: %s | Volume: %s", f"{int(row['oi']):,}", f"{int(row['volume']):,}")
            logger.info(
                "Greeks: delta=%.3f | theta=%.2f | vega=%.2f | Trend=%s",
                row['delta'],
                row['theta'],
                row['vega'],
                row['trend'],
            )
            logger.info("Factors:")
            for factor in row['reason'][:4]:
                logger.info("  - %s", factor)
        
        # Summary statistics
        logger.info("%s", "=" * 100)
        logger.info("SUMMARY STATISTICS")
        logger.info("%s", "=" * 100)
        logger.info("Total Opportunities: %s", len(opportunities_df))
        logger.info("Average Score: %.1f", opportunities_df['score'].mean())
        logger.info("Average IV: %.2f%%", opportunities_df['iv'].mean())

        avg_hv = opportunities_df['hv'].mean()
        avg_iv_rank = opportunities_df['iv_rank'].mean()
        logger.info("Average HV: %s", f"{avg_hv:.2f}%" if pd.notna(avg_hv) else "N/A")
        logger.info("Average IV Rank: %s", f"{avg_iv_rank:.2f}" if pd.notna(avg_iv_rank) else "N/A")
        logger.info("Breakdown by Volatility Mode:")
        logger.info("\n%s", opportunities_df.groupby('vol_mode')['symbol'].count().to_string())
        
        logger.info("Breakdown by Strategy:")
        type_stats = opportunities_df.groupby('strategy').agg({
            'score': 'mean',
            'iv': 'mean',
            'symbol': 'count'
        }).round(2)
        type_stats.columns = ['Avg Score', 'Avg IV', 'Count']
        logger.info("\n%s", type_stats.to_string())


# ==================== USAGE EXAMPLE ====================

if __name__ == "__main__":
    load_dotenv(dotenv_path=Path(".env"))

    # Initialize scanner with your Dhan credentials
    scanner = DiscountedPremiumScanner(
        hardtoken=os.getenv("DHAN_ACCESS_TOKEN"),
        client_id=os.getenv("DHAN_CLIENT_ID")
    )
    
    # Option 1: Scan a single underlying (e.g., NIFTY)
    logger.info("Scanning NIFTY for discounted premiums...")
    nifty_opportunities = scanner.scan_underlying(
        security_id=13,
        security_segment="IDX_I",
        security_name="NIFTY",
        expiry=None  # Uses nearest expiry
    )
    
    # Option 2: Scan all FNO stocks
    logger.info("Scanning all FNO stocks...")
    all_opportunities = scanner.scan_all_fno_stocks(
        min_discount_score=40
    )
    all_opportunities = reduce_to_one_per_symbol_expiry(all_opportunities)
    
    # Generate report
    scanner.generate_report(all_opportunities)
    
    # Save to CSV
    if not all_opportunities.empty:
        all_opportunities.to_csv("discounted_premiums.csv", index=False)
        logger.info("Results saved to discounted_premiums.csv")

    scanner.send_clean_telegram(all_opportunities)
