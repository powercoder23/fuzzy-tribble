# -*- coding: utf-8 -*-
"""
Sonar-Laplace Scanner (service: sonar)

Applies a signal-processing low-pass filter (Ehlers SuperSmoother — a 2-pole
Butterworth / Laplace-family filter) to each F&O name's intraday price series to
derive a smoothed midline + adaptive bands. From those it reads:

    trend      : slope of the smoothed midline (UP / DOWN / FLAT)
    dyn S/R    : lower band = dynamic support, upper band = dynamic resistance
    breakout   : last price closes beyond a band  -> CE (up) / PE (down)
    reversal   : price was beyond a band and crossed back toward the mean

Design rules (same isolation philosophy as the other screeners)
* Reads 5-min CLOSE candles from the shared DataProvider (cache-first, falling
  back to a direct fetch on a miss) instead of the iv_history spot snapshots.
* Pure, side-effect-free math (super_smoother / bands / classify) — unit-testable.
* Fail-open: a name with too few points is skipped, never crashes the scan.

Public surface
    super_smoother(series, period)            -> list           (pure)
    dynamic_bands(series, smoothed, mult)      -> (upper, lower) (pure)
    classify(prev, last, upper, lower, slope_pct) -> dict        (pure)
    SonarScanner().scan() / .persist() / .send_telegram()
    get_latest_sonar(security_id)             -> dict (for composite / strategies)
"""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime

import pandas as pd

from collectors import iv_store
import notifications
import sonar_laplace_config as cfg
from data_provider import DataProvider

logger = logging.getLogger(__name__)
CE, PE = "CE", "PE"


# --------------------------------------------------------------------------- #
# Pure DSP / math (no I/O — unit-testable)
# --------------------------------------------------------------------------- #
def super_smoother(series: list, period: int) -> list:
    """Ehlers 2-pole SuperSmoother low-pass filter (Butterworth/Laplace family).

    Removes high-frequency noise far better than an EMA for the same lag.
    Returns a list the same length as `series`.
    """
    n = len(series)
    if n == 0:
        return []
    if n < 3 or period < 2:
        return list(series)

    arg = 1.414 * math.pi / period
    a1 = math.exp(-arg)
    b1 = 2 * a1 * math.cos(arg)
    c2 = b1
    c3 = -a1 * a1
    c1 = 1 - c2 - c3

    ss = list(series)  # seed first two with raw prices
    for i in range(2, n):
        ss[i] = c1 * (series[i] + series[i - 1]) / 2.0 + c2 * ss[i - 1] + c3 * ss[i - 2]
    return ss


def dynamic_bands(series: list, smoothed: list, mult: float):
    """Adaptive bands = smoothed last ± mult × std(residuals). Returns (upper, lower, mid)."""
    if not smoothed:
        return None, None, None
    residuals = [series[i] - smoothed[i] for i in range(len(smoothed))]
    if len(residuals) < 2:
        return smoothed[-1], smoothed[-1], smoothed[-1]
    mean = sum(residuals) / len(residuals)
    var = sum((r - mean) ** 2 for r in residuals) / (len(residuals) - 1)
    std = math.sqrt(var)
    mid = smoothed[-1]
    return mid + mult * std, mid - mult * std, mid


def slope_pct(smoothed: list, lookback: int, ref_price: float) -> float:
    """Smoothed-line slope over `lookback`, as a % of price (signed)."""
    if len(smoothed) <= lookback or not ref_price:
        return 0.0
    return (smoothed[-1] - smoothed[-1 - lookback]) / ref_price * 100.0


