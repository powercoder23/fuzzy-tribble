import os
import logging
import math
from pathlib import Path
import sqlite3

import pandas as pd
import numpy as np
import requests
from upstox_adapter import UpstoxDhanAdapter
from upstox_token_manager import load_upstox_token
from dotenv import load_dotenv
from datetime import date, datetime, timedelta
import time
# FIX 7: removed `from scipy import stats` — it was imported but never used
# anywhere in this module (percentile/rank math uses numpy directly).
import warnings
from f_o_stocks_list import get_stock_futures
from load_scrip_master_sqlite import update_scrip_master, get_security_id_symbol_map
from discount_config import (
    MIN_IV_SAMPLES,
    CHAIN_API_MIN_INTERVAL_SEC,
    CHAIN_API_RETRY_BACKOFF_SEC,
    CHAIN_API_MAX_RETRIES,           # FIX 1: retry budget for the chain endpoint
    CHAIN_API_BACKOFF_MULTIPLIER,    # FIX 1: exponential backoff multiplier
    LIQUIDITY,
    LOOSE_LIQUIDITY,                 # FIX 4: legacy loose thresholds (opt-in)
    ALLOW_LOOSE_LIQUIDITY,           # FIX 4: toggle strict vs loose gates
    STRIKE,
    MIN_DTE_DAYS,
    MIN_SCORE,
    TRADE_PLAN,
    STRONG_LIQUIDITY,
    NSE_HOLIDAYS,                    # FIX 3: holiday-aware DTE calendar
    ENABLE_DIRECTIONAL_CONFIRMATION, # FIX 8: directional confirmation toggle
    DIRECTIONAL_WEIGHT,              # FIX 8: directional blend weight
    ENABLE_FUTURES_OI_CONFIRMATION,  # FIX 9: futures-OI confirmation toggle
    FUTURES_OI_BONUS,               # FIX 9: directional bonus on confirming buildup
)
warnings.filterwarnings('ignore')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# IV_HISTORY_FILE = Path("iv_history.csv")
IV_HISTORY_FILE = Path("data") / "iv_history.db"

# MIN_IV_SAMPLES, CHAIN_API_MIN_INTERVAL_SEC, CHAIN_API_RETRY_BACKOFF_SEC and the
# scanning thresholds now live in discount_config.py (imported above).
# Dhan rate-limits the option-chain family of endpoints to 1 request / 3 seconds;
# breaching it produces silent failures (empty body, no error code).
DEFAULT_FNO_STOCKS = {
    13:    "NIFTY",
    14:    "BANKNIFTY",
    1333:  "HDFCBANK",
    2885:  "RELIANCE",
    4963:  "ICICIBANK",
    1594:  "INFY",
    11536: "TCS",
    1394:  "HINDUNILVR",
    3045:  "SBIN",
}

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


def get_trading_days_to_expiry(expiry):
    """
    Estimate trading-day distance to an expiry date by ignoring weekends.
    This is used by other strategy modules to select near-expiry contracts
    without requiring a full holiday calendar.
    """
    if not expiry:
        return 0

    try:
        expiry_date = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
    except ValueError:
        return 0

    today = date.today()
    if expiry_date <= today:
        return 0

    days = 0
    current = today
    while current < expiry_date:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days


def get_actual_trading_days_to_expiry(expiry, holidays=None):
    """
    FIX 3: trading-day distance to expiry that excludes BOTH weekends AND NSE
    trading holidays.

    `holidays` is an iterable of 'YYYY-MM-DD' strings; it defaults to
    discount_config.NSE_HOLIDAYS. When that list is empty / unavailable / has
    only malformed entries, this degrades exactly to the weekend-only
    get_trading_days_to_expiry() behaviour above, so existing logic is never
    broken — it simply becomes more accurate once holidays are populated.
    """
    if not expiry:
        return 0

    try:
        expiry_date = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
    except ValueError:
        return 0

    today = date.today()
    if expiry_date <= today:
        return 0

    # Build a set of holiday dates, ignoring malformed entries defensively so a
    # single bad config value cannot raise.
    holiday_set = set()
    source = NSE_HOLIDAYS if holidays is None else holidays
    for item in source or []:
        try:
            holiday_set.add(datetime.strptime(str(item)[:10], "%Y-%m-%d").date())
        except (ValueError, TypeError):
            continue

    days = 0
    current = today
    while current < expiry_date:
        if current.weekday() < 5 and current not in holiday_set:
            days += 1
        current += timedelta(days=1)
    return days


