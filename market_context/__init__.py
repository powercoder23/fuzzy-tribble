# -*- coding: utf-8 -*-
"""
market_context — the single source of market description for every strategy.

Usage (the whole public surface):

    import market_context
    ctx = market_context.get()

    if ctx.available and ctx.volatility.is_(HIGH_VOL, PANIC):
        ...   # the STRATEGY decides what that means

Guarantees every consumer can rely on:

  1. ``get()`` NEVER raises.
  2. ``get()`` NEVER calls a broker API.
  3. ``get()`` performs at most ONE indexed SQLite read, and is process-cached
     for ``config.GET_CACHE_TTL_SEC`` — so a batch caller (e.g.
     ``paper_trader.collect_factor_snapshot`` booking 40 signals) does one
     read, not forty.
  4. Data older than ``config.MAX_CONTEXT_AGE_SEC`` is reported as
     UNAVAILABLE rather than returned stale. A stale regime is worse than no
     regime, because it looks like information.
  5. On ANY failure — subsystem off, DB missing, table missing, malformed row
     — the result is ``NEUTRAL_CONTEXT`` with ``available=False`` and every
     axis inert. No strategy can be broken by this subsystem.

PHASE 1 OPERATING RULE (operator decision 2026-08-03)
-----------------------------------------------------
Market Context is OBSERVATIONAL ONLY. It does not veto entries, change exits,
change stop-losses, change targets, or change sizing. It is collected and
persisted so that — after sufficient paper-trading data — its effect on
expectancy can be MEASURED before it is ever allowed to influence a decision.

``config.influences_trading()`` returns False and is the single greppable
predicate guarding that rule.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from market_context import config
from market_context.contracts import (  # noqa: F401  (re-exported public API)
    ALL_AXES,
    AXIS_BREADTH,
    AXIS_LIQUIDITY,
    AXIS_PARTICIPATION,
    AXIS_POSITIONING,
    AXIS_TREND,
    AXIS_VOLATILITY,
    AxisState,
    BREAKDOWN,
    BREAKOUT,
    EVENT_NONE,
    HIGH_PARTICIPATION,
    HIGH_VOL,
    ILLIQUID,
    LIQUID,
    LONG_BUILDUP,
    LONG_LIQUIDATION,
    LOW_PARTICIPATION,
    LOW_VOL,
    MarketContext,
    NEUTRAL_CONTEXT,
    NORMAL_LIQUIDITY,
    NORMAL_PARTICIPATION,
    NORMAL_VOL,
    PANIC,
    RANGE,
    REVERSAL,
    SCHEMA_VERSION,
    SHORT_BUILDUP,
    SHORT_COVERING,
    STABLE,
    STRENGTHENING,
    THIN,
    TRANSITIONING,
    TRENDING_DOWN,
    TRENDING_UP,
    UNKNOWN,
    WEAKENING,
    neutral_axis,
)

logger = logging.getLogger(__name__)

__all__ = [
    "get", "refresh", "invalidate", "MarketContext", "AxisState",
    "NEUTRAL_CONTEXT", "ALL_AXES", "SCHEMA_VERSION",
    "get_quote", "request_quote", "release_quote",
]

# --------------------------------------------------------------------------- #
# Process-local cache
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_cached: MarketContext | None = None
_cached_at: float = 0.0

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S")


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _age_seconds(ts_text) -> float | None:
    parsed = _parse_ts(ts_text)
    if parsed is None:
        return None
    return max((datetime.now() - parsed).total_seconds(), 0.0)


def _as_float(row, key, default=0.0) -> float:
    try:
        value = row.get(key)
        return float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _as_int(row, key, default=0) -> int:
    try:
        value = row.get(key)
        return int(value) if value is not None else int(default)
    except (TypeError, ValueError):
        return int(default)


def _as_str(row, key, default=UNKNOWN) -> str:
    value = row.get(key)
    return str(value) if value not in (None, "") else default


def _axis_from_row(name: str, row: dict, available_names: set,
                   inputs_by_axis: dict, reasons_by_axis: dict) -> AxisState:
    """Rebuild one AxisState from its mc_regime columns.

    An axis is only marked available when the writer listed it in
    `axes_available` AND its state column is populated and not UNKNOWN. Both
    conditions matter: a half-written row must not present as information.
    """
    state = _as_str(row, f"{name}_state", UNKNOWN)
    available = name in available_names and state not in (UNKNOWN, "", None)
    if not available:
        return neutral_axis(name)

    raw_inputs = inputs_by_axis.get(name) or {}
    raw_reasons = reasons_by_axis.get(name) or []
    return AxisState(
        name=name,
        state=state,
        score=_as_float(row, f"{name}_score"),
        confidence=_as_float(row, f"{name}_confidence"),
        direction=_as_str(row, f"{name}_direction", UNKNOWN),
        dwell_minutes=_as_int(row, f"{name}_dwell_minutes"),
        transition_prob=_as_float(row, f"{name}_transition_prob"),
        event=_as_str(row, "trend_event", EVENT_NONE) if name == AXIS_TREND else EVENT_NONE,
        available=True,
        inputs=dict(raw_inputs) if isinstance(raw_inputs, dict) else {},
        reasons=tuple(raw_reasons) if isinstance(raw_reasons, (list, tuple)) else (),
    )


def _build(row: dict) -> MarketContext:
    """mc_regime row -> MarketContext. Raises nothing the caller must handle;
    _load() wraps it."""
    from market_context.store import _json_or_none

    ts = row.get("ts")
    age = _age_seconds(ts)
    if age is None or age > config.MAX_CONTEXT_AGE_SEC:
        # Present but stale: report unavailable rather than mislead.
        logger.debug("market_context: snapshot stale (age=%s) — returning neutral", age)
        return NEUTRAL_CONTEXT

    available_names = set(_json_or_none(row.get("axes_available")) or [])
    inputs_by_axis = _json_or_none(row.get("axis_inputs")) or {}
    reasons_by_axis = _json_or_none(row.get("reasons")) or {}
    missing = _json_or_none(row.get("missing_inputs")) or []

    axes = {
        name: _axis_from_row(name, row, available_names, inputs_by_axis, reasons_by_axis)
        for name in ALL_AXES
    }

    return MarketContext(
        available=any(a.available for a in axes.values()),
        as_of=str(ts) if ts else None,
        age_seconds=age,
        data_quality=_as_float(row, "data_quality"),
        missing_inputs=tuple(missing) if isinstance(missing, (list, tuple)) else (),
        config_version=_as_str(row, "config_version", ""),
        config_hash=_as_str(row, "config_hash", ""),
        schema_version=_as_int(row, "schema_version", SCHEMA_VERSION),
        source="db",
        **axes,
    )


def _load() -> MarketContext:
    """Read + build, swallowing everything. This is the fail-open boundary."""
    try:
        if not config.is_enabled():
            return NEUTRAL_CONTEXT
        from market_context.store import latest_regime_row

        row = latest_regime_row()
        if not row:
            return NEUTRAL_CONTEXT
        return _build(row)
    except Exception:
        # Deliberately broad. Nothing in this subsystem may ever propagate an
        # exception into a scan loop — the platform's fail-open convention.
        logger.debug("market_context.get(): load failed, returning neutral",
                     exc_info=True)
        return NEUTRAL_CONTEXT


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def get(max_age_seconds: float | None = None) -> MarketContext:
    """Latest market context. Never raises, never calls a broker.

    max_age_seconds : optional per-call freshness override. When the cached
        snapshot is older than this, it is re-read from the DB. Use it only
        when a caller genuinely needs tighter freshness than
        ``config.GET_CACHE_TTL_SEC``; the default is right for scan loops.
    """
    global _cached, _cached_at

    ttl = config.GET_CACHE_TTL_SEC if max_age_seconds is None else max_age_seconds
    now = time.monotonic()

    cached = _cached
    if cached is not None and (now - _cached_at) < ttl:
        return cached

    with _lock:
        # Re-check inside the lock: another thread may have refreshed while we
        # waited, and a scan loop can call this from several threads.
        now = time.monotonic()
        if _cached is not None and (now - _cached_at) < ttl:
            return _cached
        ctx = _load()
        _cached = ctx
        _cached_at = now
        return ctx


def refresh() -> MarketContext:
    """Force a re-read, bypassing the cache."""
    invalidate()
    return get()


def invalidate() -> None:
    """Drop the process cache. Used by tests and after a config reload."""
    global _cached, _cached_at
    with _lock:
        _cached = None
        _cached_at = 0.0


# --------------------------------------------------------------------------- #
# Live quotes (fast-monitoring path for dynamically-subscribed instruments —
# open positions etc. See market_context/subscriptions via store.
# request_subscription/release_subscription, and service.py's reconcile +
# _flush_live_quotes for how mc_live_quotes gets populated.)
# --------------------------------------------------------------------------- #
_quote_cache: dict[str, tuple[dict | None, float]] = {}
_quote_lock = threading.Lock()


def get_quote(instrument_key: str, max_age_seconds: float | None = None) -> dict | None:
    """{'ltp': float, 'age_sec': float} for a subscribed instrument, or None
    when unavailable/stale/not subscribed. Never raises, never calls a
    broker — same guarantees as get(). Callers MUST treat None as "fall back
    to your existing REST path", not as an error.
    """
    if not instrument_key:
        return None
    ttl = config.GET_CACHE_TTL_SEC if max_age_seconds is None else max_age_seconds
    now = time.monotonic()

    with _quote_lock:
        hit = _quote_cache.get(instrument_key)
        if hit is not None and (now - hit[1]) < ttl:
            return hit[0]

    result = _load_quote(instrument_key)
    with _quote_lock:
        _quote_cache[instrument_key] = (result, now)
    return result


def _load_quote(instrument_key: str) -> dict | None:
    try:
        if not config.is_enabled():
            return None
        from market_context.store import latest_quote

        row = latest_quote(instrument_key)
        if not row:
            return None
        age = _age_seconds(row.get("ts"))
        if age is None or age > config.QUOTE_STALE_SEC:
            return None
        return {"ltp": row["ltp"], "age_sec": age}
    except Exception:
        logger.debug("market_context.get_quote(): load failed, returning None",
                     exc_info=True)
        return None


def request_quote(instrument_key: str, owner: str, mode: str = "ltpc",
                  ttl_minutes: int | None = None) -> bool:
    """Ask the feed to stream `instrument_key` live, on behalf of `owner`
    (e.g. f"trade:{trade_id}"). Thin wrapper around store.request_subscription
    so callers (order_manager etc.) don't need to import market_context.store
    directly. Never raises; returns whether the request was recorded."""
    try:
        from market_context.store import request_subscription
        return request_subscription([instrument_key], owner, mode, ttl_minutes) > 0
    except Exception:
        logger.debug("market_context.request_quote() failed (non-fatal)", exc_info=True)
        return False


def release_quote(owner: str, instrument_key: str | None = None) -> None:
    """Release one owner's live-quote request (a specific instrument_key, or
    everything that owner asked for). Never raises."""
    try:
        from market_context.store import release_subscription
        release_subscription(owner, [instrument_key] if instrument_key else None)
    except Exception:
        logger.debug("market_context.release_quote() failed (non-fatal)", exc_info=True)
