# -*- coding: utf-8 -*-
"""
market_context/collect/snapshots.py — turn the live TickCache into persisted
observations: VIX, futures positioning, breadth, sector strength.

Phase 1 produces DATA ONLY. No classification happens here (that is Phase 2's
axis engine) and nothing influences trading.

BREADTH IS MEASURED AGAINST PREVIOUS CLOSE
------------------------------------------
Tier-3 stocks stream in `ltpc` mode, which carries `cp` (previous close). So
advance/decline is computed against the previous close — the standard
definition, and a real fix for the legacy breadth.py, which used the FIRST
IV-collector sweep of the day (~09:15-09:30) as its "open" and was therefore
blind to any opening gap.

VOLUME BREADTH IS DELIBERATELY NARROW
-------------------------------------
`ltpc` carries no volume, so up/down volume can only be computed over
instruments subscribed in `full` mode (tier 1 + tier 4 futures). That is a
small, futures-only sample and it is recorded as such: `volume_breadth_pct`
is written with the sample size it was computed from, and consumers that
care can reject it. Reporting a futures-only figure as market volume breadth
would be exactly the kind of quiet misrepresentation this subsystem exists to
remove.
"""

from __future__ import annotations

import logging
from contextlib import closing
import statistics
from datetime import date, datetime

from market_context import config as cfg
from market_context import instruments as inst
from market_context import store
from market_context.feed.cache import TickCache
from market_context.feed.subscription import SubscriptionPlan

logger = logging.getLogger(__name__)


