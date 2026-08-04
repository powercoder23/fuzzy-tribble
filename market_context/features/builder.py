# -*- coding: utf-8 -*-
"""
market_context/features/builder.py — assemble the point-in-time feature vector.

Reads persisted observations (mc_bars_1m, mc_breadth, mc_sector, mc_futures,
mc_vix) plus the VIX daily baseline from iv_history.db, and writes one
mc_features row per snapshot.

DESCRIPTION ONLY. Nothing here classifies or decides; classification is
regime/axes.py and trade policy belongs to the strategies.

Every field is optional. A missing input yields None, is listed in
`missing_inputs`, and lowers `data_quality` — which in turn caps confidence
downstream. That chain is the whole reason a thin-data reading cannot
masquerade as a confident one.
"""

from __future__ import annotations

import json
import logging
from contextlib import closing
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from market_context import config as cfg
from market_context import instruments as inst
from market_context import store
from market_context.features import estimators as est

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
IV_DB = str(DATA_DIR / "iv_history.db")

#: The index whose bars drive trend / volatility / structure.
REFERENCE_INDEX = inst.NIFTY_INDEX


@dataclass
class FeatureVector:
    ts: str = ""
    # trend
    ef_ratio: float | None = None
    ss_slope_pct: float | None = None
    mom_z: float | None = None
    vwap_position: float | None = None
    range_position: float | None = None
    orb_state: str | None = None
    prior_day_state: str | None = None
    breadth_divergence: float | None = None
    # volatility
    rv_yz_short: float | None = None
    rv_yz_long: float | None = None
    rv_ratio: float | None = None
    vix_level: float | None = None
    vix_percentile: float | None = None
    vol_of_vol: float | None = None
    vrp: float | None = None
    iv_ts_slope: float | None = None
    # liquidity
    spread_pctile: float | None = None
    depth_total: float | None = None
    depth_imbalance: float | None = None
    # participation
    volume_ratio: float | None = None
    trade_count_ratio: float | None = None
    active_names_pct: float | None = None
    # positioning
    nifty_quadrant: str | None = None
    banknifty_quadrant: str | None = None
    basis_ann_nifty: float | None = None
    basis_ann_banknifty: float | None = None
    stock_fut_long_pct: float | None = None
    # breadth
    adv_dec_pct: float | None = None
    volume_breadth_pct: float | None = None
    thrust: float | None = None
    sector_dispersion: float | None = None
    implied_corr_proxy: float | None = None
    # integrity
    data_quality: float = 0.0
    missing_inputs: list = field(default_factory=list)
    config_hash: str = ""
    config_version: str = ""
    # not persisted — handed to the axis classifiers
    breadth_is_subsample: bool = False

    def as_row(self) -> tuple:
        return (
            self.ts, self.ef_ratio, self.ss_slope_pct, self.mom_z,
            self.vwap_position, self.range_position, self.orb_state,
            self.prior_day_state, self.breadth_divergence,
            self.rv_yz_short, self.rv_yz_long, self.rv_ratio, self.vix_level,
            self.vix_percentile, self.vol_of_vol, self.vrp, self.iv_ts_slope,
            self.spread_pctile, self.depth_total, self.depth_imbalance,
            self.volume_ratio, self.trade_count_ratio, self.active_names_pct,
            self.nifty_quadrant, self.banknifty_quadrant, self.basis_ann_nifty,
            self.basis_ann_banknifty, self.stock_fut_long_pct,
            self.adv_dec_pct, self.volume_breadth_pct, self.thrust,
            self.sector_dispersion, self.implied_corr_proxy,
            self.data_quality, json.dumps(self.missing_inputs),
            self.config_hash, self.config_version,
        )

    def as_dict(self) -> dict:
        return asdict(self)


