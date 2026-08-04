# -*- coding: utf-8 -*-
"""
market_context/feed/normaliser.py — Upstox feed frame -> internal Tick.

WHY THIS IS DEFENSIVE
---------------------
The Upstox V3 feed is protobuf-decoded by the SDK into nested dicts whose
exact field names differ by mode and by feed variant (index vs market vs
option). The documented shapes that could be confirmed offline are:

    ltpc mode      {"feeds": {KEY: {"ltpc": {"ltp","ltt","ltq","cp"}}}, ...}
    option_chain   {"feeds": {KEY: {"oc": {"ltpc", "bidAskQuote",
                                           "optionGreeks", "eFeedDetails"}}}}
    eFeedDetails   {"atp","cp","vtt","oi","poi","dhoi","dloi","tbq","tsq",
                    "lc","uc"}

`full` mode adds D5 depth and 1-min/30-min/daily candles, but its exact
protobuf field naming (marketFF / indexFF / ff wrappers) could NOT be verified
without a live socket. Rather than hardcode a guess that fails silently at
09:15 on a Monday, this module walks the payload and picks up known leaf keys
wherever they appear.

That costs a little speed (a recursive scan over a small dict) and buys the
property that a wrapper-name change degrades one field rather than the whole
feed. Once the live shape is observed, `_TIGHTEN` below marks where to replace
the scan with direct indexing.

    _TIGHTEN: after first live capture, log one raw frame per mode and pin the
              paths. Keep the scan as the fallback.

FIELD MEANING (the reason this maps almost 1:1 onto the collection list)
-----------------------------------------------------------------------
    ltp   last traded price          cp    previous close
    atp   average traded price  ->   VWAP
    vtt   volume traded today   ->   cumulative; bar volume is a DELTA
    oi    open interest              poi   previous-day OI -> OI change
    tbq/tsq total buy/sell qty  ->   order-book imbalance (liquidity axis)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: Leaf keys we harvest, mapped to the Tick attribute they populate.
#:
#: TWO SCHEMAS LIVE HERE. The WebSocket feed uses terse protobuf names
#: (ltp/cp/atp/vtt/oi/poi), while the REST full-quote endpoint used by the
#: resync path (MarketQuoteApi.get_full_market_quote) uses verbose ones
#: (last_price/volume/average_price/...). They describe the same quantities.
#: Keeping one table for both is what lets a REST-recovered quote flow through
#: the same normaliser as a live frame — otherwise restore_quotes() silently
#: seeds nothing and a resync accomplishes exactly zero.
_SCALARS = {
    # --- WebSocket feed ---
    "ltp": "ltp",
    "cp": "prev_close",
    "atp": "vwap",
    "vtt": "volume_today",
    "oi": "oi",
    "poi": "oi_prev_day",
    "prevoi": "oi_prev_day",
    "dhoi": "oi_day_high",
    "dloi": "oi_day_low",
    "tbq": "total_buy_qty",
    "tsq": "total_sell_qty",
    "ltq": "last_qty",
    "iv": "iv",
    # --- REST full market quote ---
    "last_price": "ltp",
    "volume": "volume_today",
    "average_price": "vwap",
    "last_traded_quantity": "last_qty",
    "total_buy_quantity": "total_buy_qty",
    "total_sell_quantity": "total_sell_qty",
    "open_interest": "oi",
    "previous_oi": "oi_prev_day",
}

#: Depth quote field aliases seen across modes/versions.
_BID_PRICE = ("bp", "bidp", "bidprice")
_ASK_PRICE = ("ap", "askp", "askprice")
_BID_QTY = ("bq", "bidq", "bidqty")
_ASK_QTY = ("aq", "askq", "askqty")


@dataclass
class Tick:
    """One normalised observation for one instrument."""

    instrument_key: str
    received_at: datetime

    ltp: float | None = None
    prev_close: float | None = None
    vwap: float | None = None
    volume_today: float | None = None      # cumulative vtt
    oi: float | None = None
    oi_prev_day: float | None = None
    oi_day_high: float | None = None
    oi_day_low: float | None = None
    total_buy_qty: float | None = None
    total_sell_qty: float | None = None
    last_qty: float | None = None
    iv: float | None = None

    bid: float | None = None
    ask: float | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None

    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    day_close: float | None = None

    raw_keys: tuple[str, ...] = field(default_factory=tuple)

    # ---- derived ---------------------------------------------------------- #
    @property
    def chg_pct(self) -> float | None:
        if self.ltp is None or not self.prev_close:
            return None
        return (self.ltp - self.prev_close) / self.prev_close * 100.0

    @property
    def oi_chg_pct(self) -> float | None:
        if self.oi is None or not self.oi_prev_day:
            return None
        return (self.oi - self.oi_prev_day) / self.oi_prev_day * 100.0

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float | None:
        s = self.spread
        if s is None:
            return None
        mid = (self.bid + self.ask) / 2.0
        return (s / mid * 100.0) if mid > 0 else None

    @property
    def depth_imbalance(self) -> float | None:
        """total_buy_qty / total_sell_qty. >1 = bid-heavy."""
        if not self.total_buy_qty or not self.total_sell_qty:
            return None
        return self.total_buy_qty / self.total_sell_qty

    def is_empty(self) -> bool:
        """A frame carrying no price at all is not an observation."""
        return self.ltp is None and self.day_close is None


def _num(value) -> float | None:
    """Upstox sends several numerics as STRINGS (ltt, vtt, oi). Coerce, and
    treat the unparseable as missing rather than as zero — a false zero in
    volume would corrupt volume breadth."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _walk(node: Any, depth: int = 0, max_depth: int = 6) -> Iterable[tuple[str, Any]]:
    """Yield (lowercased_key, value) for every mapping entry, depth-limited."""
    if depth > max_depth:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key).lower(), value
            if isinstance(value, (dict, list)):
                yield from _walk(value, depth + 1, max_depth)
    elif isinstance(node, list):
        for item in node[:8]:          # depth quotes; only the top levels matter
            if isinstance(item, (dict, list)):
                yield from _walk(item, depth + 1, max_depth)