def _ts(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# India VIX
# --------------------------------------------------------------------------- #
def collect_vix(cache: TickCache, now: datetime | None = None,
                db_path: str | None = None) -> dict | None:
    """Persist the intraday India VIX reading.

    This is the fix for the platform's largest context gap: until now VIX was
    EOD-only (collectors/vix_collector.py -> vix_daily), so engine/regime.py's
    `VIX_RED = 22` no-trade gate was evaluating YESTERDAY's close and could
    not fire on a day that spiked intraday.

    `percentile` / `z_score` / `vol_of_vol` are left NULL in Phase 1 — they
    need the daily baseline join, which belongs with the volatility axis in
    Phase 2.
    """
    tick = cache.last(inst.INDIA_VIX)
    if tick is None or tick.ltp is None:
        return None
    row = {
        "ts": _ts(now),
        "ltp": tick.ltp,
        "prev_close": tick.prev_close,
        "chg_pct": tick.chg_pct,
        "day_open": tick.day_open,
        "day_high": tick.day_high,
        "day_low": tick.day_low,
    }
    try:
        with closing(store.connect(db_path)) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO mc_vix (ts, ltp, prev_close, chg_pct,"
                " day_open, day_high, day_low) VALUES (?,?,?,?,?,?,?)",
                (row["ts"], row["ltp"], row["prev_close"], row["chg_pct"],
                 row["day_open"], row["day_high"], row["day_low"]),
            )
            conn.commit()
    except Exception:
        logger.exception("collect_vix: write failed (non-fatal)")
        return None
    return row


# --------------------------------------------------------------------------- #
# Futures positioning
# --------------------------------------------------------------------------- #
_SPOT_KEY_BY_UNDERLYING = {
    "NIFTY": inst.NIFTY_INDEX,
    "BANKNIFTY": inst.BANKNIFTY_INDEX,
}


def classify_quadrant(price_chg_pct, oi_chg_pct,
                      min_price_pct: float | None = None,
                      min_oi_pct: float | None = None) -> str:
    """The India-standard futures price x OI quadrant.

    Deadbands are MANDATORY. Without them a 0.01% drift produces a confident
    LONG_BUILDUP, and the positioning axis becomes noise wearing a label.

    The distinction this buys: a rally on SHORT_COVERING with falling OI is
    exhausted buying, not new conviction, and typically does not continue —
    which is precisely where a breakout entry gets trapped.
    """
    from market_context.contracts import (
        LONG_BUILDUP, LONG_LIQUIDATION, POSITIONING_NEUTRAL, SHORT_BUILDUP,
        SHORT_COVERING, UNKNOWN,
    )
    if price_chg_pct is None or oi_chg_pct is None:
        return UNKNOWN
    min_price = cfg.POS_MIN_PRICE_CHG_PCT if min_price_pct is None else min_price_pct
    min_oi = cfg.POS_MIN_OI_CHG_PCT if min_oi_pct is None else min_oi_pct
    if abs(price_chg_pct) < min_price or abs(oi_chg_pct) < min_oi:
        return POSITIONING_NEUTRAL
    if price_chg_pct > 0:
        return LONG_BUILDUP if oi_chg_pct > 0 else SHORT_COVERING
    return SHORT_BUILDUP if oi_chg_pct > 0 else LONG_LIQUIDATION


def annualised_basis(basis_pct: float | None, dte: int | None) -> float | None:
    """Basis as an annualised carry rate. Comparable across expiries — a 0.4%
    basis at 3 DTE and at 30 DTE are very different positioning signals."""
    if basis_pct is None or not dte or dte <= 0:
        return None
    return basis_pct * (cfg.POS_BASIS_ANNUALISATION_DAYS / dte)


def collect_futures(cache: TickCache, plan: SubscriptionPlan,
                    now: datetime | None = None,
                    db_path: str | None = None) -> list[dict]:
    """Persist basis + OI quadrant for every subscribed future.

    Spot is taken from the SAME cache snapshot as the future, so the two legs
    of the basis are timestamp-aligned — which is why futures and their
    underlying are deliberately placed on the same connection.
    """
    now = now or datetime.now()
    today = now.date()
    rows = []
    for future in plan.futures():
        tick = cache.last(future.instrument_key)
        if tick is None or tick.ltp is None:
            continue

        spot_key = _SPOT_KEY_BY_UNDERLYING.get((future.underlying or "").upper())
        if spot_key is None:
            found = next((i for i in plan.by_tier.get(3, [])
                          if i.symbol.upper() == (future.underlying or "").upper()), None)
            spot_key = found.instrument_key if found else None
        spot_tick = cache.last(spot_key) if spot_key else None
        spot = spot_tick.ltp if spot_tick else None

        basis = (tick.ltp - spot) if (spot is not None) else None
        basis_pct = (basis / spot * 100.0) if (basis is not None and spot) else None
        dte = None
        if future.expiry:
            try:
                dte = (date.fromisoformat(future.expiry) - today).days
            except ValueError:
                dte = None

        row = {
            "ts": _ts(now),
            "instrument_key": future.instrument_key,
            "symbol": future.symbol,
            "expiry": future.expiry,
            "dte": dte,
            "ltp": tick.ltp,
            "spot": spot,
            "basis": basis,
            "basis_pct": basis_pct,
            "basis_annualised": annualised_basis(basis_pct, dte),
            "oi": tick.oi,
            "oi_prev_day": tick.oi_prev_day,
            "oi_chg_pct": tick.oi_chg_pct,
            "price_chg_pct": tick.chg_pct,
            "quadrant": classify_quadrant(tick.chg_pct, tick.oi_chg_pct),
            "volume": tick.volume_today,
            "vwap": tick.vwap,
            "day_open": tick.day_open,
            "day_high": tick.day_high,
            "day_low": tick.day_low,
            "prev_close": tick.prev_close,
        }
        rows.append(row)

    if not rows:
        return []
    try:
        with closing(store.connect(db_path)) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO mc_futures (ts, instrument_key, symbol,"
                " expiry, dte, ltp, spot, basis, basis_pct, basis_annualised,"
                " oi, oi_prev_day, oi_chg_pct, price_chg_pct, quadrant, volume,"
                " vwap, day_open, day_high, day_low, prev_close)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [tuple(r[k] for k in (
                    "ts", "instrument_key", "symbol", "expiry", "dte", "ltp",
                    "spot", "basis", "basis_pct", "basis_annualised", "oi",
                    "oi_prev_day", "oi_chg_pct", "price_chg_pct", "quadrant",
                    "volume", "vwap", "day_open", "day_high", "day_low",
                    "prev_close")) for r in rows],
            )
            conn.commit()
    except Exception:
        logger.exception("collect_futures: write failed (non-fatal)")
        return []
    return rows


