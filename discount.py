import os
import logging
import math
from pathlib import Path
import sqlite3

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

# IV_HISTORY_FILE = Path("iv_history.csv")
IV_HISTORY_FILE = Path("data") / "iv_history.db"

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

class DiscountedPremiumScanner:
    """
    Scanner to identify options trading at discounted premiums
    Using Dhan API with proper authentication pattern
    """
    
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
        self.context = DhanContext(client_id, hardtoken)
        self.dhan = dhanhq(self.context)
        self.risk_free_rate = 0.065  # 6.5% - update from RBI periodically
        self.iv_history_file = IV_HISTORY_FILE
        self.store_intraday = store_intraday
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.fno_stocks = self.load_fno_stocks()
        
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
        response = self.dhan.option_chain(
            under_security_id=underlying_security_id,
            under_exchange_segment=underlying_segment,
            expiry=expiry
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
        response = self.dhan.expiry_list(
            under_security_id=underlying_security_id,
            under_exchange_segment=underlying_segment
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

    def fetch_historical_iv(self, security_id, exchange_segment, lookback_days=252):
        """
        Load persisted ATM IV history from SQLite DB (read-only).
        Writes are handled by a separate service.
        """
        if not self.iv_history_file.exists():
            return []

        try:
            with sqlite3.connect(str(self.iv_history_file), timeout=30.0) as conn:
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
                rows = cursor.fetchall()
                return [row[0] for row in reversed(rows)]
        except Exception:
            logger.exception("Failed to read IV history from DB: %s", self.iv_history_file)
            return []
    
    def fetch_historical_iv_old(self, security_id, exchange_segment, lookback_days=252):
        """
        Load persisted ATM IV history for IV Rank / IV Percentile calculations.
        
        Args:
            security_id: Security ID
            exchange_segment: Exchange segment
            lookback_days: Number of days to look back
        
        Returns:
            list: Historical ATM IV values
        """
        if not self.iv_history_file.exists():
            return []

        try:
            df = pd.read_csv(self.iv_history_file)
        except Exception:
            logger.exception("Failed to read IV history file: %s", self.iv_history_file)
            return []

        if "snapshot_time" not in df.columns:
            df["snapshot_time"] = "00:00:00"

        required_cols = {"security_id", "snapshot_date", "snapshot_time", "atm_iv"}
        if not required_cols.issubset(df.columns):
            return []

        filtered = df[df["security_id"].astype(str) == str(security_id)].copy()
        if filtered.empty:
            return []

        filtered["snapshot_date"] = pd.to_datetime(filtered["snapshot_date"], errors="coerce")
        filtered["snapshot_time"] = filtered["snapshot_time"].fillna("00:00:00").astype(str)
        filtered["atm_iv"] = pd.to_numeric(filtered["atm_iv"], errors="coerce")
        filtered = filtered.dropna(subset=["snapshot_date", "atm_iv"])
        filtered = filtered[
            (filtered["atm_iv"] >= 1.0) &
            (filtered["atm_iv"] <= 200.0)
        ]
        filtered = filtered.sort_values(["snapshot_date", "snapshot_time"])
        filtered = filtered.tail(lookback_days)
        return filtered["atm_iv"].tolist()
    
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

    def extract_atm_reference_ivs(self, option_chain, spot_price):
        if not option_chain:
            return {"atm_strike": None, "atm_call_iv": None, "atm_put_iv": None, "atm_iv": None}

        atm_strike = min(option_chain.keys(), key=lambda strike: abs(float(strike) - spot_price))
        atm_data = option_chain.get(atm_strike, {})
        atm_call_iv = (atm_data.get("ce") or {}).get("implied_volatility") or None
        atm_put_iv = (atm_data.get("pe") or {}).get("implied_volatility") or None
        valid = [value for value in [atm_call_iv, atm_put_iv] if value and value > 0]
        atm_iv = float(np.mean(valid)) if valid else None
        return {
            "atm_strike": float(atm_strike),
            "atm_call_iv": atm_call_iv,
            "atm_put_iv": atm_put_iv,
            "atm_iv": atm_iv,
        }

    def persist_iv_snapshot(self, security_id, exchange_segment, security_name, expiry, spot_price, atm_context, store_intraday=None):
        """Persist one ATM IV snapshot per day to build IV rank / percentile history."""
        return
        # atm_iv = atm_context.get("atm_iv")
        # if atm_iv is None or atm_iv <= 0 or atm_iv < 1 or atm_iv > 200:
        #     return

        # store_intraday = self.store_intraday if store_intraday is None else store_intraday
        # snapshot_dt = datetime.now()

        # snapshot = pd.DataFrame([{
        #     "snapshot_date": snapshot_dt.date().isoformat(),
        #     "snapshot_time": snapshot_dt.strftime("%H:%M:%S"),
        #     "security_id": str(security_id),
        #     "symbol": security_name,
        #     "spot_price": spot_price,
        #     "atm_strike": atm_context.get("atm_strike"),
        #     "atm_iv": atm_iv,
        #     "atm_call_iv": atm_context.get("atm_call_iv"),
        #     "atm_put_iv": atm_context.get("atm_put_iv"),
        # }])

        # if self.iv_history_file.exists():
        #     try:
        #         existing = pd.read_csv(self.iv_history_file)
        #     except Exception:
        #         logger.exception("Failed to read IV history file for update: %s", self.iv_history_file)
        #         existing = pd.DataFrame()
        #     combined = pd.concat([existing, snapshot], ignore_index=True)
        # else:
        #     combined = snapshot

        # if "snapshot_time" not in combined.columns:
        #     combined["snapshot_time"] = "00:00:00"
        # if "symbol" not in combined.columns:
        #     combined["symbol"] = security_name

        # for column in IV_HISTORY_COLUMNS:
        #     if column not in combined.columns:
        #         combined[column] = np.nan

        # combined["security_id"] = combined["security_id"].astype(str)
        # combined["snapshot_date"] = pd.to_datetime(combined["snapshot_date"], errors="coerce").dt.date.astype(str)
        # combined["snapshot_time"] = combined["snapshot_time"].fillna("00:00:00").astype(str)
        # combined["atm_iv"] = pd.to_numeric(combined["atm_iv"], errors="coerce")
        # combined["atm_call_iv"] = pd.to_numeric(combined["atm_call_iv"], errors="coerce")
        # combined["atm_put_iv"] = pd.to_numeric(combined["atm_put_iv"], errors="coerce")
        # combined["spot_price"] = pd.to_numeric(combined["spot_price"], errors="coerce")
        # combined["atm_strike"] = pd.to_numeric(combined["atm_strike"], errors="coerce")

        # combined = combined[
        #     combined["snapshot_date"].notna() &
        #     combined["security_id"].notna() &
        #     combined["atm_iv"].notna() &
        #     (combined["atm_iv"] >= 1.0) &
        #     (combined["atm_iv"] <= 200.0)
        # ]

        # dedupe_subset = ["snapshot_date", "security_id"]
        # if store_intraday:
        #     dedupe_subset = ["snapshot_date", "snapshot_time", "security_id"]

        # combined = combined[IV_HISTORY_COLUMNS]
        # combined = combined.drop_duplicates(subset=dedupe_subset, keep="last")
        # combined = combined.sort_values(["security_id", "snapshot_date", "snapshot_time"])
        # combined.to_csv(self.iv_history_file, index=False)
    
    # ==================== 3. DISCOUNTED PREMIUM DETECTION ====================

    def build_strategy_plan(self, option_type, strike_price, spot_price, mid_price, option_chain,
                            expected_move, trend, score):
        """Create tradable strategy suggestions from a shortlisted option."""
        strike_keys = sorted(float(key) for key in option_chain.keys())
        if option_type == "CALL":
            candidate_shorts = [strike for strike in strike_keys if strike > strike_price]
            short_strike = candidate_shorts[0] if candidate_shorts else None
        else:
            candidate_shorts = [strike for strike in strike_keys if strike < strike_price]
            short_strike = candidate_shorts[-1] if candidate_shorts else None

        entry = mid_price
        stop_loss = mid_price * 0.65 if mid_price else 0
        target = mid_price * 1.8 if mid_price else 0
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
                     expected_move_ratio, iv_rank=None, iv_percentile=None, vol_mode="skew"):
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
        relevance_score = clip_score(100 - (max(expected_move_ratio - 0.5, 0) / 1.0) * 100)

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
                          has_iv_history=False):
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
        
        for option_type in ['ce', 'pe']:
            if option_type not in strike_data:
                continue
            
            opt = strike_data[option_type]
            
            oi = opt.get('oi', 0)
            volume = opt.get('volume', 0)
            delta = opt.get('greeks', {}).get('delta', 0)
            vega = opt.get('greeks', {}).get('vega', 0)
            abs_delta = abs(delta)

            # Skip illiquid or extremely low-probability options
            if oi < 1000 or volume <= 0:
                continue
            if not hedging_mode and abs_delta < 0.10:
                continue

            current_iv = opt.get('implied_volatility', 0)
            if current_iv == 0:
                continue

            reference_iv = (atm_context or {}).get("atm_call_iv") if option_type == "ce" else (atm_context or {}).get("atm_put_iv")
            if not reference_iv:
                reference_iv = (atm_context or {}).get("atm_iv")
            skew_discount = ((reference_iv - current_iv) / reference_iv) * 100 if reference_iv and reference_iv > 0 else None
            iv_context = "below_atm" if reference_iv and current_iv < reference_iv else "above_atm"

            distance_from_spot = abs(strike_price - spot_price)
            expected_move_ratio = (distance_from_spot / expected_move) if expected_move and expected_move > 0 else 0
            if expected_move and expected_move_ratio > 1.5:
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
            )

            score = score_details["score"]
            if score < 55:
                continue

            bid = opt.get('top_bid_price', opt.get('last_price', 0))
            ask = opt.get('top_ask_price', opt.get('last_price', 0))
            mid_price = (bid + ask) / 2 if bid and ask else opt.get('last_price', 0)
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
            if expected_move and expected_move_ratio <= 1.0:
                reasons.append("Strike is inside the 1x expected move envelope")
            if oi > 10000 and volume > 1000:
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
                "entry": strategy_plan["entry"],
                "stop_loss": strategy_plan["stop_loss"],
                "target": strategy_plan["target"],
                "risk_reward": strategy_plan["risk_reward"],
                "reason": reasons,
                "mid_price": native_number(mid_price),
                "bid": native_number(bid),
                "ask": native_number(ask),
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
        self.persist_iv_snapshot(security_id, security_segment, security_name, expiry, spot_price, atm_context)
        dte = self.days_to_expiry(expiry)
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
        
        # Scan each strike
        all_discounted = []
        
        for strike_str, strike_data in option_chain.items():
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
            )
            
            all_discounted.extend(discounted)
        
        # Sort by discount score
        all_discounted.sort(key=lambda x: x['score'], reverse=True)
        logger.info("Completed scan for %s with %s discounted opportunities", security_name, len(all_discounted))
        
        return all_discounted
    
    # ==================== 4. MULTI-STOCK SCANNER ====================
    
    def scan_all_fno_stocks(self, security_ids=None, expiry=None, min_discount_score=55):
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
                
                # Rate limiting
                time.sleep(1)
                
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
        all_opportunities.to_csv("discounted_premiums.csv", index=False)
        logger.info("Results saved to discounted_premiums.csv")

    scanner.send_telegram_summary(all_opportunities)