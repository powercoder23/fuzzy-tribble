# -*- coding: utf-8 -*-
"""
market_context/feed/resync.py — REST recovery after a feed outage.

REST is the RECOVERY path, never the data path. On a clean session this module
issues ZERO calls. It runs only when the gap that just closed was longer than
``config.RESYNC_THRESHOLD_SEC`` (default 120s).

WHAT AN OUTAGE ACTUALLY BREAKS
------------------------------
Losing the socket for a few minutes costs more than the missing ticks:

  1. **Day-cumulative state is gone.** `vtt` (volume traded today) and `oi`
     are cumulative. The cache clears its volume baselines on disconnect
     (TickCache.mark_disconnected), so without a resync EVERY subsequent bar
     that session writes NULL volume — one 90-second blip would silently
     destroy volume breadth for the rest of the day.

  2. **The gap is invisible in the data.** A 20-minute outage looks, in
     mc_bars_1m, exactly like 20 minutes of a market that did not move.

So resync does two things:

  * ONE batched `get_full_market_quote` call (the endpoint accepts up to 500
    instruments) to re-seed day-cumulative state for every subscribed key;
  * per-tier-1 intraday candle backfill to fill mc_bars_1m across the gap,
    written with ``tick_count = 0`` so research can tell a reconstructed bar
    from an observed one.

Everything is fail-soft. A resync that fails leaves the session with NULL
volumes and a recorded gap — degraded but honest, which is the correct
failure mode for a measurement system.
"""

from __future__ import annotations

import logging
from contextlib import closing
from datetime import datetime, timedelta

from market_context import config as cfg
from market_context import store
from market_context.feed.cache import TickCache, floor_minute
from market_context.feed.normaliser import normalise_instrument
from market_context.feed.subscription import SubscriptionPlan

logger = logging.getLogger(__name__)

#: get_full_market_quote accepts up to 500 instruments per call.
QUOTE_BATCH_SIZE = 500

#: Upstox API version header required by the v2 market-quote endpoints.
API_VERSION = "2.0"


