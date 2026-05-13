# -*- coding: utf-8 -*-
import os
import re
import csv
import math
import time
import logging
import sqlite3
from datetime import datetime, date, timedelta, time as dt_time
from pathlib import Path

import pandas as pd
import numpy as np
import pytz
import requests

from discount import (
    DiscountedPremiumScanner,
    unwrap_dhan_payload,
    get_trading_days_to_expiry,
)
from token_manager import TokenManager
from config import Config
import iv_store
from momentum_config import (
    CAPITAL, RISK_CONFIG, REGIME, ORB, LIQUIDITY, STRIKE,
    SCRIP_MASTER_DB, IV_HISTORY_DB, TRADE_LOG_PATH, LOT_SIZE_FALLBACK,
)

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLASS 1: ScripMasterLotSizer
# ---------------------------------------------------------------------------

class ScripMasterLotSizer:
    """Lot size lookups from data/api-scrip-master.db by underlying symbol."""

    MONTH_RE = re.compile(
        r'-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\d{4}-',
        re.IGNORECASE
    )

    def __init__(self, db_path: str = SCRIP_MASTER_DB):
        self.db_path = db_path
        self._cache: dict[str, int] = {}
        self._loaded = False

    def _extract_underlying(self, trading_symbol: str):
        """Split 'BAJAJ-AUTO-May2026-8100-CE' → 'BAJAJ-AUTO'"""
        if not trading_symbol:
            return None
        parts = self.MONTH_RE.split(trading_symbol)
        return parts[0] if parts else None

    def _load_all(self) -> None:
        """Load entire lot size map into self._cache in ONE query."""
        self._loaded = True
        try:
            conn = sqlite3.connect(self.db_path)
            cur  = conn.cursor()
            cur.execute("""
                SELECT SEM_TRADING_SYMBOL,
                       CAST(SEM_LOT_UNITS AS INTEGER) as lot_size
                FROM   scrip_master
                WHERE  SEM_INSTRUMENT_NAME IN ('OPTSTK', 'OPTIDX')
                  AND  SEM_OPTION_TYPE     IN ('CE', 'PE')
                  AND  SEM_TRADING_SYMBOL  IS NOT NULL
                  AND  SEM_LOT_UNITS       IS NOT NULL
            """)
            rows = cur.fetchall()
            conn.close()

            for trading_sym, lot_size in rows:
                if not lot_size or lot_size <= 0:
                    continue
                underlying = self._extract_underlying(trading_sym)
                if not underlying:
                    continue
                if underlying not in self._cache or lot_size < self._cache[underlying]:
                    self._cache[underlying] = lot_size

            for sym, lot in LOT_SIZE_FALLBACK.items():
                if sym not in self._cache:
                    self._cache[sym] = lot

            logger.info("ScripMasterLotSizer loaded %d underlying symbols", len(self._cache))

        except Exception:
            logger.exception("ScripMasterLotSizer._load_all failed — using fallback dict only")
            self._cache.update(LOT_SIZE_FALLBACK)

    def get(self, symbol: str) -> int:
        """Return lot size for a given underlying symbol.
        Returns 1 if symbol not found (makes stock appear unaffordable — correct safe behaviour).
        """
        if not self._loaded:
            self._load_all()
        return self._cache.get(symbol, LOT_SIZE_FALLBACK.get(symbol, 1))

    def get_bulk(self, symbols: list) -> dict:
        """Return {symbol: lot_size} for all requested symbols in one call."""
        if not self._loaded:
            self._load_all()
        return {sym: self.get(sym) for sym in symbols}

    def get_option_security_id(self, underlying: str, expiry: str,
                               strike: float, option_type: str):
        """
        Look up the Dhan security_id for a specific option contract.

        Args:
            underlying:   e.g. "BAJAJ-AUTO"
            expiry:       "YYYY-MM-DD" e.g. "2026-05-27"
            strike:       e.g. 8100.0
            option_type:  "CE" or "PE"

        Returns:
            SEM_SMST_SECURITY_ID string or None if not found.
        """
        try:
            expiry_dt  = datetime.strptime(expiry, "%Y-%m-%d")
            mon_year   = expiry_dt.strftime("%b%Y")   # "May2026"
            strike_int = int(strike) if strike == int(strike) else strike
            candidates = [
                f"{underlying}-{mon_year}-{strike_int}-{option_type}",
                f"{underlying}-{mon_year}-{float(strike)}-{option_type}",
            ]
            conn = sqlite3.connect(self.db_path)
            cur  = conn.cursor()
            for pattern in candidates:
                cur.execute("""
                    SELECT SEM_SMST_SECURITY_ID FROM scrip_master
                    WHERE  SEM_TRADING_SYMBOL = ?
                      AND  SEM_OPTION_TYPE    = ?
                    LIMIT 1
                """, (pattern, option_type))
                row = cur.fetchone()
                if row:
                    conn.close()
                    return str(row[0])
            conn.close()
            return None
        except Exception:
            logger.warning("get_option_security_id failed for %s %s %s %s",
                           underlying, expiry, strike, option_type)
            return None


# ---------------------------------------------------------------------------
# CLASS 2: MomentumRiskManager
# ---------------------------------------------------------------------------