#: Inputs that count toward data_quality. Deliberately the ones the axes
#: actually consume — counting decorative fields would inflate the score.
_QUALITY_KEYS = (
    "ef_ratio", "ss_slope_pct", "vwap_position", "range_position",
    "rv_yz_short", "rv_yz_long", "vix_level", "vix_percentile",
    "spread_pctile", "depth_imbalance", "volume_ratio",
    "nifty_quadrant", "adv_dec_pct",
)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _recent_bars(instrument_key: str, limit: int, db_path=None) -> list[dict]:
    """Newest-last OHLCV bars for one instrument."""
    if not store.db_exists(db_path):
        return []
    try:
        with closing(store.connect(db_path, read_only=True)) as conn:
            rows = conn.execute(
                "SELECT ts, open, high, low, close, volume, vwap, bid, ask,"
                " bid_qty, ask_qty, tick_count FROM mc_bars_1m "
                "WHERE instrument_key = ? ORDER BY ts DESC LIMIT ?",
                (instrument_key, int(limit)),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
    except sqlite3.Error:
        logger.debug("feature builder: bar read failed", exc_info=True)
        return []


def _latest(table: str, db_path=None) -> dict | None:
    if not store.db_exists(db_path):
        return None
    try:
        with closing(store.connect(db_path, read_only=True)) as conn:
            row = conn.execute(
                f"SELECT * FROM {table} ORDER BY ts DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def _latest_futures(db_path=None) -> list[dict]:
    if not store.db_exists(db_path):
        return []
    try:
        with closing(store.connect(db_path, read_only=True)) as conn:
            latest = conn.execute("SELECT MAX(ts) AS ts FROM mc_futures").fetchone()
            if not latest or not latest["ts"]:
                return []
            rows = conn.execute(
                "SELECT * FROM mc_futures WHERE ts = ?", (latest["ts"],)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _breadth_series(lookback_minutes: int, db_path=None) -> list[dict]:
    if not store.db_exists(db_path):
        return []
    try:
        with closing(store.connect(db_path, read_only=True)) as conn:
            rows = conn.execute(
                "SELECT ts, adv_dec_pct FROM mc_breadth "
                "WHERE ts >= datetime('now','localtime',?) ORDER BY ts ASC",
                (f"-{int(lookback_minutes)} minutes",),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


#: (date, limit) -> closes. The daily VIX series changes ONCE A DAY, but the
#: feature builder runs every snapshot; without this the service would reopen
#: the 315 MB iv_history.db every 60 seconds to re-read the same 252 numbers.
_vix_cache: dict = {}


def _vix_daily_closes(limit: int) -> list[float]:
    """Trailing India VIX daily closes, for the percentile baseline.

    Read-only, cross-database, from the EXISTING vix_daily table. The daily
    collector and that table are untouched by this subsystem — intraday VIX
    complements the EOD series, it does not replace it.

    Cached per calendar day (see _vix_cache).
    """
    if not os.path.exists(IV_DB):
        return []
    cache_key = (datetime.now().strftime("%Y-%m-%d"), int(limit))
    cached = _vix_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        conn = sqlite3.connect(f"file:{IV_DB}?mode=ro", uri=True, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            rows = conn.execute(
                "SELECT close FROM vix_daily WHERE close IS NOT NULL "
                "ORDER BY date DESC LIMIT ?", (int(limit),)).fetchall()
        finally:
            conn.close()
        closes = [float(r[0]) for r in rows]
        _vix_cache.clear()                  # only ever one live day
        _vix_cache[cache_key] = closes
        return closes
    except sqlite3.Error:
        logger.debug("feature builder: vix_daily read failed", exc_info=True)
        return []


def _seasonal_volume(instrument_key: str, bucket_start: str, db_path=None):
    """(current_bucket_volume, median_of_same_bucket_on_prior_sessions).

    Intraday volume is strongly U-shaped, so comparing 09:20 with 13:00 is
    meaningless. Every reading is normalised against the SAME time-of-day
    bucket over the trailing sessions. Returns (None, None) until enough
    history exists — the platform computes this decay curve today
    (iv_analytics.intraday_decay_curve) and never uses it.
    """
    if not store.db_exists(db_path):
        return None, None
    bucket_minutes = max(cfg.PART_BUCKET_MINUTES, 1)
    try:
        with closing(store.connect(db_path, read_only=True)) as conn:
            rows = conn.execute(
                """
                SELECT substr(ts, 1, 10) AS d,
                       (CAST(strftime('%H', ts) AS INTEGER) * 60
                        + CAST(strftime('%M', ts) AS INTEGER)) / ? AS bucket,
                       SUM(volume) AS vol
                FROM mc_bars_1m
                WHERE instrument_key = ? AND volume IS NOT NULL
                GROUP BY d, bucket
                """,
                (bucket_minutes, instrument_key),
            ).fetchall()
    except sqlite3.Error:
        return None, None

    today = bucket_start[:10]
    try:
        minute_of_day = int(bucket_start[11:13]) * 60 + int(bucket_start[14:16])
    except (ValueError, IndexError):
        return None, None
    target = minute_of_day // bucket_minutes

    current = None
    history = []
    for row in rows:
        if int(row["bucket"]) != target:
            continue
        if row["d"] == today:
            current = float(row["vol"] or 0)
        else:
            history.append(float(row["vol"] or 0))
    if current is None or len(history) < cfg.PART_MIN_SESSIONS:
        return current, None
    history.sort()
    mid = len(history) // 2
    median = (history[mid] if len(history) % 2
              else (history[mid - 1] + history[mid]) / 2.0)
    return current, (median or None)


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build(cache=None, now: datetime | None = None, db_path=None) -> FeatureVector:
    """Assemble the feature vector for this snapshot. Never raises."""
    now = now or datetime.now()
    fv = FeatureVector(ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                       config_hash=cfg.config_hash(),
                       config_version=cfg.CONFIG_VERSION)
    try:
        _build_trend_and_vol(fv, cache, db_path)
        _build_liquidity(fv, db_path)
        _build_participation(fv, now, db_path)
        _build_positioning(fv, db_path)
        _build_breadth(fv, db_path)
    except Exception:
        logger.exception("feature builder: partial failure (continuing)")

    present = sum(1 for k in _QUALITY_KEYS if getattr(fv, k, None) is not None)
    fv.data_quality = present / len(_QUALITY_KEYS)
    fv.missing_inputs = [k for k in _QUALITY_KEYS if getattr(fv, k, None) is None]

    # A recent outage suppresses quality while the picture rebuilds.
    gap_sec = store.recent_gap_seconds(db_path, within_minutes=30)
    if gap_sec > 0:
        penalty = min(gap_sec / (30 * 60.0), 0.5)
        fv.data_quality = max(0.0, fv.data_quality * (1.0 - penalty))
    return fv


def _build_trend_and_vol(fv: FeatureVector, cache, db_path) -> None:
    need = max(cfg.VOL_RV_LONG_BARS, cfg.TREND_ER_LOOKBACK + 1,
               cfg.TREND_SS_LOOKBACK + cfg.TREND_SS_MIN_BARS) + 5
    bars = _recent_bars(REFERENCE_INDEX, need, db_path)
    closes = [b["close"] for b in bars if b.get("close") is not None]

    if len(closes) >= 3:
        fv.ef_ratio = est.efficiency_ratio(closes[-cfg.TREND_ER_LOOKBACK:])
        fv.ss_slope_pct = est.slope_pct(closes, cfg.TREND_SS_LOOKBACK,
                                        cfg.TREND_SS_PERIOD)

    ohlc = [(b["open"], b["high"], b["low"], b["close"]) for b in bars
            if None not in (b.get("open"), b.get("high"), b.get("low"), b.get("close"))]
    if len(ohlc) >= cfg.VOL_RV_MIN_BARS:
        fv.rv_yz_short = est.yang_zhang_vol(ohlc[-cfg.VOL_RV_SHORT_BARS:],
                                            cfg.VOL_ANNUALISATION_MINUTES)
        fv.rv_yz_long = est.yang_zhang_vol(ohlc[-cfg.VOL_RV_LONG_BARS:],
                                           cfg.VOL_ANNUALISATION_MINUTES)
        fv.rv_ratio = est.ratio(fv.rv_yz_short, fv.rv_yz_long)
        per_bar = est.realized_vol_per_bar(ohlc[-cfg.VOL_RV_SHORT_BARS:])
        fv.mom_z = est.vol_scaled_momentum(closes[-cfg.TREND_ER_LOOKBACK:], per_bar)

    # Live position within the session — from the cache when the socket is up,
    # else from the most recent bar.
    tick = cache.last(REFERENCE_INDEX) if cache is not None else None
    price = tick.ltp if tick and tick.ltp is not None else (
        closes[-1] if closes else None)
    vwap = tick.vwap if tick and tick.vwap is not None else (
        bars[-1].get("vwap") if bars else None)
    day_high = tick.day_high if tick else None
    day_low = tick.day_low if tick else None
    if day_high is None and bars:
        day_high = max((b["high"] for b in bars if b.get("high") is not None),
                       default=None)
    if day_low is None and bars:
        day_low = min((b["low"] for b in bars if b.get("low") is not None),
                      default=None)
    fv.vwap_position = est.vwap_position(price, vwap, day_high, day_low)
    fv.range_position = est.range_position(price, day_high, day_low)

    # VIX + variance risk premium
    vix_row = _latest("mc_vix", db_path)
    if vix_row and vix_row.get("ltp") is not None:
        fv.vix_level = float(vix_row["ltp"])
    elif tick is None and cache is not None:
        pass
    if cache is not None:
        vix_tick = cache.last(inst.INDIA_VIX)
        if vix_tick and vix_tick.ltp is not None:
            fv.vix_level = vix_tick.ltp

    history = _vix_daily_closes(cfg.VOL_VIX_PERCENTILE_LOOKBACK_DAYS)
    fv.vix_percentile = est.percentile_of(fv.vix_level, history)
    fv.vol_of_vol = est.stdev(est.pct_changes(list(reversed(history))[-30:]))
    if cfg.VRP_ENABLED:
        fv.vrp = est.variance_risk_premium(fv.vix_level, fv.rv_yz_long)

    _build_structure(fv, bars, price)


def _build_structure(fv: FeatureVector, bars, price) -> None:
    """Opening-range and prior-day location. Descriptive labels only."""
    if not bars or price is None:
        return
    day = bars[-1]["ts"][:10]
    todays = [b for b in bars if b["ts"][:10] == day]
    if len(todays) >= 2:
        span = max(cfg.STRUCT_ORB_MINUTES, 1)
        opening = todays[:span]
        highs = [b["high"] for b in opening if b.get("high") is not None]
        lows = [b["low"] for b in opening if b.get("low") is not None]
        if highs and lows:
            buffer_pct = cfg.STRUCT_BREAKOUT_BUFFER_PCT / 100.0
            hi, lo = max(highs), min(lows)
            if price > hi * (1 + buffer_pct):
                fv.orb_state = "ABOVE"
            elif price < lo * (1 - buffer_pct):
                fv.orb_state = "BELOW"
            else:
                fv.orb_state = "INSIDE"

    prior = [b for b in bars if b["ts"][:10] != day]
    if prior:
        highs = [b["high"] for b in prior if b.get("high") is not None]
        lows = [b["low"] for b in prior if b.get("low") is not None]
        if highs and lows:
            if price > max(highs):
                fv.prior_day_state = "ABOVE"
            elif price < min(lows):
                fv.prior_day_state = "BELOW"
            else:
                fv.prior_day_state = "INSIDE"


def _build_liquidity(fv: FeatureVector, db_path) -> None:
    """Spread percentile + depth, from the reference index's own session.

    Normalised against the instrument's OWN trailing distribution, so the
    number is comparable across instruments and does not need a per-symbol
    threshold.
    """
    bars = _recent_bars(REFERENCE_INDEX, cfg.LIQ_SPREAD_LOOKBACK_BARS, db_path)
    spreads = []
    for bar in bars:
        bid, ask = bar.get("bid"), bar.get("ask")
        if bid and ask and ask >= bid and (bid + ask) > 0:
            spreads.append((ask - bid) / ((ask + bid) / 2.0) * 100.0)
    if len(spreads) >= 5:
        fv.spread_pctile = est.percentile_of(spreads[-1], spreads[:-1])

    last = bars[-1] if bars else None
    if last:
        bid_qty, ask_qty = last.get("bid_qty"), last.get("ask_qty")
        if bid_qty is not None and ask_qty is not None:
            fv.depth_total = float(bid_qty) + float(ask_qty)
            if ask_qty:
                fv.depth_imbalance = float(bid_qty) / float(ask_qty)


def _build_participation(fv: FeatureVector, now: datetime, db_path) -> None:
    bucket_start = now.strftime("%Y-%m-%d %H:%M:00")
    current, baseline = _seasonal_volume(REFERENCE_INDEX, bucket_start, db_path)
    fv.volume_ratio = est.ratio(current, baseline)

    bars = _recent_bars(REFERENCE_INDEX, cfg.PART_BUCKET_MINUTES, db_path)
    counts = [b["tick_count"] for b in bars if b.get("tick_count")]
    if len(counts) >= 3:
        recent = counts[-1]
        prior = counts[:-1]
        avg = sum(prior) / len(prior) if prior else None
        fv.trade_count_ratio = est.ratio(recent, avg)

    breadth = _latest("mc_breadth", db_path)
    if breadth and breadth.get("sample_quality") is not None:
        fv.active_names_pct = float(breadth["sample_quality"]) * 100.0


def _build_positioning(fv: FeatureVector, db_path) -> None:
    rows = _latest_futures(db_path)
    if not rows:
        return
    long_like = {"LONG_BUILDUP", "SHORT_COVERING"}
    stock_rows = []
    for row in rows:
        symbol = (row.get("symbol") or "").upper()
        if symbol.startswith("NIFTY FUT") or symbol.startswith("NIFTY "):
            if fv.nifty_quadrant is None:
                fv.nifty_quadrant = row.get("quadrant")
                fv.basis_ann_nifty = row.get("basis_annualised")
        elif symbol.startswith("BANKNIFTY"):
            if fv.banknifty_quadrant is None:
                fv.banknifty_quadrant = row.get("quadrant")
                fv.basis_ann_banknifty = row.get("basis_annualised")
        else:
            stock_rows.append(row)

    graded = [r for r in stock_rows if r.get("quadrant") in
              ("LONG_BUILDUP", "SHORT_BUILDUP", "SHORT_COVERING", "LONG_LIQUIDATION")]
    if graded:
        longs = sum(1 for r in graded if r["quadrant"] in long_like)
        fv.stock_fut_long_pct = longs / len(graded) * 100.0


def _build_breadth(fv: FeatureVector, db_path) -> None:
    row = _latest("mc_breadth", db_path)
    if row:
        fv.adv_dec_pct = row.get("adv_dec_pct")
        fv.volume_breadth_pct = row.get("volume_breadth_pct")
        fv.breadth_is_subsample = bool(row.get("is_subsample"))

    series = _breadth_series(cfg.BREADTH_THRUST_LOOKBACK_MIN, db_path)
    points = [r["adv_dec_pct"] for r in series if r.get("adv_dec_pct") is not None]
    if len(points) >= 2:
        fv.thrust = points[-1] - points[0]

    sectors = _sector_returns(db_path)
    fv.sector_dispersion = est.stdev(sectors) if sectors else None
    if fv.sector_dispersion is not None and sectors:
        mean_abs = sum(abs(s) for s in sectors) / len(sectors)
        if mean_abs > 0:
            # Low dispersion relative to average move => everything moving
            # together => high implied correlation.
            fv.implied_corr_proxy = est.clip(
                1.0 - (fv.sector_dispersion / mean_abs))


def _sector_returns(db_path) -> list[float]:
    if not store.db_exists(db_path):
        return []
    try:
        with closing(store.connect(db_path, read_only=True)) as conn:
            latest = conn.execute("SELECT MAX(ts) AS ts FROM mc_sector").fetchone()
            if not latest or not latest["ts"]:
                return []
            rows = conn.execute(
                "SELECT ret_pct FROM mc_sector WHERE ts = ? AND ret_pct IS NOT NULL",
                (latest["ts"],)).fetchall()
        return [float(r[0]) for r in rows]
    except sqlite3.Error:
        return []


# --------------------------------------------------------------------------- #
# Persist
# --------------------------------------------------------------------------- #
def persist(fv: FeatureVector, db_path=None) -> bool:
    try:
        with closing(store.connect(db_path)) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO mc_features (ts, ef_ratio, ss_slope_pct,"
                " mom_z, vwap_position, range_position, orb_state,"
                " prior_day_state, breadth_divergence, rv_yz_short, rv_yz_long,"
                " rv_ratio, vix_level, vix_percentile, vol_of_vol, vrp,"
                " iv_ts_slope, spread_pctile, depth_total, depth_imbalance,"
                " volume_ratio, trade_count_ratio, active_names_pct,"
                " nifty_quadrant, banknifty_quadrant, basis_ann_nifty,"
                " basis_ann_banknifty, stock_fut_long_pct, adv_dec_pct,"
                " volume_breadth_pct, thrust, sector_dispersion,"
                " implied_corr_proxy, data_quality, missing_inputs,"
                " config_hash, config_version)"
                " VALUES (" + ",".join("?" * 37) + ")",
                fv.as_row(),
            )
            conn.commit()
        return True
    except Exception:
        logger.exception("feature builder: persist failed (non-fatal)")
        return False