def parse_strike_key(value):
    """
    FIX 5: best-effort conversion of an option-chain strike key to float.

    Strike keys can arrive as ints, floats, or numeric strings such as
    '25000' or '25000.0'. Returns None for anything that cannot be parsed so
    callers can skip the malformed strike instead of crashing on float().
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_chain_metrics(option_chain):
    """
    Summarize option chain-level OI / volume metrics for macro flow context.
    Used by the IV collector and directional strategy to understand whether
    call-side or put-side positioning is currently dominant.
    """
    metrics = {
        "total_call_oi": 0.0,
        "total_put_oi": 0.0,
        "total_call_volume": 0.0,
        "total_put_volume": 0.0,
        "max_oi_strike_call": None,
        "max_oi_strike_put": None,
    }

    if not isinstance(option_chain, dict):
        return metrics

    max_call_oi = -1.0
    max_put_oi = -1.0

    for strike_key, strike_data in option_chain.items():
        if not isinstance(strike_data, dict):
            continue

        ce = strike_data.get("ce") or {}
        pe = strike_data.get("pe") or {}

        call_oi = float(ce.get("oi") or 0)
        put_oi = float(pe.get("oi") or 0)
        call_volume = float(ce.get("volume") or 0)
        put_volume = float(pe.get("volume") or 0)

        metrics["total_call_oi"] += call_oi
        metrics["total_put_oi"] += put_oi
        metrics["total_call_volume"] += call_volume
        metrics["total_put_volume"] += put_volume

        if call_oi >= max_call_oi:
            max_call_oi = call_oi
            metrics["max_oi_strike_call"] = strike_key
        if put_oi >= max_put_oi:
            max_put_oi = put_oi
            metrics["max_oi_strike_put"] = strike_key

    return metrics


class DiscountedPremiumScanner:
    """
    Scanner to identify options trading at discounted premiums
    Using Dhan API with proper authentication pattern
    """
    
    def __init__(self, hardtoken=None, client_id=None, store_intraday=False,
                 upstox_adapter=None):
        """Initialize scanner with an Upstox adapter (auto-created if not provided)."""
        if upstox_adapter is None:
            upstox_adapter = UpstoxDhanAdapter(load_upstox_token())
        self.client_id = "upstox"
        self.context   = None
        self.dhan      = upstox_adapter
        self.risk_free_rate = 0.065  # 6.5% - update from RBI periodically
        self.iv_history_file = IV_HISTORY_FILE
        self.store_intraday = store_intraday
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self._last_chain_api_call = 0.0
        self._expiry_cache = {}

        # FIX 4: resolve the effective liquidity gate once. ALLOW_LOOSE_LIQUIDITY
        # (config constant OR the ALLOW_LOOSE_LIQUIDITY env var) reverts to the
        # historical loose thresholds; otherwise the stricter LIQUIDITY tiers
        # apply. Per-strike checks read self.liquidity so the choice is central.
        _env_loose = os.getenv("ALLOW_LOOSE_LIQUIDITY")
        if _env_loose is not None:
            allow_loose = _env_loose.strip().lower() in ("1", "true", "yes", "on")
        else:
            allow_loose = ALLOW_LOOSE_LIQUIDITY
        self.liquidity = LOOSE_LIQUIDITY if allow_loose else LIQUIDITY
        logger.info(
            "Liquidity gates: %s (min_oi=%s, min_volume=%s, max_spread_pct=%s)",
            "LOOSE" if allow_loose else "STRICT",
            self.liquidity["min_oi"], self.liquidity["min_volume"],
            self.liquidity["max_spread_pct"],
        )

        # FIX 9: lazily-created futures-OI provider + per-run classification
        # cache. Default-off via ENABLE_FUTURES_OI_CONFIRMATION; never required
        # for a scan and fully fail-open (see _get_futures_classification).
        self._futures_oi_provider = None
        self._futures_oi_cache = {}

        self.fno_stocks = self.load_fno_stocks()

    def _throttle_chain_api(self):
        """Enforce Dhan's 1-req-per-3-sec limit on the option-chain endpoint family."""
        elapsed = time.monotonic() - self._last_chain_api_call
        if elapsed < CHAIN_API_MIN_INTERVAL_SEC:
            time.sleep(CHAIN_API_MIN_INTERVAL_SEC - elapsed)
        self._last_chain_api_call = time.monotonic()

    @staticmethod
    def _is_rate_limit_response(response):
        """Dhan signals rate-limit with status=failure, empty data, and null error fields."""
        if not isinstance(response, dict) or response.get("status") == "success":
            return False
        data = response.get("data")
        if data not in (None, "", [], {}):
            return False
        remarks = response.get("remarks") or {}
        if not isinstance(remarks, dict):
            return False  # Upstox errors return a plain string, not a Dhan-style error dict
        return all(remarks.get(k) is None for k in ("error_code", "error_type", "error_message"))

    @staticmethod
    def _option_chain_is_empty(response):
        """
        FIX 1: a *successful* chain response that carries no strikes is a
        transient glitch worth retrying (Dhan occasionally returns an empty
        'oc' right after session priming). Non-success responses are handled by
        the rate-limit / error paths, so we don't double-count them here.
        """
        if not isinstance(response, dict) or response.get("status") != "success":
            return False
        data = unwrap_dhan_payload(response.get("data") or {})
        oc = data.get("oc") if isinstance(data, dict) else None
        return not isinstance(oc, dict) or len(oc) == 0

    @staticmethod
    def _expiry_list_is_empty(response):
        """FIX 1: a successful expiry response with no payload is retryable."""
        if not isinstance(response, dict) or response.get("status") != "success":
            return False
        return response.get("data") in (None, "", [], {})

    def _call_chain_api(self, fn, is_empty_result=None, **kwargs):
        """
        Throttle + retry wrapper around Dhan's option-chain endpoint family.

        FIX 1: the previous implementation retried at most ONCE on Dhan's silent
        rate-limit response. It now retries up to CHAIN_API_MAX_RETRIES times
        with exponential backoff and covers more failure modes. A retry fires on:
          * the silent rate-limit response (_is_rate_limit_response)
          * an empty/invalid payload, judged by the optional `is_empty_result`
            predicate the caller supplies (empty option chain / empty expiry list)
          * a temporary network error raised by `fn` (requests timeouts, etc.)

        Backoff before retry N (1-indexed) is
            CHAIN_API_RETRY_BACKOFF_SEC * CHAIN_API_BACKOFF_MULTIPLIER ** (N - 1)
        => 4s, 8s, 16s with the default config. Every retry attempt is logged.
        The steady-state 1-req/3s throttle is still honoured (the backoff sleeps
        only add spacing), so we never exceed the broker's rate limit.
        """
        last_response = None
        retry_reason = ""
        for attempt in range(CHAIN_API_MAX_RETRIES + 1):
            if attempt == 0:
                # First call: honour the normal steady-state throttle.
                self._throttle_chain_api()
            else:
                # Retry: wait an exponentially growing backoff. The sleep itself
                # also satisfies the rate-limit spacing, so we just refresh the
                # last-call marker afterwards.
                backoff = CHAIN_API_RETRY_BACKOFF_SEC * (
                    CHAIN_API_BACKOFF_MULTIPLIER ** (attempt - 1)
                )
                logger.warning(
                    "Chain API retry %s/%s (reason: %s); backing off %.1fs",
                    attempt, CHAIN_API_MAX_RETRIES, retry_reason, backoff,
                )
                time.sleep(backoff)
                self._last_chain_api_call = time.monotonic()

            try:
                response = fn(**kwargs)
            except Exception as exc:
                # Temporary network/API error -> retry until the budget is spent,
                # then re-raise so the caller's existing error handling kicks in.
                retry_reason = f"network error: {exc}"
                last_response = None
                if attempt >= CHAIN_API_MAX_RETRIES:
                    logger.error("Chain API exhausted retries after repeated network errors")
                    raise
                continue

            last_response = response

            if self._is_rate_limit_response(response):
                retry_reason = "rate-limited"
                if attempt >= CHAIN_API_MAX_RETRIES:
                    logger.error("Chain API still rate-limited after %s retries", CHAIN_API_MAX_RETRIES)
                    return response
                continue

            if is_empty_result is not None and is_empty_result(response):
                retry_reason = "empty result"
                if attempt >= CHAIN_API_MAX_RETRIES:
                    logger.warning("Chain API returned empty result after %s retries", CHAIN_API_MAX_RETRIES)
                    return response
                continue

            return response

        return last_response

    def extract_chain_metrics(self, option_chain):
        """Return chain-level OI/volume metrics for the given option chain."""
        return extract_chain_metrics(option_chain)

    @staticmethod
    def _expiry_cache_key(security_id, segment):
        # All NSE_FNO stocks share the same monthly expiry calendar; one cache entry covers them.
        if segment == "NSE_FNO":
            return ("__shared__", "NSE_FNO")
        return (str(security_id), segment)
        
    # ==================== 1. DATA FETCHING METHODS ====================

    def load_fno_stocks(self):
        """
        Build the F&O universe dynamically by combining:
        1. NSE's live stock-futures symbols
        2. Dhan scrip-master security-id resolution
        """
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

        return dict(sorted(resolved.items(), key=lambda item: item[1]))

    def send_telegram_summary(self, opportunities_df):
        """Send a short end-of-run summary to Telegram."""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            logger.info("Telegram alert skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
            return

        if opportunities_df is None or opportunities_df.empty:
            message = (
                "Options Scanner Summary\n"
                f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                "No qualifying opportunities found."
            )
        else:
            top_rows = opportunities_df.head(5)
            lines = [
                "Options Scanner Summary",
                f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                f"Matches: {len(opportunities_df)}",
                "Top ideas:",
            ]
            for _, row in top_rows.iterrows():
                lines.append(
                    f"{row['symbol']} {row['type']} {row['strike']:.0f} | "
                    f"{row['strategy']} | Score {row['score']:.1f}"
                )
            message = "\n".join(lines)

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": self.telegram_chat_id,
                    "text": message,
                },
                timeout=15,
            )
            response.raise_for_status()
            logger.info("Telegram summary sent")
        except Exception:
            logger.exception("Failed to send Telegram summary")
    
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
        response = self._call_chain_api(
            self.dhan.option_chain,
            is_empty_result=self._option_chain_is_empty,  # FIX 1: retry on empty chain
            under_security_id=underlying_security_id,
            under_exchange_segment=underlying_segment,
            expiry=expiry,
        )
        if response.get("status") != "success":
            logger.error(
                "Failed to fetch option chain for %s (%s), expiry %s: %s",
                underlying_security_id,
                underlying_segment,
                expiry,
                response,
            )
        return response
    
    def get_expiry_list(self, underlying_security_id, underlying_segment):
        """
        Get all available expiries for an underlying
        
        Args:
            underlying_security_id: Security ID
            underlying_segment: "IDX_I" or "NSE_FNO"
        
        Returns:
            list: List of expiry dates
        """
        cache_key = self._expiry_cache_key(underlying_security_id, underlying_segment)
        cached = self._expiry_cache.get(cache_key)
        if cached:
            # FIX 6/11: all NSE_FNO stocks resolve to one shared cache key, so
            # every stock after the first is a cache HIT and triggers no API
            # call. Logged at DEBUG to confirm reuse without spamming INFO.
            logger.debug(
                "Expiry cache HIT for %s (%s) via key %s",
                underlying_security_id, underlying_segment, cache_key,
            )
            return cached
        logger.debug(
            "Expiry cache MISS for %s (%s); fetching expiry list",
            underlying_security_id, underlying_segment,
        )

        response = self._call_chain_api(
            self.dhan.expiry_list,
            is_empty_result=self._expiry_list_is_empty,  # FIX 1: retry on empty expiry list
            under_security_id=underlying_security_id,
            under_exchange_segment=underlying_segment,
        )
        if response.get("status") != "success":
            logger.error(
                "Failed to fetch expiries for %s (%s): %s",
                underlying_security_id,
                underlying_segment,
                response,
            )
            return []

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

        expiries = sorted(set(expiries))
        if expiries:
            self._expiry_cache[cache_key] = expiries
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
                oi=False
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

    def fetch_historical_iv(self, security_id, exchange_segment, lookback_days=252,
                             include_intraday=False):
        """
        Load persisted ATM IV history from SQLite DB (read-only).
        Writes are handled by a separate service.

        FIX 2: defaults to daily rows only so intraday snapshots don't inflate
        IV Rank / IV Percentile. Pass include_intraday=True to allow all rows.
        """
        if not self.iv_history_file.exists():
            return []

        try:
            with sqlite3.connect(str(self.iv_history_file), timeout=30.0) as conn:
                if include_intraday:
                    cursor = conn.execute("""
                        SELECT atm_iv
                        FROM iv_history
                        WHERE security_id = ?
                        AND atm_iv IS NOT NULL
                        AND atm_iv >= 1.0
                        AND atm_iv <= 200.0
                        ORDER BY timestamp DESC
                        LIMIT ?
                    """, (str(security_id), lookback_days))
                else:
                    # FIX 2: restrict to daily snapshots for clean IV Rank/Pct computation.
                    cursor = conn.execute("""
                        SELECT atm_iv
                        FROM iv_history
                        WHERE security_id = ?
                        AND data_type = 'daily'
                        AND atm_iv IS NOT NULL
                        AND atm_iv >= 1.0
                        AND atm_iv <= 200.0
                        ORDER BY timestamp DESC
                        LIMIT ?
                    """, (str(security_id), lookback_days))
                rows = cursor.fetchall()
                return [row[0] for row in reversed(rows)]
        except Exception:
            logger.exception("Failed to read IV history from DB: %s", self.iv_history_file)
            return []

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
        # FIX 2: validate the historical frame before touching it. Missing the
        # required 'close' column would raise KeyError and abort the scan; we
        # now log once and return None so the caller continues safely.
        if price_df is None or price_df.empty or len(price_df) < window:
            return None
        if 'close' not in price_df.columns:
            logger.warning(
                "HV (window=%s) skipped: required 'close' column missing (columns=%s)",
                window, list(price_df.columns),
            )
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
        # FIX 2: validate the required column up-front so a malformed historical
        # frame degrades gracefully (all-None HV) instead of crashing the scan.
        # This logs at most once per underlying rather than once per HV window.
        if price_df is None or price_df.empty or 'close' not in price_df.columns:
            if price_df is not None and not price_df.empty and 'close' not in price_df.columns:
                logger.warning(
                    "HV metrics skipped: required 'close' column missing (columns=%s)",
                    list(price_df.columns),
                )
            return {"hv10": None, "hv20": None, "hv60": None, "weighted_hv": None}

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

    def extract_atm_reference_ivs(self, option_chain, spot_price):
        empty = {"atm_strike": None, "atm_call_iv": None, "atm_put_iv": None, "atm_iv": None}
        if not option_chain:
            return empty

        # FIX 5: defensively parse strike keys (int / float / "25000" / "25000.0")
        # instead of calling float() inline. Malformed keys are logged and
        # skipped so a single bad key can never crash ATM selection.
        valid_strikes = []
        for key in option_chain.keys():
            parsed = parse_strike_key(key)
            if parsed is None:
                logger.warning("Ignoring malformed strike key %r during ATM selection", key)
                continue
            valid_strikes.append((parsed, key))
        if not valid_strikes:
            logger.warning("No valid numeric strike keys found; cannot determine ATM")
            return empty

        atm_parsed, atm_strike = min(valid_strikes, key=lambda pair: abs(pair[0] - spot_price))
        atm_data = option_chain.get(atm_strike, {})
        atm_call = atm_data.get("ce") or {}
        atm_put = atm_data.get("pe") or {}
        atm_call_iv = atm_call.get("implied_volatility") or None
        atm_put_iv = atm_put.get("implied_volatility") or None
        atm_call_oi = atm_call.get("oi") or 0
        atm_put_oi = atm_put.get("oi") or 0
        valid = [value for value in [atm_call_iv, atm_put_iv] if value and value > 0]
        atm_iv = float(np.mean(valid)) if valid else None
        return {
            # FIX 5: report the already-parsed numeric strike (atm_strike is the
            # original, possibly-string key used only to index the chain above).
            "atm_strike": float(atm_parsed),
            "atm_call_iv": atm_call_iv,
            "atm_put_iv": atm_put_iv,
            "atm_call_oi": atm_call_oi,
            "atm_put_oi": atm_put_oi,
            "atm_iv": atm_iv,
        }

    # IV snapshot persistence is owned by the IV collector service
    # (collectors/iv_collector_service.py); the discount scanner is a
    # read-only consumer of iv_history.db.

    # ==================== 3. DISCOUNTED PREMIUM DETECTION ====================

    def build_strategy_plan(self, option_type, strike_price, spot_price, mid_price, option_chain,
                            expected_move, trend, score):
        """Create tradable strategy suggestions from a shortlisted option."""
        # FIX 5: skip malformed strike keys so one bad key can't abort planning.
        strike_keys = sorted(
            parsed for parsed in (parse_strike_key(key) for key in option_chain.keys())
            if parsed is not None
        )
        if option_type == "CALL":
            candidate_shorts = [strike for strike in strike_keys if strike > strike_price]
            short_strike = candidate_shorts[0] if candidate_shorts else None
        else:
            candidate_shorts = [strike for strike in strike_keys if strike < strike_price]
            short_strike = candidate_shorts[-1] if candidate_shorts else None

        entry = mid_price
        stop_loss = mid_price * TRADE_PLAN["stop_loss_mult"] if mid_price else 0
        target = mid_price * TRADE_PLAN["target_mult"] if mid_price else 0
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

    def _get_futures_classification(self, symbol):
        """
        FIX 9: classify the underlying's nearest-expiry futures into one of the
        OI quadrants (LONG_BUILDUP / SHORT_BUILDUP / SHORT_COVERING /
        LONG_UNWINDING) by reusing the isolated, fail-open oi_validator module.

        Returns None when the feature is disabled OR futures OI is unavailable;
        the scan then proceeds exactly as before. The result is cached per
        symbol for the lifetime of the scanner so we make at most one futures
        fetch per underlying per run. Every failure path is swallowed — this
        never raises and never blocks a scan.
        """
        if not ENABLE_FUTURES_OI_CONFIRMATION:
            return None
        if symbol in self._futures_oi_cache:
            return self._futures_oi_cache[symbol]

        classification = None
        try:
            # Imported lazily so the discount scanner has no hard dependency on
            # the OI module unless the feature is actually switched on.
            from oi_validator import FuturesOIProvider, classify
            if self._futures_oi_provider is None:
                self._futures_oi_provider = FuturesOIProvider(scanner=self)
            snapshot = self._futures_oi_provider.fetch_snapshot(symbol)
            if snapshot is not None and getattr(snapshot, "available", False):
                classification = classify(snapshot.price_change_pct, snapshot.oi_change_pct)
                logger.debug(
                    "Futures OI for %s: %s (dP %.2f%% / dOI %.2f%%)",
                    symbol, classification, snapshot.price_change_pct, snapshot.oi_change_pct,
                )
            else:
                logger.debug("Futures OI unavailable for %s; skipping confirmation", symbol)
        except Exception:
            # Fail-open: any import/data error means "no confirmation available".
            logger.debug("Futures OI classification raised for %s", symbol, exc_info=True)
            classification = None

        self._futures_oi_cache[symbol] = classification
        return classification

    def compute_directional_score(self, option_type, spot_price, trend_context,
                                  atm_context, futures_classification=None):
        """
        FIX 8: directional confirmation layer (0-100).

        Cheap IV alone should not produce a high-confidence signal. This scores
        how well price structure + flow agree with the option's direction:

          CALL (ce) is bullish-confirmed when: spot > EMA20, EMA20 > EMA50,
              trend == "bullish", and ATM call OI dominates ATM put OI.
          PUT  (pe) is the bearish mirror.

        Each satisfied condition contributes an equal share of 100. When EMA
        structure is unavailable (insufficient history) we return a neutral 50
        so the layer never penalizes purely on missing data.

        FIX 9: when ENABLE_FUTURES_OI_CONFIRMATION is on AND a futures-OI
        classification is available, a confirming buildup (LONG for calls /
        SHORT for puts) adds FUTURES_OI_BONUS. Missing futures data is ignored
        (no penalty), so the scan is unaffected when the feature is off.
        """
        trend_context = trend_context or {}
        atm_context = atm_context or {}
        ema20 = trend_context.get("ema20")
        ema50 = trend_context.get("ema50")
        trend = trend_context.get("trend", "neutral")
        call_oi = atm_context.get("atm_call_oi") or 0
        put_oi = atm_context.get("atm_put_oi") or 0

        if ema20 is None or ema50 is None or spot_price is None:
            base = 50.0  # neutral: not enough structure to judge direction
        else:
            if option_type == "ce":
                checks = [spot_price > ema20, ema20 > ema50, trend == "bullish", call_oi > put_oi]
            else:
                checks = [spot_price < ema20, ema20 < ema50, trend == "bearish", put_oi > call_oi]
            base = (sum(1 for c in checks if c) / len(checks)) * 100.0

        # FIX 9: optional futures-OI buildup bonus (additive, fail-open).
        if ENABLE_FUTURES_OI_CONFIRMATION and futures_classification:
            if option_type == "ce" and futures_classification == "LONG_BUILDUP":
                base += FUTURES_OI_BONUS
            elif option_type == "pe" and futures_classification == "SHORT_BUILDUP":
                base += FUTURES_OI_BONUS

        return clip_score(base)

    def score_option(self, current_iv, weighted_hv, delta, vega, oi, volume, skew_discount,
                     expected_move_ratio, iv_rank=None, iv_percentile=None, vol_mode="skew",
                     has_expected_move=True):
        """Weighted quantitative score for option selection."""
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

        vega_score = clip_score(vega * 400) if vega is not None else 20.0
        liquidity_score = clip_score((math.log1p(max(oi, 0)) * 12) + (math.log1p(max(volume, 0)) * 8))
        skew_score = clip_score(50 + skew_discount * 4) if skew_discount is not None else 40.0
        # Strike relevance only means something when we have a valid expected move.
        # Without one, expected_move_ratio is 0, which would otherwise hand every
        # strike a perfect (100) relevance score and inflate the composite. Fall
        # back to a neutral 50 in that case.
        if has_expected_move:
            relevance_score = clip_score(100 - (max(expected_move_ratio - 0.5, 0) / 1.0) * 100)
        else:
            relevance_score = 50.0

        if vol_mode == "historical":
            cheap_vol_score = clip_score(100 - ((iv_rank * 0.5) + (iv_percentile * 0.5)))
            final_score = (
                cheap_vol_score * 0.25 +
                hv_score * 0.20 +
                delta_score * 0.15 +
                vega_score * 0.10 +
                liquidity_score * 0.10 +
                skew_score * 0.10 +
                relevance_score * 0.10
            )
            component_scores = {
                "iv_regime": native_number(round(cheap_vol_score, 2)),
                "iv_vs_hv": native_number(round(hv_score, 2)),
                "delta": native_number(round(delta_score, 2)),
                "vega": native_number(round(vega_score, 2)),
                "liquidity": native_number(round(liquidity_score, 2)),
                "skew": native_number(round(skew_score, 2)),
                "strike_relevance": native_number(round(relevance_score, 2)),
            }
        else:
            final_score = (
                skew_score * 0.25 +
                hv_score * 0.25 +
                delta_score * 0.15 +
                vega_score * 0.10 +
                liquidity_score * 0.10 +
                relevance_score * 0.15
            )
            component_scores = {
                "skew": native_number(round(skew_score, 2)),
                "iv_vs_hv": native_number(round(hv_score, 2)),
                "delta": native_number(round(delta_score, 2)),
                "vega": native_number(round(vega_score, 2)),
                "liquidity": native_number(round(liquidity_score, 2)),
                "strike_relevance": native_number(round(relevance_score, 2)),
            }

        return {
            "score": round(final_score, 2),
            "component_scores": component_scores,
        }
    
    def scan_single_strike(self, strike_data, strike_price, spot_price, option_chain,
                          historical_ivs=None, hv_metrics=None, atm_context=None,
                          expected_move=None, dte=None, trend="neutral", hedging_mode=False,
                          has_iv_history=False, trend_context=None,
                          futures_classification=None, trading_dte=None):
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
            trend_context: Full EMA structure dict for the directional layer (FIX 8)
            futures_classification: Futures OI quadrant for this underlying (FIX 9)
            trading_dte: Holiday-aware trading days to expiry (FIX 3/10)

        Returns:
            list: Structured trade candidates
        """
        discounted = []
        weighted_hv = (hv_metrics or {}).get("weighted_hv")
        
        for option_type in ['ce', 'pe']:
            if option_type not in strike_data:
                continue
            
            opt = strike_data[option_type]
            
            oi = opt.get('oi', 0)
            volume = opt.get('volume', 0)
            delta = opt.get('greeks', {}).get('delta', 0)
            vega = opt.get('greeks', {}).get('vega', 0)
            abs_delta = abs(delta)

            # Skip illiquid or extremely low-probability options.
            # FIX 4: thresholds come from self.liquidity (strict by default; the
            # legacy loose values only when ALLOW_LOOSE_LIQUIDITY is set).
            # FIX 11: every rejection reason is logged at DEBUG (never INFO) so a
            # full scan stays quiet by default but is fully diagnosable on demand.
            if oi < self.liquidity["min_oi"] or volume < self.liquidity["min_volume"]:
                logger.debug(
                    "Reject %s %.2f: liquidity oi=%s (min %s) volume=%s (min %s)",
                    option_type, strike_price, oi, self.liquidity["min_oi"],
                    volume, self.liquidity["min_volume"],
                )
                continue
            if not hedging_mode and abs_delta < STRIKE["min_abs_delta"]:
                logger.debug(
                    "Reject %s %.2f: |delta|=%.3f below min %.2f",
                    option_type, strike_price, abs_delta, STRIKE["min_abs_delta"],
                )
                continue

            # Tradeability gate: require a live two-sided quote within the max
            # spread. Without this a strike with a huge bid-ask could rank highly
            # yet be impossible to enter without giving up the edge.
            bid = opt.get('top_bid_price') or 0
            ask = opt.get('top_ask_price') or 0
            if bid <= 0 or ask <= 0:
                logger.debug(
                    "Reject %s %.2f: no live two-sided quote (bid=%s ask=%s)",
                    option_type, strike_price, bid, ask,
                )  # FIX 11
                continue
            mid_price = (bid + ask) / 2
            if mid_price <= 0:
                continue
            spread_pct = (ask - bid) / mid_price
            if spread_pct > self.liquidity["max_spread_pct"]:
                logger.debug(
                    "Reject %s %.2f: spread %.3f > max %.3f",
                    option_type, strike_price, spread_pct, self.liquidity["max_spread_pct"],
                )  # FIX 11
                continue

            current_iv = opt.get('implied_volatility', 0)
            if current_iv == 0:
                logger.debug("Reject %s %.2f: IV unavailable", option_type, strike_price)  # FIX 11
                continue

            reference_iv = (atm_context or {}).get("atm_call_iv") if option_type == "ce" else (atm_context or {}).get("atm_put_iv")
            if not reference_iv:
                reference_iv = (atm_context or {}).get("atm_iv")
            skew_discount = ((reference_iv - current_iv) / reference_iv) * 100 if reference_iv and reference_iv > 0 else None
            iv_context = "below_atm" if reference_iv and current_iv < reference_iv else "above_atm"

            has_expected_move = bool(expected_move and expected_move > 0)
            distance_from_spot = abs(strike_price - spot_price)
            expected_move_ratio = (distance_from_spot / expected_move) if has_expected_move else 0
            if has_expected_move and expected_move_ratio > STRIKE["max_expected_move_ratio"]:
                logger.debug(
                    "Reject %s %.2f: EM ratio %.2f > max %.2f",
                    option_type, strike_price, expected_move_ratio,
                    STRIKE["max_expected_move_ratio"],
                )  # FIX 11
                continue

            vol_mode = "historical" if has_iv_history else "skew"
            iv_rank = self.calculate_iv_rank(current_iv, historical_ivs) if has_iv_history else None
            iv_percentile = self.calculate_iv_percentile(current_iv, historical_ivs) if has_iv_history else None
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
                has_expected_move=has_expected_move,
            )

            # FIX 8: blend the directional confirmation layer into the final
            # score. score_option() returns the UNCHANGED base composite (its
            # internal component weights are preserved); here we combine it with
            # the 0-100 directional_score using DIRECTIONAL_WEIGHT (default 15%).
            #   final = base * (1 - DIRECTIONAL_WEIGHT) + directional * DIRECTIONAL_WEIGHT
            # When ENABLE_DIRECTIONAL_CONFIRMATION is False, score == base_score
            # so results are byte-identical to the pre-change behaviour.
            base_score = score_details["score"]
            directional_score = self.compute_directional_score(
                option_type=option_type,
                spot_price=spot_price,
                trend_context=trend_context,
                atm_context=atm_context,
                futures_classification=futures_classification,
            )
            if ENABLE_DIRECTIONAL_CONFIRMATION:
                score = round(
                    base_score * (1 - DIRECTIONAL_WEIGHT)
                    + directional_score * DIRECTIONAL_WEIGHT,
                    2,
                )
            else:
                score = base_score

            if score < MIN_SCORE:
                logger.debug(
                    "Reject %s %.2f: score %.2f < MIN_SCORE %s (base=%.2f, directional=%.2f)",
                    option_type, strike_price, score, MIN_SCORE, base_score, directional_score,
                )  # FIX 11
                continue

            hv_gap = weighted_hv - current_iv if weighted_hv else None
            moneyness = ((strike_price - spot_price) / spot_price * 100) if option_type == 'ce' else ((spot_price - strike_price) / spot_price * 100)

            strategy_plan = self.build_strategy_plan(
                option_type='CALL' if option_type == 'ce' else 'PUT',
                strike_price=strike_price,
                spot_price=spot_price,
                mid_price=mid_price,
                option_chain=option_chain,
                expected_move=expected_move,
                trend=trend,
                score=score,
            )

            reasons = []
            if has_iv_history and iv_rank is not None and iv_rank <= 35:
                reasons.append(f"IV Rank is compressed at {iv_rank:.1f}")
            if has_iv_history and iv_percentile is not None and iv_percentile <= 35:
                reasons.append(f"IV Percentile is low at {iv_percentile:.1f}")
            if hv_gap and hv_gap > 0:
                reasons.append(f"IV is {hv_gap:.2f} points below weighted HV")
            if 0.15 <= abs_delta <= 0.40:
                reasons.append(f"Delta {delta:.2f} sits in the preferred directional range")
            if skew_discount and skew_discount > 0:
                reasons.append(f"Strike IV is {skew_discount:.2f}% below ATM IV")
            reasons.append(f"IV context is {iv_context}")
            if has_expected_move and expected_move_ratio <= 1.0:
                reasons.append("Strike is inside the 1x expected move envelope")
            if oi > STRONG_LIQUIDITY["oi"] and volume > STRONG_LIQUIDITY["volume"]:
                reasons.append("Liquidity is strong in both OI and volume")

            discounted.append({
                "symbol": None,
                "strategy": strategy_plan["strategy"],
                "strike": native_number(strike_price),
                "short_strike": native_number(strategy_plan["short_strike"]),
                "type": 'CALL' if option_type == 'ce' else 'PUT',
                "vol_mode": vol_mode,
                "iv_context": iv_context,
                "iv": native_number(current_iv),
                "iv_rank": native_number(iv_rank),
                "iv_percentile": native_number(iv_percentile),
                "hv": native_number(weighted_hv),
                "hv10": native_number((hv_metrics or {}).get("hv10")),
                "hv20": native_number((hv_metrics or {}).get("hv20")),
                "hv60": native_number((hv_metrics or {}).get("hv60")),
                "delta": native_number(delta),
                "vega": native_number(vega),
                "theta": native_number(opt.get('greeks', {}).get('theta', 0)),
                "score": native_number(score),
                # FIX 8/10: directional analytics. base_score is the pre-blend
                # composite; directional_score is the 0-100 confirmation layer.
                "base_score": native_number(base_score),
                "directional_score": native_number(directional_score),
                "entry": strategy_plan["entry"],
                "stop_loss": strategy_plan["stop_loss"],
                "target": strategy_plan["target"],
                "risk_reward": strategy_plan["risk_reward"],
                "reason": reasons,
                "mid_price": native_number(mid_price),
                "bid": native_number(bid),
                "ask": native_number(ask),
                "spread_pct": native_number(spread_pct),
                "spot": native_number(spot_price),
                "moneyness": native_number(moneyness),
                "oi": oi,
                "volume": volume,
                "expected_move": native_number(expected_move),
                "expected_move_ratio": native_number(expected_move_ratio),
                "atm_iv": native_number((atm_context or {}).get("atm_iv")),
                "atm_reference_iv": native_number(reference_iv),
                "skew_discount": native_number(skew_discount),
                "trend": trend,
                "dte": dte,
                # FIX 3/10: holiday-aware trading days to expiry (additive column;
                # the MIN_DTE gate still uses calendar days to preserve behaviour).
                "trading_dte": trading_dte,
                # FIX 10: timestamp each record so future win-rate tracking can
                # join scans to outcomes. Additive only — existing columns intact.
                "scan_timestamp": datetime.now().isoformat(timespec="seconds"),
                "component_scores": score_details["component_scores"],
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
            expiry = expiries[0]  # Nearest expiry
            logger.info("Using nearest expiry: %s", expiry)

        # Time gate: too close to expiry, premium decays faster than any cheap-IV
        # edge can pay off. Checked before the chain fetch to save an API call.
        dte = self.days_to_expiry(expiry)
        if dte < MIN_DTE_DAYS:
            logger.info(
                "Skipping %s: nearest expiry %s is %s DTE (< MIN_DTE_DAYS=%s)",
                security_name, expiry, dte, MIN_DTE_DAYS,
            )
            return []

        # FIX 3: holiday-aware trading-day distance, computed for analytics/export
        # only. The MIN_DTE_DAYS gate above intentionally still uses calendar days
        # so the universe selection behaviour is unchanged.
        trading_dte = get_actual_trading_days_to_expiry(expiry, NSE_HOLIDAYS)

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
        logger.info("Option chain strikes available: %s", len(option_chain))

        atm_context = self.extract_atm_reference_ivs(option_chain, spot_price)
        historical_ivs = self.fetch_historical_iv(security_id, security_segment)
        has_iv_history = len(historical_ivs) >= MIN_IV_SAMPLES
        iv_rank_atm = self.calculate_iv_rank(atm_context.get("atm_iv") or 0, historical_ivs) if atm_context.get("atm_iv") and has_iv_history else None
        iv_percentile_atm = self.calculate_iv_percentile(atm_context.get("atm_iv") or 0, historical_ivs) if atm_context.get("atm_iv") and has_iv_history else None
        expected_move = self.compute_expected_move(spot_price, atm_context.get("atm_iv"), dte)
        logger.info("Volatility Mode: %s", "IV_HISTORY" if has_iv_history else "SKEW")
        logger.info("IV Samples Available: %s", len(historical_ivs))
        if atm_context.get("atm_iv"):
            logger.info("ATM IV: %.2f", atm_context["atm_iv"])
        if iv_rank_atm is not None:
            logger.info("ATM IV Rank / Percentile: %.2f / %.2f", iv_rank_atm, iv_percentile_atm)
        if expected_move is not None:
            logger.info("Expected Move (%.0f DTE): %.2f points", dte, expected_move)
        
        # Calculate historical volatility if requested
        hv_metrics = {"hv10": None, "hv20": None, "hv60": None, "weighted_hv": None}
        trend_context = {"trend": "neutral"}
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

        if historical_ivs:
            logger.info("Historical IV samples: %s", len(historical_ivs))

        # FIX 9: classify futures OI once per underlying (default-off, fail-open).
        # Computed here so all strikes of this underlying share a single futures
        # fetch; returns None unless ENABLE_FUTURES_OI_CONFIRMATION is set.
        futures_classification = self._get_futures_classification(security_name)

        # Scan each strike
        all_discounted = []

        for strike_str, strike_data in option_chain.items():
            # FIX 5: tolerate malformed strike keys instead of crashing float().
            strike_price = parse_strike_key(strike_str)
            if strike_price is None:
                logger.warning("Skipping malformed strike key %r for %s", strike_str, security_name)
                continue

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
                trend_context=trend_context,                    # FIX 8: EMA structure
                futures_classification=futures_classification,  # FIX 9: OI confirmation
                trading_dte=trading_dte,                        # FIX 3/10: analytics
                has_iv_history=has_iv_history,
            )

            all_discounted.extend(discounted)
        
        # Sort by discount score
        all_discounted.sort(key=lambda x: x['score'], reverse=True)
        logger.info("Completed scan for %s with %s discounted opportunities", security_name, len(all_discounted))
        
        return all_discounted
    
    # ==================== 4. MULTI-STOCK SCANNER ====================

    def _prefetch_shared_fno_expiries(self, security_ids):
        """
        FIX 6: prime the shared NSE_FNO expiry cache with a single API call.

        The first stock primes the shared cache entry; every subsequent stock is
        then a cache HIT (see get_expiry_list logging). Failures are non-fatal —
        if the prefetch returns nothing, per-stock lookups simply happen lazily
        as before. This never increases request frequency (it just front-loads
        the one call that would have happened on the first stock anyway).
        """
        for sec_id, sec_name in security_ids.items():
            if sec_name in ('NIFTY', 'BANKNIFTY'):
                continue  # indices have their own per-id expiry calendar
            self.get_expiry_list(sec_id, "NSE_FNO")
            break  # one stock is enough to populate the shared NSE_FNO entry

    def scan_all_fno_stocks(self, security_ids=None, expiry=None, min_discount_score=MIN_SCORE):
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

        # FIX 6: warm the shared NSE F&O expiry cache with ONE call before the
        # loop. All NSE_FNO stocks resolve to a single shared cache key
        # (_expiry_cache_key), so after this prefetch every per-stock expiry
        # lookup inside scan_underlying is a cache HIT and issues no further API
        # call. Index expiries are still fetched lazily on first use (separate
        # calendars). This keeps total expiry-list calls at ~3 regardless of how
        # many stocks are scanned, without raising request frequency.
        self._prefetch_shared_fno_expiries(security_ids)

        all_opportunities = []

        for sec_id, sec_name in security_ids.items():
            try:
                # Determine segment
                if sec_name in ['NIFTY', 'BANKNIFTY']:
                    segment = "IDX_I"
                else:
                    segment = "NSE_FNO"
                
                discounted = self.scan_underlying(
                    security_id=sec_id,
                    security_segment=segment,
                    security_name=sec_name,
                    expiry=expiry,
                    use_hv=True
                )
                
                # Add stock info and filter
                for opt in discounted:
                    if opt['score'] >= min_discount_score:
                        opt['symbol'] = sec_name
                        opt['security_id'] = sec_id
                        all_opportunities.append(opt)

            except Exception:
                logger.exception("Error scanning %s", sec_name)
        
        # Convert to DataFrame
        if all_opportunities:
            df = pd.DataFrame(all_opportunities)
            df = df.sort_values('score', ascending=False)
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

            logger.info("%s - %s @ Strike %.2f", row['symbol'], row['strategy'], row['strike'])
            logger.info("%s", "-" * 50)
            logger.info("Score: %.2f/100 | Type: %s | Vol Mode: %s", row['score'], row['type'], row['vol_mode'])
            if pd.notna(row['iv_rank']) and pd.notna(row['iv_percentile']):
                logger.info("IV: %.2f%% | IV Rank: %.2f | IV Percentile: %.2f", row['iv'], row['iv_rank'], row['iv_percentile'])
            else:
                logger.info("IV: %.2f%% | IV Context: %s", row['iv'], row['iv_context'])
            logger.info("HV Benchmark: %s | Skew Discount vs ATM: %s", hv_text, skew_text)
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
        min_discount_score=55
    )
    
    # Generate report
    scanner.generate_report(all_opportunities)
    
    # Save to CSV
    if not all_opportunities.empty:
        all_opportunities.to_csv(Path("data") / "discounted_premiums.csv", index=False)
        logger.info("Results saved to data/discounted_premiums.csv")

    scanner.send_telegram_summary(all_opportunities)