# -*- coding: utf-8 -*-
"""
signals/momentum.py — pure historical replay of momentum_strategy.py's ORB
and VWAP entry rules (momentum_strategy.MomentumScanner.check_orb_signal /
check_vwap_signal), against a multi-day candles DataFrame instead of "today".

Deliberate simplification vs. the live code: the live scanner's `df.iloc[-2]`
"last completed candle" trick exists only because its live fetch also
contains a still-forming current candle. Here each day's candles are all
already-closed bars, so the loop index IS the last completed candle directly
— same math, one fewer indirection. Everything else (opening range window,
volume-ratio gate, entry cutoff, VWAP reclaim/break rule) is copied as-is
from momentum_strategy.py, with thresholds pulled from momentum_config so
this can never silently drift from the live parameters.

One signal per day per symbol per rule (first qualifying bar) — once a
breakout/reclaim fires, a real trader doesn't keep re-entering the same setup
intraday.
"""

from __future__ import annotations

from datetime import time as dt_time

import pandas as pd

from momentum_config import ORB

ENTRY_CUTOFF = dt_time(ORB["entry_cutoff_hour"], ORB["entry_cutoff_min"])
RANGE_CANDLES = ORB["range_candles"]
VOLUME_MULT = ORB["volume_mult"]
VWAP_VOLUME_MULT = 1.3  # hardcoded in the live check_vwap_signal too


def _with_vwap(day_df: pd.DataFrame) -> pd.DataFrame:
    df = day_df.copy()
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"] = (tp * df["volume"]).cumsum() / df["volume"].replace(0, pd.NA).cumsum()
    df["vwap"] = df["vwap"].fillna(df["close"])
    return df


def _opening_range(day_df: pd.DataFrame) -> tuple[float, float] | None:
    if len(day_df) < RANGE_CANDLES + 1:
        return None
    orb = day_df.head(RANGE_CANDLES)
    return float(orb["high"].max()), float(orb["low"].min())


def find_orb_signals(candles: pd.DataFrame, symbol: str) -> list[dict]:
    """[{symbol, side, entry_ts, entry_price, trigger, volume_ratio}, ...]"""
    if candles is None or candles.empty:
        return []
    out = []
    for day, day_df in candles.groupby(candles["ts"].dt.date):
        day_df = day_df.sort_values("ts").reset_index(drop=True)
        orb = _opening_range(day_df)
        if not orb:
            continue
        orb_high, orb_low = orb
        if orb_high == orb_low:
            continue
        for i in range(RANGE_CANDLES, len(day_df)):
            row = day_df.iloc[i]
            if row["ts"].time() >= ENTRY_CUTOFF:
                break
            prior_vols = day_df["volume"].iloc[max(0, i - 5):i]
            prev5_avg = prior_vols.mean() if len(prior_vols) > 0 else 0
            vol_ratio = float(row["volume"] / prev5_avg) if prev5_avg > 0 else 0.0
            if vol_ratio < VOLUME_MULT:
                continue
            side = None
            if float(row["close"]) > orb_high:
                side = "CE"
            elif float(row["close"]) < orb_low:
                side = "PE"
            if side:
                out.append({
                    "symbol": symbol, "side": side, "trigger": "ORB",
                    "entry_ts": row["ts"], "entry_price": round(float(row["close"]), 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "orb_high": round(orb_high, 2), "orb_low": round(orb_low, 2),
                })
                break  # one ORB entry per symbol per day
    return out


def find_vwap_signals(candles: pd.DataFrame, symbol: str) -> list[dict]:
    if candles is None or candles.empty:
        return []
    out = []
    for day, day_df in candles.groupby(candles["ts"].dt.date):
        day_df = _with_vwap(day_df.sort_values("ts").reset_index(drop=True))
        for i in range(1, len(day_df)):
            row = day_df.iloc[i]
            if row["ts"].time() >= ENTRY_CUTOFF:
                break
            prev = day_df.iloc[i - 1]
            prior_vols = day_df["volume"].iloc[max(0, i - 5):i]
            prev5_avg = prior_vols.mean() if len(prior_vols) > 0 else 0
            vol_ratio = float(row["volume"] / prev5_avg) if prev5_avg > 0 else 0.0
            if vol_ratio < VWAP_VOLUME_MULT:
                continue
            bullish = float(prev["close"]) < float(prev["vwap"]) and float(row["close"]) > float(row["vwap"])
            bearish = float(prev["close"]) > float(prev["vwap"]) and float(row["close"]) < float(row["vwap"])
            side = trigger = None
            if bullish:
                side, trigger = "CE", "vwap_reclaim"
            elif bearish:
                side, trigger = "PE", "vwap_break"
            if side:
                out.append({
                    "symbol": symbol, "side": side, "trigger": trigger,
                    "entry_ts": row["ts"], "entry_price": round(float(row["close"]), 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "vwap": round(float(row["vwap"]), 2),
                })
                break  # one VWAP entry per symbol per day
    return out


def find_signals(candles: pd.DataFrame, symbol: str, rule: str = "both") -> list[dict]:
    """rule: 'orb' | 'vwap' | 'both'. Merged, sorted by entry_ts."""
    sigs = []
    if rule in ("orb", "both"):
        sigs += find_orb_signals(candles, symbol)
    if rule in ("vwap", "both"):
        sigs += find_vwap_signals(candles, symbol)
    return sorted(sigs, key=lambda s: s["entry_ts"])