def _first_quote(payload: Any) -> dict | None:
    """Best bid/ask level, tolerating list-of-levels or a single dict."""
    for key, value in _walk(payload):
        if key not in ("bidaskquote", "bidask", "marketlevel"):
            continue
        candidate = value
        if isinstance(candidate, dict) and "bidaskquote" in {
            str(k).lower() for k in candidate
        }:
            for k2, v2 in candidate.items():
                if str(k2).lower() == "bidaskquote":
                    candidate = v2
                    break
        if isinstance(candidate, list):
            candidate = candidate[0] if candidate else None
        if isinstance(candidate, dict):
            return candidate
    return None


def _pick(mapping: dict, aliases) -> float | None:
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for alias in aliases:
        if alias in lowered:
            value = _num(lowered[alias])
            if value is not None:
                return value
    return None


def _day_ohlc(payload: Any) -> dict:
    """Daily OHLC, from either schema.

    WebSocket: `marketOHLC.ohlc` is a LIST of interval entries (1m / 30m / 1d)
    and the '1d' one must be selected explicitly — picking a 1-minute entry
    would make day_high/day_low a one-minute range, silently wrong rather than
    obviously broken.

    REST full quote: `ohlc` is a PLAIN DICT of open/high/low/close with no
    interval field, and is already the day's.
    """
    best: dict = {}
    for key, value in _walk(payload):
        if key != "ohlc":
            continue
        if isinstance(value, dict):
            lowered = {str(k).lower(): v for k, v in value.items()}
            if "close" in lowered or "open" in lowered:
                candidate = {
                    "day_open": _num(lowered.get("open")),
                    "day_high": _num(lowered.get("high")),
                    "day_low": _num(lowered.get("low")),
                    "day_close": _num(lowered.get("close")),
                }
                if any(v is not None for v in candidate.values()):
                    return candidate
            continue
        if not isinstance(value, list):
            continue
        for entry in value:
            if not isinstance(entry, dict):
                continue
            lowered = {str(k).lower(): v for k, v in entry.items()}
            interval = str(lowered.get("interval", "")).lower()
            if interval not in ("1d", "d1", "day", "1day"):
                continue
            best = {
                "day_open": _num(lowered.get("open")),
                "day_high": _num(lowered.get("high")),
                "day_low": _num(lowered.get("low")),
                "day_close": _num(lowered.get("close")),
            }
            if best.get("day_close") is not None:
                return best
    return best


def normalise_instrument(instrument_key: str, payload: Any,
                         received_at: datetime | None = None) -> Tick:
    """Build a Tick from one instrument's payload."""
    tick = Tick(instrument_key=instrument_key,
                received_at=received_at or datetime.now())

    seen: list[str] = []
    for key, value in _walk(payload):
        attr = _SCALARS.get(key)
        if attr is None:
            continue
        number = _num(value)
        # First occurrence wins: outer/summary fields precede nested repeats.
        if number is not None and getattr(tick, attr) is None:
            setattr(tick, attr, number)
            seen.append(key)

    quote = _first_quote(payload)
    if quote:
        tick.bid = _pick(quote, _BID_PRICE)
        tick.ask = _pick(quote, _ASK_PRICE)
        tick.bid_qty = _pick(quote, _BID_QTY)
        tick.ask_qty = _pick(quote, _ASK_QTY)

    for attr, value in _day_ohlc(payload).items():
        if value is not None:
            setattr(tick, attr, value)

    if tick.ltp is None and tick.day_close is not None:
        tick.ltp = tick.day_close

    tick.raw_keys = tuple(sorted(set(seen)))
    return tick


def normalise_message(message: Any,
                      received_at: datetime | None = None) -> list[Tick]:
    """Split one feed frame into per-instrument Ticks.

    Returns [] for heartbeats, acks and anything without a `feeds` mapping —
    those are normal traffic, not errors.
    """
    if not isinstance(message, dict):
        return []
    feeds = None
    for key, value in message.items():
        if str(key).lower() in ("feeds", "feed") and isinstance(value, dict):
            feeds = value
            break
    if not feeds:
        return []

    now = received_at or datetime.now()
    out: list[Tick] = []
    for instrument_key, payload in feeds.items():
        try:
            tick = normalise_instrument(str(instrument_key), payload, now)
        except Exception:
            logger.debug("normalise failed for %s", instrument_key, exc_info=True)
            continue
        if not tick.is_empty():
            out.append(tick)
    return out