def classify(prev_price, last_price, upper, lower, slope_p, min_slope) -> dict:
    """Map the smoothed state to trend + signal + bias + dyn S/R."""
    if slope_p >= min_slope:
        trend = "UP"
    elif slope_p <= -min_slope:
        trend = "DOWN"
    else:
        trend = "FLAT"

    signal, bias = "NONE", None
    if upper is not None and last_price > upper:
        signal, bias = "BREAKOUT_UP", CE
    elif lower is not None and last_price < lower:
        signal, bias = "BREAKDOWN", PE
    elif lower is not None and prev_price is not None and prev_price < lower <= last_price:
        signal, bias = "REVERSAL_UP", CE
    elif upper is not None and prev_price is not None and prev_price > upper >= last_price:
        signal, bias = "REVERSAL_DOWN", PE
    else:
        # In-band: lean on trend for a soft bias.
        bias = CE if trend == "UP" else PE if trend == "DOWN" else None

    return {"trend": trend, "signal": signal, "bias": bias,
            "support": round(lower, 2) if lower is not None else None,
            "resistance": round(upper, 2) if upper is not None else None}


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #
class SonarScanner:
    def __init__(self, db_path: str | None = None, data_provider=None):
        self.db_path = db_path or iv_store.DB_PATH
        self._provider = data_provider  # injected from runner

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _series_map(self) -> dict:
        """{security_id: (symbol, [close,...])} from the DataProvider 5-min cache."""
        if self._provider is None:
            return {}
        out = {}
        from f_o_stocks_list import get_stock_futures
        from load_scrip_master_sqlite import get_security_id_symbol_map
        # {sec_id: symbol} for the current F&O futures universe.
        symbol_map = get_security_id_symbol_map(get_stock_futures())
        for sec_id, symbol in symbol_map.items():
            df = self._provider.intraday_candles(
                str(sec_id), "NSE_FO", interval=5
            )
            if df is None or df.empty or "close" not in df.columns:
                continue
            # V2 (P2): persist the 5-min candles we already fetched so the
            # Convex engine's triggers (ORB/VWAP/break-retest) run zero-API.
            # Fail-open — candle persistence must never break the sonar scan.
            try:
                from engine import candles as engine_candles
                engine_candles.save_candles(self.db_path, str(sec_id), symbol, df)
            except Exception:  # noqa: BLE001
                logger.debug("sonar: candle persist skipped", exc_info=True)
            closes = df["close"].dropna().astype(float).tolist()
            if len(closes) >= cfg.MIN_POINTS:
                out[str(sec_id)] = (symbol, closes)
        return out

    def scan(self) -> pd.DataFrame:
        self._ensure_table()
        rows = []
        for sid, (symbol, series) in self._series_map().items():
            if len(series) < cfg.MIN_POINTS:
                continue
            ss = super_smoother(series, cfg.SMOOTH_PERIOD)
            upper, lower, mid = dynamic_bands(series, ss, cfg.BAND_MULT)
            sp = slope_pct(ss, cfg.SLOPE_LOOKBACK, series[-1])
            res = classify(series[-2] if len(series) >= 2 else None,
                           series[-1], upper, lower, sp, cfg.MIN_SLOPE_PCT)
            if res["bias"] is None and res["signal"] == "NONE":
                continue  # nothing actionable
            rows.append({
                "security_id": sid, "symbol": symbol,
                "last": round(series[-1], 2), "mid": round(mid, 2) if mid else None,
                "trend": res["trend"], "signal": res["signal"], "bias": res["bias"],
                "support": res["support"], "resistance": res["resistance"],
                "slope_pct": round(sp, 3),
            })
        if not rows:
            logger.info("sonar: no actionable names this scan")
            return pd.DataFrame()
        # Rank: explicit breakouts/reversals first, then steeper trends.
        order = {"BREAKOUT_UP": 0, "BREAKDOWN": 0, "REVERSAL_UP": 1, "REVERSAL_DOWN": 1, "NONE": 2}
        df = pd.DataFrame(rows)
        df["_o"] = df["signal"].map(order).fillna(2)
        df = df.sort_values(["_o", "slope_pct"], key=lambda c: c.abs() if c.name == "slope_pct" else c,
                            ascending=[True, False]).drop(columns="_o").reset_index(drop=True)
        logger.info("sonar: %d actionable | %d breakouts",
                    len(df), df["signal"].isin(["BREAKOUT_UP", "BREAKDOWN"]).sum())
        return df

    # ---- persistence ------------------------------------------------------- #
    def _ensure_table(self):
        with self._connect() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {cfg.PERSIST_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    security_id TEXT NOT NULL, symbol TEXT, timestamp DATETIME NOT NULL,
                    last REAL, mid REAL, trend TEXT, signal TEXT, bias TEXT,
                    support REAL, resistance REAL, slope_pct REAL,
                    UNIQUE(security_id, timestamp)
                )""")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_sonar_sid_time "
                         f"ON {cfg.PERSIST_TABLE}(security_id, timestamp)")
            conn.commit()

    def persist(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        self._ensure_table()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        n = 0
        with self._connect() as conn:
            for _, r in df.iterrows():
                cur = conn.execute(
                    f"""INSERT INTO {cfg.PERSIST_TABLE}
                        (security_id, symbol, timestamp, last, mid, trend, signal,
                         bias, support, resistance, slope_pct)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(security_id, timestamp) DO NOTHING""",
                    (r["security_id"], r["symbol"], ts, r["last"], r["mid"], r["trend"],
                     r["signal"], r["bias"], r["support"], r["resistance"], r["slope_pct"]),
                )
                n += cur.rowcount
            conn.commit()
        logger.info("sonar: persisted %d rows", n)
        return n

    # ---- alerting ---------------------------------------------------------- #
    def send_telegram(self, df: pd.DataFrame) -> None:
        lines = ["📡 Sonar-Laplace Scanner (smoothed dynamic S/R)",
                 f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
        if df.empty:
            lines.append("No actionable smoothed breakouts/reversals this scan.")
        else:
            for _, r in df.head(cfg.TOP_N_ALERT).iterrows():
                dot = "🟢" if r["bias"] == CE else "🔴" if r["bias"] == PE else "⚪"
                lines.append(
                    f"{dot} {r['symbol']:<11} {r['signal']} | {r['trend']} "
                    f"| last {r['last']:.1f} (S {r['support']}/R {r['resistance']})"
                )
        lines.append("\nℹ️ Dynamic (moving) S/R — use as a trend/confirmation filter, "
                     "anchor entries to PDH/PDL, VWAP, ORB.")
        if notifications.notify("\n".join(lines), parse_mode=None):
            logger.info("sonar: alert sent")
        else:
            logger.info("sonar: alert skipped; no channel configured")


# --------------------------------------------------------------------------- #
# Consumer helper (for composite / morning_confluence)
# --------------------------------------------------------------------------- #
def get_latest_sonar(security_id: str, db_path: str | None = None) -> dict:
    path = db_path or iv_store.DB_PATH
    try:
        with sqlite3.connect(path) as conn:
            cur = conn.execute(
                f"""SELECT trend, signal, bias, support, resistance, slope_pct, timestamp, last
                    FROM {cfg.PERSIST_TABLE} WHERE security_id=?
                    ORDER BY timestamp DESC LIMIT 1""",
                (str(security_id),),
            )
            row = cur.fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    keys = ["trend", "signal", "bias", "support", "resistance", "slope_pct", "timestamp", "last"]
    return dict(zip(keys, row))