class MomentumRiskManager:

    def __init__(self, capital: float = CAPITAL):
        self.capital        = capital
        self.daily_pnl      = 0.0
        self.trades_today   = 0
        self.open_positions: list = []

    def max_risk(self) -> float:
        """Maximum INR risk per trade."""
        return self.capital * RISK_CONFIG["max_risk_pct"]

    def can_trade(self) -> tuple:
        """Returns (True, 'OK') or (False, reason_string)."""
        daily_loss_limit = -(self.capital * RISK_CONFIG["daily_loss_limit_pct"])
        if self.daily_pnl <= daily_loss_limit:
            return False, f"daily_loss_limit_hit(pnl={self.daily_pnl:.0f})"
        if self.trades_today >= RISK_CONFIG["max_trades_per_day"]:
            return False, f"max_trades_reached({self.trades_today})"
        if len(self.open_positions) >= RISK_CONFIG["max_open_positions"]:
            return False, f"max_positions_open({len(self.open_positions)})"
        return True, "OK"

    def calculate_lots(self, atm_premium: float, lot_size: int) -> int:
        """How many lots can we buy within max_risk()?"""
        if atm_premium <= 0 or lot_size <= 0:
            return 0
        risk_per_lot = atm_premium * RISK_CONFIG["sl_pct"] * lot_size
        return max(0, int(self.max_risk() / risk_per_lot))

    def is_affordable(self, atm_premium: float, lot_size: int) -> bool:
        return self.calculate_lots(atm_premium, lot_size) >= 1

    def sl_price(self, premium: float) -> float:
        """SL = entry × (1 - sl_pct). Rounded to 1 decimal."""
        return round(premium * (1 - RISK_CONFIG["sl_pct"]), 1)

    def targets(self, premium: float) -> dict:
        """T1 and T2 price levels."""
        return {
            "t1": round(premium * RISK_CONFIG["target1_mult"], 1),
            "t2": round(premium * RISK_CONFIG["target2_mult"], 1),
        }

    def record_trade(self, pnl: float = 0.0) -> None:
        """Call when a trade signal is acted on."""
        self.daily_pnl    += pnl
        self.trades_today += 1

    def add_position(self, position: dict) -> None:
        self.open_positions.append(position)

    def remove_position(self, symbol: str) -> None:
        self.open_positions = [p for p in self.open_positions
                               if p.get("symbol") != symbol]

    def reset_daily(self) -> None:
        """Call at end of day. Resets P&L and trade count. Keeps positions list."""
        self.daily_pnl    = 0.0
        self.trades_today = 0

    def summary(self) -> dict:
        remaining = max(0.0, self.max_risk() + self.daily_pnl)
        return {
            "capital":        self.capital,
            "daily_pnl":      round(self.daily_pnl, 2),
            "daily_pnl_pct":  round(self.daily_pnl / self.capital * 100, 2),
            "trades_today":   self.trades_today,
            "open_positions": len(self.open_positions),
            "remaining_risk": round(remaining, 2),
            "daily_limit":    round(self.capital * RISK_CONFIG["daily_loss_limit_pct"], 2),
            "max_trades":     RISK_CONFIG["max_trades_per_day"],
        }


# ---------------------------------------------------------------------------
# CLASS 3: MomentumRegimeFilter
# ---------------------------------------------------------------------------