# --------------------------------------------------------------------------- #
# Breadth
# --------------------------------------------------------------------------- #
def collect_breadth(cache: TickCache, plan: SubscriptionPlan,
                    now: datetime | None = None,
                    db_path: str | None = None) -> dict | None:
    """Advance/decline (and, where available, volume) breadth vs previous close."""
    constituents = plan.by_tier.get(3, [])
    if not constituents:
        return None

    advancing = declining = unchanged = 0
    seen = 0
    up_volume = down_volume = 0.0
    volume_names = 0

    for item in constituents:
        tick = cache.last(item.instrument_key)
        if tick is None or tick.chg_pct is None:
            continue
        seen += 1
        chg = tick.chg_pct
        if chg >= cfg.BREADTH_MIN_MOVE_PCT:
            advancing += 1
        elif chg <= -cfg.BREADTH_MIN_MOVE_PCT:
            declining += 1
        else:
            unchanged += 1
        if tick.volume_today:
            volume_names += 1
            if chg > 0:
                up_volume += tick.volume_today
            elif chg < 0:
                down_volume += tick.volume_today

    moving = advancing + declining
    adv_dec_pct = (advancing / moving * 100.0) if moving else None
    if moving < cfg.BREADTH_MIN_NAMES:
        adv_dec_pct = None          # too few names to be a reading at all

    total_volume = up_volume + down_volume
    volume_breadth_pct = (up_volume / total_volume * 100.0) if total_volume else None

    universe_size = len(constituents)
    row = {
        "ts": _ts(now),
        "universe_size": universe_size,
        "advancing": advancing,
        "declining": declining,
        "unchanged": unchanged,
        "adv_dec_pct": adv_dec_pct,
        "up_volume": up_volume or None,
        "down_volume": down_volume or None,
        "volume_breadth_pct": volume_breadth_pct,
        # Surfaced, not hidden: below this the reading is a partial-universe
        # subsample (see config.BREADTH_FULL_UNIVERSE_MIN), and the Phase-2
        # confidence model caps accordingly.
        "is_subsample": 1 if universe_size < cfg.BREADTH_FULL_UNIVERSE_MIN else 0,
        "sample_quality": (seen / universe_size) if universe_size else 0.0,
    }
    try:
        with closing(store.connect(db_path)) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO mc_breadth (ts, universe_size, advancing,"
                " declining, unchanged, adv_dec_pct, up_volume, down_volume,"
                " volume_breadth_pct, is_subsample, sample_quality)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row[k] for k in (
                    "ts", "universe_size", "advancing", "declining", "unchanged",
                    "adv_dec_pct", "up_volume", "down_volume",
                    "volume_breadth_pct", "is_subsample", "sample_quality")),
            )
            conn.commit()
    except Exception:
        logger.exception("collect_breadth: write failed (non-fatal)")
        return None
    return row


# --------------------------------------------------------------------------- #
# Sector strength + dispersion
# --------------------------------------------------------------------------- #
def collect_sector(cache: TickCache, plan: SubscriptionPlan,
                   now: datetime | None = None,
                   db_path: str | None = None) -> list[dict]:
    """Per-sector return, relative strength vs NIFTY, and RS rank.

    Cross-sectional dispersion (stdev of sector returns) is computed here and
    surfaced via the return value; it is the cheap proxy for an implied
    correlation regime. It matters for this platform specifically because the
    concentration gate is armed but empty (both caps 0 = unlimited), so
    nothing currently detects that N "diversified" positions are one bet.
    """
    sectors = plan.by_tier.get(2, [])
    if not sectors:
        return []

    nifty = cache.last(inst.NIFTY_INDEX)
    nifty_ret = nifty.chg_pct if nifty else None

    rows = []
    for item in sectors:
        tick = cache.last(item.instrument_key)
        if tick is None or tick.chg_pct is None:
            continue
        rows.append({
            "ts": _ts(now),
            "sector": item.symbol or item.instrument_key.split("|", 1)[-1],
            "ret_pct": tick.chg_pct,
            "rel_strength": (tick.chg_pct - nifty_ret) if nifty_ret is not None else None,
            "advancing": None, "declining": None, "breadth_pct": None,
            "n_names": None,
        })

    rows.sort(key=lambda r: r["ret_pct"], reverse=True)
    for rank, row in enumerate(rows, start=1):
        row["rs_rank"] = rank

    if not rows:
        return []
    try:
        with closing(store.connect(db_path)) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO mc_sector (ts, sector, ret_pct,"
                " rel_strength, rs_rank, advancing, declining, breadth_pct, n_names)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [tuple(r[k] for k in ("ts", "sector", "ret_pct", "rel_strength",
                                      "rs_rank", "advancing", "declining",
                                      "breadth_pct", "n_names")) for r in rows],
            )
            conn.commit()
    except Exception:
        logger.exception("collect_sector: write failed (non-fatal)")
        return []
    return rows


def sector_dispersion(sector_rows) -> float | None:
    """Cross-sectional stdev of sector returns. High = stock-picking regime;
    low = macro/correlated regime."""
    values = [r["ret_pct"] for r in (sector_rows or []) if r.get("ret_pct") is not None]
    if len(values) < cfg.DISPERSION_MIN_SECTORS:
        return None
    try:
        return statistics.pstdev(values)
    except statistics.StatisticsError:
        return None


def collect_all(cache: TickCache, plan: SubscriptionPlan,
                now: datetime | None = None, db_path: str | None = None) -> dict:
    """One snapshot pass. Each collector is independent and fail-soft, so a
    failure in one never blocks the others."""
    now = now or datetime.now()
    out = {"ts": _ts(now)}
    out["vix"] = collect_vix(cache, now, db_path)
    out["futures"] = collect_futures(cache, plan, now, db_path)
    out["breadth"] = collect_breadth(cache, plan, now, db_path)
    sectors = collect_sector(cache, plan, now, db_path)
    out["sector"] = sectors
    out["dispersion"] = sector_dispersion(sectors)
    return out