class Resyncer:
    """REST recovery for the market-context feed."""

    def __init__(self, plan: SubscriptionPlan, cache: TickCache,
                 db_path: str | None = None, api_client_factory=None):
        self.plan = plan
        self.cache = cache
        self.db_path = db_path
        #: Injectable for tests; defaults to building a real Upstox client.
        self._api_client_factory = api_client_factory or self._default_api_client

    # ---- client ----------------------------------------------------------- #
    def _default_api_client(self):
        from upstox_token_manager import load_upstox_token
        import upstox_client

        token = load_upstox_token()
        if not token:
            raise RuntimeError("no Upstox access token available")
        configuration = upstox_client.Configuration()
        configuration.access_token = token
        return upstox_client.ApiClient(configuration)

    # ---- entry point ------------------------------------------------------ #
    def resync(self, gap_seconds: float, gap_started: datetime | None = None,
               gap_ended: datetime | None = None) -> dict:
        """Recover after a gap. Returns a summary dict (never raises).

        `skipped` is the normal outcome: most reconnects are fast enough that
        the next few frames rebuild everything on their own.
        """
        result = {"skipped": True, "reason": None, "quotes_seeded": 0,
                  "bars_backfilled": 0, "gap_seconds": gap_seconds}

        if gap_seconds < cfg.RESYNC_THRESHOLD_SEC:
            result["reason"] = (f"gap {gap_seconds:.0f}s < threshold "
                                f"{cfg.RESYNC_THRESHOLD_SEC:.0f}s")
            return result
        if not self.plan or not self.plan.all_keys():
            result["reason"] = "empty subscription plan"
            return result

        try:
            api_client = self._api_client_factory()
        except Exception as exc:
            logger.warning("resync: could not build API client (%s) — session "
                           "continues with NULL volumes", exc)
            result["reason"] = f"api client unavailable: {exc}"
            return result

        result["skipped"] = False
        try:
            result["quotes_seeded"] = self.restore_quotes(api_client)
        except Exception:
            logger.exception("resync: quote restore failed (non-fatal)")
        try:
            result["bars_backfilled"] = self.backfill_bars(
                api_client, gap_started, gap_ended)
        except Exception:
            logger.exception("resync: bar backfill failed (non-fatal)")

        logger.info("resync complete after %.0fs gap: %d quote(s) seeded, "
                    "%d bar(s) backfilled",
                    gap_seconds, result["quotes_seeded"], result["bars_backfilled"])
        return result

    # ---- step 1: day-cumulative state ------------------------------------- #
    def restore_quotes(self, api_client) -> int:
        """Re-seed the cache from one batched full-quote call per 500 keys.

        This is what makes volume measurable again for the rest of the
        session: it restores each instrument's `vtt` so the NEXT bar has a
        baseline to subtract from.
        """
        import upstox_client

        api = upstox_client.MarketQuoteApi(api_client)
        keys = self.plan.all_keys()
        seeded = 0
        now = datetime.now()

        for start in range(0, len(keys), QUOTE_BATCH_SIZE):
            batch = keys[start:start + QUOTE_BATCH_SIZE]
            try:
                response = api.get_full_market_quote(",".join(batch), API_VERSION)
            except Exception:
                logger.exception("resync: get_full_market_quote failed for %d key(s)",
                                 len(batch))
                continue

            payload = getattr(response, "data", None) or {}
            if not isinstance(payload, dict):
                try:
                    payload = dict(payload)
                except (TypeError, ValueError):
                    logger.warning("resync: unexpected quote payload type %s",
                                   type(payload).__name__)
                    continue

            for returned_key, quote in payload.items():
                # The quote endpoint keys its response by trading symbol
                # (e.g. "NSE_EQ:INFY"), NOT by instrument_key. Map back via
                # the plan; fall back to the returned key so an unmatched
                # entry is skipped rather than mis-attributed.
                instrument_key = self._match_key(returned_key, quote, batch)
                if instrument_key is None:
                    continue
                tick = normalise_instrument(
                    instrument_key, _to_plain(quote), received_at=now)
                if tick.is_empty():
                    continue
                self.cache.seed(tick)
                seeded += 1

        return seeded

    def _match_key(self, returned_key: str, quote, batch) -> str | None:
        """Resolve a quote-response key back to our instrument_key."""
        # 1. The response may echo the instrument_key directly.
        if returned_key in batch:
            return returned_key
        # 2. Some responses carry it as a field on the quote itself.
        for field in ("instrument_token", "instrumentToken", "instrument_key"):
            value = _get(quote, field)
            if value and str(value) in batch:
                return str(value)
        # 3. Fall back to matching on the trading symbol after the colon.
        symbol = str(returned_key).split(":")[-1].strip().upper()
        for item in self.plan.by_tier.values():
            for candidate in item:
                if (candidate.symbol or "").strip().upper() == symbol:
                    return candidate.instrument_key
        return None

    # ---- step 2: bar backfill --------------------------------------------- #
    def backfill_bars(self, api_client, gap_started: datetime | None,
                      gap_ended: datetime | None) -> int:
        """Fill mc_bars_1m across the gap for tier-1 instruments only.

        Tier 1 only, deliberately: those are the instruments the regime engine
        cannot run without, and one candle call per instrument is the
        expensive part of recovery. Breadth constituents are left with a hole,
        which mc_feed_gaps already documents.

        Backfilled rows carry `tick_count = 0`, which is the marker that
        distinguishes a reconstructed bar from an observed one. Research can
        exclude or down-weight them; without the marker the two would be
        indistinguishable.
        """
        import upstox_client

        api = upstox_client.HistoryV3Api(api_client)
        keys = self.plan.tier1_keys()
        if not keys:
            return 0

        start = floor_minute(gap_started) if gap_started else None
        end = floor_minute(gap_ended) if gap_ended else floor_minute(datetime.now())

        rows: list[tuple] = []
        for key in keys:
            try:
                response = api.get_intra_day_candle_data(key, "minutes", 1)
            except Exception:
                logger.exception("resync: intraday candles failed for %s", key)
                continue
            candles = _candles_from(response)
            for candle in candles:
                row = _candle_to_bar_row(key, candle, start, end)
                if row is not None:
                    rows.append(row)

        if not rows:
            return 0
        try:
            with closing(store.connect(self.db_path)) as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO mc_bars_1m "
                    "(instrument_key, ts, open, high, low, close, volume, oi,"
                    " oi_chg, vwap, bid, ask, bid_qty, ask_qty, tick_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()
        except Exception:
            logger.exception("resync: bar backfill write failed")
            return 0
        return len(rows)


# --------------------------------------------------------------------------- #
# Helpers — tolerant of both SDK model objects and plain dicts
# --------------------------------------------------------------------------- #
def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_plain(obj):
    """Best-effort conversion of an SDK model into a nested plain dict so the
    normaliser's key walk works on it."""
    if isinstance(obj, (dict, list)) or obj is None:
        return obj
    for attr in ("to_dict", "_asdict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict):
        return {k.lstrip("_"): v for k, v in data.items()}
    return {}


def _candles_from(response) -> list:
    data = _get(response, "data")
    candles = _get(data, "candles") if data is not None else None
    return list(candles) if candles else []


def _candle_to_bar_row(instrument_key: str, candle, start: str | None,
                       end: str | None) -> tuple | None:
    """Upstox candle -> mc_bars_1m row, filtered to the gap window.

    Candle layout is [timestamp, open, high, low, close, volume, oi].
    """
    if not isinstance(candle, (list, tuple)) or len(candle) < 5:
        return None
    raw_ts = str(candle[0])
    # '2026-08-04T10:15:00+05:30' -> '2026-08-04 10:15:00'
    ts = raw_ts.replace("T", " ")[:19]
    if len(ts) < 19:
        return None
    ts = ts[:17] + "00"                     # floor to the minute
    if start and ts < start:
        return None
    if end and ts > end:
        return None

    def _f(index):
        try:
            return float(candle[index])
        except (TypeError, ValueError, IndexError):
            return None

    return (instrument_key, ts, _f(1), _f(2), _f(3), _f(4),
            _f(5), _f(6), None, None, None, None, None, None,
            0)                              # tick_count=0 => reconstructed


def window_from_gap(gap_started: datetime | None,
                    gap_ended: datetime | None) -> tuple[str | None, str | None]:
    """Public helper: the (start, end) minute strings a gap covers."""
    start = floor_minute(gap_started) if gap_started else None
    end = floor_minute(gap_ended or datetime.now())
    return start, end