class MomentumRegimeFilter:

    def __init__(self, scanner: DiscountedPremiumScanner):
        self.scanner = scanner
        self.dhan    = scanner.dhan

    def get_daily_candles(self, security_id, exchange_segment, days=60) -> pd.DataFrame:
        """Fetch daily OHLCV for the underlying (not the option)."""
        empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        # historical_daily_data needs equity segment for stocks — NSE_FNO is option-chain only
        candle_segment = "IDX_I" if exchange_segment == "IDX_I" else "NSE_EQ"
        try:
            response = self.dhan.historical_daily_data(
                security_id      = str(security_id),
                exchange_segment = candle_segment,
                instrument_type  = "INDEX" if candle_segment == "IDX_I" else "EQUITY",
                expiry_code      = 0,
                from_date        = (date.today() - timedelta(days=days + 20)).isoformat(),
                to_date          = date.today().isoformat(),
            )
            data = response.get("data", []) if isinstance(response, dict) else []

            if isinstance(data, dict):
                data = unwrap_dhan_payload(data)
                if isinstance(data, dict):
                    for key in ("candles", "ohlc", "data"):
                        if key in data and isinstance(data[key], list):
                            data = data[key]
                            break

            if not isinstance(data, list) or not data:
                return empty

            df = pd.DataFrame(data)
            df.columns = [c.lower() for c in df.columns]

            date_col = next((c for c in df.columns if c in ("timestamp", "date", "start_time", "time")), None)
            if date_col:
                df["date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date.astype(str)
            elif "date" not in df.columns:
                return empty

            for col in ("open", "high", "low", "close", "volume"):
                if col not in df.columns:
                    df[col] = 0.0
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.dropna(subset=["close"])
            df = df[df["close"] > 0]
            df = df.sort_values("date").reset_index(drop=True)
            return df[["date", "open", "high", "low", "close", "volume"]]
        except Exception:
            logger.exception("get_daily_candles failed | security_id=%s", security_id)
            return empty

    def calculate_adx(self, df: pd.DataFrame, period: int = 14) -> float:
        """Wilder's ADX. Returns float. Returns 0.0 if df too short or on error."""
        try:
            if len(df) < period * 2:
                return 0.0

            prev_close = df["close"].shift(1)
            prev_high  = df["high"].shift(1)
            prev_low   = df["low"].shift(1)

            tr = pd.concat([
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"]  - prev_close).abs(),
            ], axis=1).max(axis=1)

            up_move   = df["high"] - prev_high
            down_move = prev_low   - df["low"]

            plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
            minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

            alpha = 1.0 / period
            s_tr  = pd.Series(tr).ewm(alpha=alpha, adjust=False).mean()
            s_pdm = pd.Series(plus_dm).ewm(alpha=alpha, adjust=False).mean()
            s_ndm = pd.Series(minus_dm).ewm(alpha=alpha, adjust=False).mean()

            plus_di  = 100 * s_pdm / s_tr.replace(0, np.nan)
            minus_di = 100 * s_ndm / s_tr.replace(0, np.nan)

            dx_denom = (plus_di + minus_di).replace(0, np.nan)
            dx  = 100 * (plus_di - minus_di).abs() / dx_denom
            adx = dx.ewm(alpha=alpha, adjust=False).mean()

            return float(adx.iloc[-1]) if not adx.empty else 0.0
        except Exception:
            return 0.0

    def detect(self, security_id, exchange_segment, symbol) -> dict:
        """Returns a regime dict. Never raises."""
        base = {
            "symbol":         symbol,
            "security_id":    security_id,
            "regime":         "ERROR",
            "strength":       "WEAK",
            "suggested_side": "NONE",
            "tradeable":      False,
            "adx":            0.0,
            "ema20":          0.0,
            "ema50":          0.0,
            "close":          0.0,
        }
        try:
            df = self.get_daily_candles(security_id, exchange_segment)
            if len(df) < 55:
                return {**base, "regime": "INSUFFICIENT_DATA"}

            ema20 = df["close"].ewm(span=REGIME["ema_fast"], adjust=False).mean()
            ema50 = df["close"].ewm(span=REGIME["ema_slow"], adjust=False).mean()
            adx   = self.calculate_adx(df)
            c     = float(df["close"].iloc[-1])
            e20   = float(ema20.iloc[-1])
            e50   = float(ema50.iloc[-1])

            if c > e20 > e50 and adx >= REGIME["adx_min"]:
                regime, side = "BULLISH", "CE"
            elif c < e20 < e50 and adx >= REGIME["adx_min"]:
                regime, side = "BEARISH", "PE"
            else:
                regime, side = "RANGE", "NONE"

            strength  = "STRONG" if adx >= REGIME["adx_strong"] else "WEAK"
            tradeable = regime in ("BULLISH", "BEARISH")

            return {
                **base,
                "regime":         regime,
                "strength":       strength,
                "suggested_side": side,
                "tradeable":      tradeable,
                "adx":            round(adx, 2),
                "ema20":          round(e20, 2),
                "ema50":          round(e50, 2),
                "close":          round(c, 2),
            }
        except Exception:
            logger.exception("detect failed | symbol=%s id=%s", symbol, security_id)
            return base


# ---------------------------------------------------------------------------
# CLASS 4: MomentumScanner
# ---------------------------------------------------------------------------

class MomentumScanner:

    def __init__(self, scanner: DiscountedPremiumScanner):
        self.scanner = scanner
        self.dhan    = scanner.dhan

    def get_intraday_candles(self, security_id, exchange_segment,
                             interval_minutes: int = 15) -> pd.DataFrame:
        """Fetch intraday candles from Dhan at the requested interval."""
        empty = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume", "vwap"])
        today = date.today().isoformat()
        try:
            response = self.dhan.intraday_minute_data(
                security_id      = str(security_id),
                exchange_segment = exchange_segment,
                instrument_type  = "INDEX" if exchange_segment == "IDX_I" else "EQUITY",
                from_date        = today,
                to_date          = today,
                interval         = interval_minutes,
            )
            if not isinstance(response, dict):
                logger.debug("intraday candles: non-dict response | sec_id=%s", security_id)
                return empty

            status = response.get("status", "")
            if status != "success":
                logger.debug("intraday candles: status=%s | sec_id=%s | remarks=%s",
                             status, security_id, str(response.get("remarks", ""))[:150])
                return empty

            data = response.get("data", {})

            # Dhan returns columnar dict: {"open":[...],"high":[...],"timestamp":[epoch_ints]}
            if isinstance(data, dict) and "open" in data and "close" in data:
                df = pd.DataFrame(data)
            elif isinstance(data, list) and data:
                df = pd.DataFrame(data)
            else:
                logger.debug("intraday candles: empty/unrecognised data | sec_id=%s", security_id)
                return empty

            df.columns = [c.lower() for c in df.columns]

            ts_col = next((c for c in df.columns
                           if c in ("timestamp", "start_time", "datetime", "time", "date")), None)
            if ts_col:
                # Dhan returns epoch integers (Unix seconds)
                parsed = pd.to_datetime(df[ts_col], unit="s", errors="coerce")
                if parsed.isna().all():
                    # Fallback: plain string datetime (forward-compatibility)
                    parsed = pd.to_datetime(df[ts_col], errors="coerce")
                    df["datetime"] = parsed
                else:
                    df["datetime"] = parsed.dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
            elif "datetime" not in df.columns:
                return empty

            df = df.dropna(subset=["datetime"])

            for col in ("open", "high", "low", "close", "volume"):
                if col not in df.columns:
                    df[col] = 0.0
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

            df = df.sort_values("datetime").reset_index(drop=True)

            tp = (df["high"] + df["low"] + df["close"]) / 3
            df["vwap"] = (tp * df["volume"]).cumsum() / df["volume"].replace(0, np.nan).cumsum()
            df["vwap"] = df["vwap"].fillna(df["close"])

            return df[["datetime", "open", "high", "low", "close", "volume", "vwap"]]
        except Exception:
            logger.exception("get_intraday_candles failed | security_id=%s", security_id)
            return empty

    def _no_signal(self, symbol: str, security_id: int, reason: str) -> dict:
        """Standard no-signal return dict."""
        return {
            "signal": "NONE", "trigger": "", "symbol": symbol,
            "security_id": security_id, "price": 0.0,
            "orb_high": 0.0, "orb_low": 0.0, "volume_ratio": 0.0,
            "vwap": 0.0, "timestamp": datetime.now(IST).isoformat(),
            "reason": reason,
        }

    def get_orb(self, df: pd.DataFrame) -> dict:
        """Extract opening range from first ORB['range_candles'] rows."""
        if len(df) < 3:
            return {"high": 0.0, "low": 0.0, "range": 0.0}
        orb_candles = df.head(ORB["range_candles"])
        high  = float(orb_candles["high"].max())
        low   = float(orb_candles["low"].min())
        return {"high": high, "low": low, "range": round(high - low, 2)}

    def check_orb_signal(self, security_id: int, exchange_segment: str,
                         symbol: str) -> dict:
        """Check for Opening Range Breakout signal. Never raises."""
        try:
            now = datetime.now(IST).time()
            if now >= dt_time(ORB["entry_cutoff_hour"], ORB["entry_cutoff_min"]):
                return self._no_signal(symbol, security_id, "past_entry_cutoff")

            df = self.get_intraday_candles(security_id, exchange_segment)
            if df is None or len(df) < 5:
                return self._no_signal(symbol, security_id, "insufficient_candles")

            orb = self.get_orb(df)
            if orb["range"] == 0.0:
                return self._no_signal(symbol, security_id, "orb_range_zero")

            last          = df.iloc[-2]
            prior_vols    = df["volume"].iloc[-7:-2]
            prev5_avg_vol = prior_vols.mean() if len(prior_vols) > 0 else 0
            vol_ratio     = float(last["volume"] / prev5_avg_vol) if prev5_avg_vol > 0 else 0.0
            volume_ok     = vol_ratio >= ORB["volume_mult"]

            if float(last["close"]) > orb["high"] and volume_ok:
                signal = "CE"
            elif float(last["close"]) < orb["low"] and volume_ok:
                signal = "PE"
            else:
                return self._no_signal(symbol, security_id,
                                       f"no_breakout(ratio={vol_ratio:.2f})")

            return {
                "signal":       signal,
                "trigger":      "ORB",
                "symbol":       symbol,
                "security_id":  security_id,
                "price":        round(float(last["close"]), 2),
                "orb_high":     round(orb["high"], 2),
                "orb_low":      round(orb["low"], 2),
                "volume_ratio": round(vol_ratio, 2),
                "vwap":         round(float(last["vwap"]), 2),
                "timestamp":    datetime.now(IST).isoformat(),
                "reason":       "orb_breakout",
            }
        except Exception:
            logger.exception("check_orb_signal failed for %s", symbol)
            return self._no_signal(symbol, security_id, "exception")

    def check_vwap_signal(self, security_id: int, exchange_segment: str,
                          symbol: str) -> dict:
        """Check for VWAP reclaim (bullish) or VWAP break (bearish). Never raises."""
        try:
            df = self.get_intraday_candles(security_id, exchange_segment)
            if df is None or len(df) < 5:
                return self._no_signal(symbol, security_id, "insufficient_candles")

            last          = df.iloc[-2]
            prev          = df.iloc[-3]
            prior_vols    = df["volume"].iloc[-7:-2]
            prev5_avg_vol = prior_vols.mean() if len(prior_vols) > 0 else 0
            vol_ratio     = float(last["volume"] / prev5_avg_vol) if prev5_avg_vol > 0 else 0.0
            volume_ok     = vol_ratio >= 1.3

            bullish = (float(prev["close"]) < float(prev["vwap"]) and
                       float(last["close"]) > float(last["vwap"]) and volume_ok)
            bearish = (float(prev["close"]) > float(prev["vwap"]) and
                       float(last["close"]) < float(last["vwap"]) and volume_ok)

            if bullish:
                signal, reason = "CE", "vwap_reclaim"
            elif bearish:
                signal, reason = "PE", "vwap_break"
            else:
                return self._no_signal(symbol, security_id,
                                       f"no_vwap_signal(ratio={vol_ratio:.2f})")

            return {
                "signal":       signal,
                "trigger":      "VWAP",
                "symbol":       symbol,
                "security_id":  security_id,
                "price":        round(float(last["close"]), 2),
                "orb_high":     0.0,
                "orb_low":      0.0,
                "volume_ratio": round(vol_ratio, 2),
                "vwap":         round(float(last["vwap"]), 2),
                "timestamp":    datetime.now(IST).isoformat(),
                "reason":       reason,
            }
        except Exception:
            logger.exception("check_vwap_signal failed for %s", symbol)
            return self._no_signal(symbol, security_id, "exception")


# ---------------------------------------------------------------------------
# CLASS 5: AffordabilityFilter
# ---------------------------------------------------------------------------

class AffordabilityFilter:

    def __init__(self, lot_sizer: ScripMasterLotSizer,
                 risk_manager: MomentumRiskManager):
        self.lot_sizer    = lot_sizer
        self.risk_manager = risk_manager

    def estimate_atm_premiums_bulk(self, fno_stocks: dict) -> dict:
        """Read latest IV snapshots from iv_store. Returns {security_id(int): estimated_premium}."""
        DTE = 7
        snapshots = iv_store.get_bulk_latest_snapshots(list(fno_stocks.keys()))
        result = {}
        for sec_id in fno_stocks:
            snap = snapshots.get(sec_id, {})
            iv   = snap.get("atm_iv")   or 30.0
            spot = snap.get("spot_price") or 0.0
            if spot <= 0:
                result[sec_id] = 100.0
                continue
            premium = spot * (iv / 100) * math.sqrt(DTE / 365) * 0.4
            result[sec_id] = max(1.0, round(premium, 2))
        return result

    def get_affordable_universe(self, fno_stocks: dict) -> dict:
        """Filter fno_stocks to only those where calculate_lots() >= 1."""
        symbols   = list(fno_stocks.values())
        lot_sizes = self.lot_sizer.get_bulk(symbols)
        premiums  = self.estimate_atm_premiums_bulk(fno_stocks)

        affordable = {}
        rejected   = []

        for sec_id, symbol in fno_stocks.items():
            lot_size = lot_sizes.get(symbol, 1)
            premium  = premiums.get(sec_id, 100.0)
            if self.risk_manager.is_affordable(premium, lot_size):
                affordable[sec_id] = symbol
            else:
                rejected.append(
                    f"{symbol}(lot={lot_size},prem≈{premium:.0f},"
                    f"risk≈{premium*RISK_CONFIG['sl_pct']*lot_size:.0f})"
                )

        logger.info(
            "Affordability filter: %d/%d affordable | rejected sample: %s",
            len(affordable), len(fno_stocks),
            str(rejected[:5]) if rejected else "none",
        )
        if rejected:
            logger.debug("All rejected: %s", rejected)

        return affordable


# ---------------------------------------------------------------------------
# CLASS 6: MomentumSignalRanker
# ---------------------------------------------------------------------------

class MomentumSignalRanker:

    def rank(self, signals: list, regime_map: dict) -> list:
        """Score, filter misaligned signals, return top N sorted by composite_score."""
        results = []
        for signal in signals:
            side   = signal.get("signal")
            sec_id = signal.get("security_id")
            regime = regime_map.get(sec_id, {})

            if side == "CE" and regime.get("suggested_side") != "CE":
                continue
            if side == "PE" and regime.get("suggested_side") != "PE":
                continue

            score = 0
            score += 40 if regime.get("strength") == "STRONG" else 20
            score += 30  # direction aligned (already confirmed above)
            score += 10 if signal.get("trigger") == "ORB" else 0
            score += 5  if signal.get("volume_ratio", 0) >= 2.0 else 0

            signal["composite_score"] = score
            results.append(signal)

        results.sort(key=lambda s: s["composite_score"], reverse=True)
        return results[:RISK_CONFIG["max_trades_per_day"]]


# ---------------------------------------------------------------------------
# CLASS 7: MomentumTradeJournal
# ---------------------------------------------------------------------------

class MomentumTradeJournal:

    HEADERS = [
        "date", "time", "symbol", "security_id", "option_type", "strike",
        "expiry", "lots", "qty", "entry_premium", "sl_price", "t1", "t2",
        "exit_price", "exit_reason", "pnl", "pnl_pct", "holding_minutes",
        "regime", "strength", "adx", "signal_type", "trigger",
        "volume_ratio", "composite_score", "notes",
    ]

    def __init__(self, filepath: str = TRADE_LOG_PATH):
        """Create parent dirs and CSV with headers if not exists."""
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        if not self.filepath.exists():
            with open(self.filepath, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=self.HEADERS).writeheader()

    def log_entry(self, trade: dict) -> None:
        """Append one row to CSV. Fill missing keys with empty string."""
        try:
            row = {h: trade.get(h, "") for h in self.HEADERS}
            with open(self.filepath, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=self.HEADERS).writerow(row)
        except Exception:
            logger.warning("TradeJournal.log_entry failed: %s", trade.get("symbol"))

    def get_today_stats(self) -> dict:
        """Read CSV, filter today's date, compute stats."""
        zero = {"trades": 0, "wins": 0, "losses": 0,
                "total_pnl": 0.0, "win_rate": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0}
        try:
            if not self.filepath.exists():
                return zero
            df = pd.read_csv(self.filepath)
            if df.empty:
                return zero
            today = date.today().isoformat()
            df    = df[df["date"] == today].copy()
            if df.empty:
                return zero

            df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce").fillna(0)
            closed    = df[df["exit_price"].astype(str).str.strip() != ""]
            wins      = closed[closed["pnl"] > 0]
            losses    = closed[closed["pnl"] < 0]

            return {
                "trades":    len(df),
                "wins":      len(wins),
                "losses":    len(losses),
                "total_pnl": round(float(closed["pnl"].sum()), 2),
                "win_rate":  round(len(wins) / len(closed) * 100, 1) if len(closed) > 0 else 0.0,
                "avg_win":   round(float(wins["pnl"].mean()), 2) if len(wins) > 0 else 0.0,
                "avg_loss":  round(float(losses["pnl"].mean()), 2) if len(losses) > 0 else 0.0,
            }
        except Exception:
            logger.warning("get_today_stats failed")
            return zero


# ---------------------------------------------------------------------------
# CLASS 8: MomentumTelegramNotifier
# ---------------------------------------------------------------------------

class MomentumTelegramNotifier:

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token or ""
        self.chat_id   = chat_id or ""
        self._url      = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def send(self, text: str) -> bool:
        """POST to Telegram. Returns True on success. Never raises."""
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram not configured — skipping send")
            return False
        try:
            resp = requests.post(
                self._url,
                json={"chat_id": self.chat_id, "text": text,
                      "parse_mode": "HTML"},
                timeout=10,
            )
            if not resp.ok:
                logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception:
            logger.warning("Telegram send exception")
            return False

    def send_premarket_report(self, regime_results: list, vix: float,
                              affordable_count: int, risk_summary: dict) -> None:
        """Format and send premarket summary."""
        if vix < 16:
            vix_emoji, vix_status = "🟢", "NORMAL"
        elif vix <= 22:
            vix_emoji, vix_status = "🟡", "ELEVATED"
        else:
            vix_emoji, vix_status = "🔴", "HIGH"

        bullish = sorted(
            [r for r in regime_results if r.get("regime") == "BULLISH"],
            key=lambda r: r.get("adx", 0), reverse=True
        )[:6]
        bearish = sorted(
            [r for r in regime_results if r.get("regime") == "BEARISH"],
            key=lambda r: r.get("adx", 0), reverse=True
        )[:6]
        tradeable_count = sum(1 for r in regime_results if r.get("tradeable"))
        range_count     = sum(1 for r in regime_results
                              if r.get("regime") not in ("BULLISH", "BEARISH"))

        capital    = risk_summary.get("capital", CAPITAL)
        daily_lim  = risk_summary.get("daily_limit", capital * RISK_CONFIG["daily_loss_limit_pct"])
        max_trades = risk_summary.get("max_trades", RISK_CONFIG["max_trades_per_day"])

        bull_syms = ", ".join(r["symbol"] for r in bullish) or "—"
        bear_syms = ", ".join(r["symbol"] for r in bearish) or "—"
        day_str   = datetime.now(IST).strftime("%d %b %Y")

        msg = (
            f"🌅 <b>MOMENTUM SCANNER</b> | {day_str}\n"
            f"India VIX: {vix:.1f} | {vix_emoji} {vix_status}\n"
            "──────────────────────────────\n"
            f"Affordable stocks: {affordable_count}/213\n"
            f"Tradeable (regime OK): {tradeable_count}\n"
            "──────────────────────────────\n"
            f"Capital: ₹{capital:,.0f} | Daily limit: ₹{daily_lim:,.0f}\n"
            f"Max trades today: {max_trades}\n"
            "──────────────────────────────\n"
            f"📈 BULLISH ({len(bullish)}): {bull_syms}\n"
            f"📉 BEARISH ({len(bearish)}): {bear_syms}\n"
            f"⏸ RANGE/OTHER: {range_count}"
        )
        self.send(msg)

    def send_signal_alert(self, signal: dict, regime: dict,
                          strike_data: dict, lots: int,
                          risk_data: dict) -> None:
        """Format and send trade signal alert."""
        symbol      = signal.get("symbol", "")
        option_type = signal.get("signal", "")
        strike      = strike_data.get("strike", "")
        trigger     = signal.get("trigger", "")
        regime_name = regime.get("regime", "")
        adx         = regime.get("adx", 0.0)
        strength    = regime.get("strength", "")

        ltp      = strike_data.get("ltp", 0.0)
        qty      = risk_data.get("qty", 0)
        sl       = risk_data.get("sl", 0.0)
        t1       = risk_data.get("t1", 0.0)
        t2       = risk_data.get("t2", 0.0)
        max_risk = risk_data.get("max_risk", 0.0)

        oi         = strike_data.get("oi", 0)
        vol        = strike_data.get("volume", 0)
        spread_pct = strike_data.get("spread_pct", 0.0) * 100
        vol_ratio  = signal.get("volume_ratio", 0.0)
        score      = signal.get("composite_score", 0)

        auto_exec = os.getenv("AUTO_EXECUTE", "false").strip().lower() == "true"
        footer = "" if auto_exec else "\n⚠️ <i>Alert only — place order manually on Dhan app</i>"

        msg = (
            f"🚨 <b>SIGNAL: {symbol} {option_type} {strike}</b>\n"
            f"Trigger: {trigger} | {regime_name} | ADX: {adx:.1f} ({strength})\n"
            "──────────────────────────────\n"
            f"Entry: ₹{ltp:.1f} | Lots: {lots} | Qty: {qty:,}\n"
            f"SL: ₹{sl:.1f} (-30%) | T1: ₹{t1:.1f} | T2: ₹{t2:.1f}\n"
            f"Max risk: ₹{max_risk:,.0f}\n"
            "──────────────────────────────\n"
            f"OI: {oi:,} | Vol: {vol:,} | Spread: {spread_pct:.1f}%\n"
            f"Vol ratio: {vol_ratio:.1f}x | Score: {score:.0f}/100"
            f"{footer}"
        )
        self.send(msg)

    def send_daily_summary(self, stats: dict, risk_summary: dict) -> None:
        """EOD summary message."""
        auto_exec   = os.getenv("AUTO_EXECUTE", "false").strip().lower() == "true"
        manual_note = "" if auto_exec else "\nℹ️ Manual execution mode (AUTO_EXECUTE=false)"

        trades     = stats.get("trades", 0)
        wins       = stats.get("wins", 0)
        losses     = stats.get("losses", 0)
        win_rate   = stats.get("win_rate", 0.0)
        total_pnl  = stats.get("total_pnl", 0.0)
        capital    = risk_summary.get("capital", CAPITAL)
        pnl_pct    = risk_summary.get("daily_pnl_pct", 0.0)
        closed     = wins + losses

        msg = (
            "📊 <b>MOMENTUM DAILY SUMMARY</b>\n"
            "──────────────────────────────\n"
            f"Signals sent: {trades} | Closed: {closed}\n"
            f"Wins: {wins} | Losses: {losses} | Win rate: {win_rate}%\n"
            f"Total P&amp;L: ₹{total_pnl:+,.0f}\n"
            "──────────────────────────────\n"
            f"Capital: ₹{capital:,} | Day P&amp;L: {pnl_pct:+.2f}%"
            f"{manual_note}"
        )
        self.send(msg)

    def send_no_trade_alert(self, reason: str) -> None:
        self.send(f"⛔ <b>Momentum: No trades today</b>\nReason: {reason}")


# ---------------------------------------------------------------------------
# CLASS 9: MomentumStrategyRunner
# ---------------------------------------------------------------------------

class MomentumStrategyRunner:
    """Top-level orchestrator. All public run_* methods are called by main.py."""

    def __init__(self, capital: float = CAPITAL):
        self.risk_manager  = MomentumRiskManager(capital)
        self.token_manager = TokenManager()
        self.lot_sizer     = ScripMasterLotSizer()
        self._scanner      = None
        self._regime_filter   = None
        self._mom_scanner     = None
        self._affordability   = None
        self._notifier        = None
        self._journal         = MomentumTradeJournal()
        self._ranker          = MomentumSignalRanker()
        self._affordable_universe: dict = {}
        self._regime_cache: dict        = {}

    def _build_scanner(self) -> DiscountedPremiumScanner:
        token = self.token_manager.refresh_if_needed()
        if not token:
            raise RuntimeError("Failed to get valid Dhan token")
        return DiscountedPremiumScanner(
            hardtoken=token, client_id=Config.DHAN_CLIENT_ID)

    def _ensure_components(self) -> None:
        """Lazy initialisation. Safe to call multiple times."""
        if self._scanner is not None:
            return
        self._scanner       = self._build_scanner()
        self._regime_filter = MomentumRegimeFilter(self._scanner)
        self._mom_scanner   = MomentumScanner(self._scanner)
        self._affordability = AffordabilityFilter(self.lot_sizer, self.risk_manager)
        self._notifier      = MomentumTelegramNotifier(
            self._scanner.telegram_bot_token,
            self._scanner.telegram_chat_id,
        )
        logger.info("MomentumStrategyRunner components initialised")

    def _get_exchange_segment(self, symbol: str) -> str:
        INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}
        return "IDX_I" if symbol in INDEX_SYMBOLS else "NSE_FNO"

    def _get_india_vix(self) -> float:
        """Fetch India VIX daily data. Returns last close. Returns -1.0 on failure."""
        try:
            df = self._regime_filter.get_daily_candles("20", "IDX_I", days=5)
            if df.empty:
                logger.warning("VIX fetch returned empty — security_id 20 may be wrong for Dhan. Proceeding without VIX gate.")
                return -1.0
            vix = float(df["close"].iloc[-1])
            logger.info("India VIX: %.2f", vix)
            return vix
        except Exception:
            logger.warning("_get_india_vix failed — proceeding without VIX gate")
            return -1.0

    def _select_strike(self, chain: dict, spot: float, side: str,
                       offset: int, symbol: str, expiry: str) -> dict:
        """Select the best option strike from the Dhan option chain dict."""
        try:
            if not chain or spot <= 0:
                return {}

            strikes = sorted([float(k) for k in chain.keys()])
            if not strikes:
                return {}

            if len(strikes) >= 2:
                gaps = [strikes[i + 1] - strikes[i]
                        for i in range(min(5, len(strikes) - 1))]
                strike_gap = max(set(gaps), key=gaps.count)
            else:
                strike_gap = 50

            atm = round(spot / strike_gap) * strike_gap

            if side == "CE":
                target = atm + offset * strike_gap
            else:
                target = atm - offset * strike_gap

            closest = min(strikes, key=lambda s: abs(s - target))

            # Find actual dict key matching closest
            actual_key = None
            for k in chain.keys():
                if float(k) == closest:
                    actual_key = k
                    break
            if actual_key is None:
                return {}

            entry = chain[actual_key]
            sub   = entry.get("call" if side == "CE" else "put", {})

            ltp    = float(sub.get("ltp", 0))
            bid    = float(sub.get("bid", 0))
            ask    = float(sub.get("ask", 0))
            oi     = int(sub.get("oi", 0))
            volume = int(sub.get("volume", 0))
            iv     = float(sub.get("implied_volatility", sub.get("iv", 0)))
            delta  = float(sub.get("delta", 0))

            mid        = (bid + ask) / 2 if (bid + ask) > 0 else ltp
            spread_pct = (ask - bid) / mid if mid > 0 else 1.0

            option_sec_id = self.lot_sizer.get_option_security_id(
                underlying  = symbol,
                expiry      = expiry,
                strike      = closest,
                option_type = side,
            )
            if option_sec_id is None:
                option_sec_id = ""

            return {
                "strike":             closest,
                "ltp":                round(ltp, 2),
                "bid":                round(bid, 2),
                "ask":                round(ask, 2),
                "oi":                 oi,
                "volume":             volume,
                "iv":                 round(iv, 2),
                "delta":              round(delta, 3),
                "spread_pct":         round(spread_pct, 4),
                "side":               side,
                "option_security_id": option_sec_id,
                "atm":                atm,
                "strike_gap":         strike_gap,
            }
        except Exception:
            logger.exception("_select_strike failed | symbol=%s spot=%.1f side=%s", symbol, spot, side)
            return {}

    def _check_liquidity(self, strike_data: dict) -> tuple:
        """Returns (True, 'OK') or (False, reason)."""
        if strike_data.get("oi", 0) < LIQUIDITY["min_oi"]:
            return False, f"low_oi({strike_data.get('oi', 0)})"
        if strike_data.get("volume", 0) < LIQUIDITY["min_volume"]:
            return False, f"low_volume({strike_data.get('volume', 0)})"
        if strike_data.get("spread_pct", 1.0) > LIQUIDITY["max_spread_pct"]:
            return False, f"wide_spread({strike_data.get('spread_pct', 1.0):.2%})"
        return True, "OK"

    def _place_order(self, strike_data: dict, lots: int,
                     lot_size: int, sl_price: float) -> dict:
        """Place buy order + immediate SL order via Dhan API."""
        try:
            option_sec_id = strike_data.get("option_security_id", "")
            if not option_sec_id:
                logger.error("_place_order: no option_security_id in strike_data")
                return {"status": "no_option_security_id"}

            qty = lots * lot_size

            response = self._scanner.dhan.place_order(
                security_id      = option_sec_id,
                exchange_segment  = self._scanner.dhan.NSE_FNO,
                transaction_type  = self._scanner.dhan.BUY,
                quantity          = qty,
                order_type        = self._scanner.dhan.MARKET,
                product_type      = self._scanner.dhan.INTRA,
                price             = 0,
            )
            logger.info("Buy order response: %s", response)

            if response.get("status") != "success":
                return {"status": "buy_failed", "response": response}

            sl_response = self._scanner.dhan.place_order(
                security_id      = option_sec_id,
                exchange_segment  = self._scanner.dhan.NSE_FNO,
                transaction_type  = self._scanner.dhan.SELL,
                quantity          = qty,
                order_type        = self._scanner.dhan.SL_M,
                product_type      = self._scanner.dhan.INTRA,
                price             = 0,
                trigger_price     = sl_price,
            )

            if sl_response.get("status") != "success":
                logger.error("SL order failed — placing emergency market sell: %s", sl_response)
                self._scanner.dhan.place_order(
                    security_id      = option_sec_id,
                    exchange_segment  = self._scanner.dhan.NSE_FNO,
                    transaction_type  = self._scanner.dhan.SELL,
                    quantity          = qty,
                    order_type        = self._scanner.dhan.MARKET,
                    product_type      = self._scanner.dhan.INTRA,
                    price             = 0,
                )
                symbol = strike_data.get("side", "")
                self._notifier.send(
                    f"⚠️ SL order failed for {symbol} {strike_data.get('strike')} "
                    "— emergency exit placed"
                )
                return {"status": "sl_failed_emergency_exit"}

            return {
                "status":       "ok",
                "buy_order_id": response.get("orderId", ""),
                "sl_order_id":  sl_response.get("orderId", ""),
            }

        except Exception:
            logger.exception("_place_order exception")
            return {"status": "exception"}

    def run_premarket(self) -> dict:
        """Called at 9:00 AM."""
        try:
            self._ensure_components()

            vix = self._get_india_vix()
            if vix > REGIME["vix_max"]:
                self._notifier.send_no_trade_alert(
                    f"VIX={vix:.1f} above limit {REGIME['vix_max']}")
                self._affordable_universe = {}
                return {"vix": vix, "tradeable": False, "reason": "high_vix"}

            affordable = self._affordability.get_affordable_universe(
                self._scanner.fno_stocks)
            self._affordable_universe = affordable

            candidates = dict(list(affordable.items())[:60])
            for sec_id, symbol in candidates.items():
                segment = self._get_exchange_segment(symbol)
                result  = self._regime_filter.detect(sec_id, segment, symbol)
                self._regime_cache[sec_id] = result
                time.sleep(0.3)

            regime_results   = list(self._regime_cache.values())
            tradeable_count  = sum(1 for r in regime_results if r.get("tradeable"))

            self._notifier.send_premarket_report(
                regime_results, vix, len(affordable),
                self.risk_manager.summary())

            logger.info("Premarket | affordable=%d | tradeable=%d | vix=%.1f",
                        len(affordable), tradeable_count, vix)
            return {
                "vix":        vix,
                "affordable": len(affordable),
                "tradeable":  tradeable_count,
            }

        except Exception as e:
            logger.exception("run_premarket failed")
            return {"error": str(e)}

    def run_intraday_scan(self) -> list:
        """Called every 5 minutes between 9:30–11:30 AM."""
        try:
            self._ensure_components()

            can, reason = self.risk_manager.can_trade()
            if not can:
                logger.info("run_intraday_scan skipped: %s", reason)
                return []

            now = datetime.now(IST).time()
            if now >= dt_time(ORB["entry_cutoff_hour"], ORB["entry_cutoff_min"]):
                return []

            if not self._affordable_universe:
                logger.warning("run_premarket() has not run yet")
                return []

            # Sort: STRONG tradeable first, then WEAK tradeable
            def sort_key(item):
                r = self._regime_cache.get(item[0], {})
                if r.get("tradeable") and r.get("strength") == "STRONG":
                    return 0
                if r.get("tradeable") and r.get("strength") == "WEAK":
                    return 1
                return 99

            candidates = sorted(self._affordable_universe.items(), key=sort_key)[:30]

            raw_signals = []
            for sec_id, symbol in candidates:
                regime = self._regime_cache.get(sec_id, {})
                if not regime.get("tradeable"):
                    continue
                segment  = self._get_exchange_segment(symbol)
                orb_sig  = self._mom_scanner.check_orb_signal(sec_id, segment, symbol)
                vwap_sig = self._mom_scanner.check_vwap_signal(sec_id, segment, symbol)
                for sig in [orb_sig, vwap_sig]:
                    if sig["signal"] != "NONE":
                        raw_signals.append(sig)
                time.sleep(0.3)

            if not raw_signals:
                logger.info("run_intraday_scan: no signals this cycle")
                return []

            ranked = self._ranker.rank(raw_signals, self._regime_cache)

            sent_signals = []
            for sig in ranked:
                can, reason = self.risk_manager.can_trade()
                if not can:
                    logger.info("Stopping signal processing: %s", reason)
                    break

                sec_id  = sig["security_id"]
                symbol  = sig["symbol"]
                side    = sig["signal"]
                regime  = self._regime_cache.get(sec_id, {})
                segment = self._get_exchange_segment(symbol)

                try:
                    expiries = self._scanner.get_expiry_list(sec_id, segment)
                    expiries = [e for e in expiries
                                if get_trading_days_to_expiry(e) >= 4]
                    if not expiries:
                        continue
                    expiry     = expiries[0]
                    chain_resp = self._scanner.get_option_chain(sec_id, segment, expiry)
                    if not (isinstance(chain_resp, dict) and
                            chain_resp.get("status") == "success"):
                        continue
                    chain_data = unwrap_dhan_payload(chain_resp.get("data") or {})
                    spot  = chain_data.get("last_price", 0)
                    chain = chain_data.get("oc", {})
                    if not chain or not spot:
                        continue
                except Exception:
                    logger.exception("Chain fetch failed for %s", symbol)
                    continue

                strike_data = self._select_strike(
                    chain, spot, side,
                    STRIKE["intraday_otm_offset"], symbol, expiry)
                if not strike_data:
                    continue

                liq_ok, liq_reason = self._check_liquidity(strike_data)
                if not liq_ok:
                    logger.info("Liquidity fail %s: %s", symbol, liq_reason)
                    continue

                lot_size = self.lot_sizer.get(symbol)
                premium  = strike_data.get("ltp", 0)
                if premium <= 0:
                    continue
                lots = self.risk_manager.calculate_lots(premium, lot_size)
                if lots < 1:
                    logger.info("Unaffordable post-chain %s prem=%.1f lot=%d",
                                symbol, premium, lot_size)
                    continue

                sl   = self.risk_manager.sl_price(premium)
                tgts = self.risk_manager.targets(premium)
                sig["composite_score"] = sig.get("composite_score", 0)

                now_ist = datetime.now(IST)
                trade = {
                    "date":            now_ist.strftime("%Y-%m-%d"),
                    "time":            now_ist.strftime("%H:%M:%S"),
                    "symbol":          symbol,
                    "security_id":     sec_id,
                    "option_type":     side,
                    "strike":          strike_data.get("strike"),
                    "expiry":          expiry,
                    "lots":            lots,
                    "qty":             lots * lot_size,
                    "entry_premium":   premium,
                    "sl_price":        sl,
                    "t1":              tgts["t1"],
                    "t2":              tgts["t2"],
                    "exit_price":      "",
                    "exit_reason":     "",
                    "pnl":             "",
                    "pnl_pct":         "",
                    "holding_minutes": "",
                    "regime":          regime.get("regime", ""),
                    "strength":        regime.get("strength", ""),
                    "adx":             round(float(regime.get("adx", 0.0)), 1),
                    "signal_type":     sig.get("trigger", ""),
                    "trigger":         sig.get("trigger", ""),
                    "volume_ratio":    sig.get("volume_ratio", 0.0),
                    "composite_score": sig.get("composite_score", 0),
                    "notes":           "",
                }

                self._journal.log_entry(trade)

                risk_data = {
                    "qty":      lots * lot_size,
                    "sl":       sl,
                    "t1":       tgts["t1"],
                    "t2":       tgts["t2"],
                    "max_risk": round(premium * RISK_CONFIG["sl_pct"] * lots * lot_size, 2),
                }
                self._notifier.send_signal_alert(sig, regime, strike_data, lots, risk_data)

                if os.getenv("AUTO_EXECUTE", "false").strip().lower() == "true":
                    order = self._place_order(strike_data, lots, lot_size, sl)
                    trade["order"] = order

                self.risk_manager.record_trade()
                self.risk_manager.add_position({
                    "symbol":   symbol,
                    "side":     side,
                    "strike":   strike_data.get("strike"),
                    "expiry":   expiry,
                    "entry":    premium,
                    "lots":     lots,
                    "lot_size": lot_size,
                    "sl":       sl,
                    "t1":       tgts["t1"],
                    "t2":       tgts["t2"],
                })
                time.sleep(0.3)
                sent_signals.append(trade)

            return sent_signals

        except Exception as e:
            logger.exception("run_intraday_scan failed")
            return []

    def run_eod(self) -> None:
        """Called at end of day (≥ 15:15). Sends daily summary and resets counters."""
        try:
            self._ensure_components()
            stats = self._journal.get_today_stats()
            self._notifier.send_daily_summary(stats, self.risk_manager.summary())
            self.risk_manager.reset_daily()
            logger.info("EOD summary sent | stats=%s", stats)
        except Exception:
            logger.exception("run_eod failed")
