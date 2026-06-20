# -*- coding: utf-8 -*-
"""
data_provider.py — the single L2 data loader (ARCHITECTURE_REFACTOR_PLAN.md §4, §12).

Instead of every strategy fetching candles on demand, two continuous background
pollers OWN candle fetching; strategies just subscribe/unsubscribe instruments.

  * CandlePoller(5)  — wakes shortly after each 5-min candle close, fetches the
                       latest 5-min candle for every SUBSCRIBED instrument once.
  * CandlePoller(15) — same on the 15-min boundary.

Each instrument is fetched ONCE per interval no matter how many strategies need
it (the union of subscribers is the fetch set). Strategies read candles via
DataProvider.intraday_candles()/daily_candles(), which serve the cache and fall
back to a direct fetch on a miss — so adoption is incremental and safe.

The fetch functions are injectable, so the pure poller/cache logic is unit-tested
with a fake fetcher (no broker). In production, pass a DiscountedPremiumScanner and
the provider reuses the existing, tested momentum candle fetchers.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Candle cache
# --------------------------------------------------------------------------- #
class CandleCache:
    """Thread-safe latest-candles store, keyed by (instrument, interval)."""

    def __init__(self):
        self._df: dict = {}
        self._ts: dict = {}
        self._lock = threading.Lock()

    def put(self, instrument, interval, df, ts=None):
        with self._lock:
            self._df[(str(instrument), int(interval))] = df
            self._ts[(str(instrument), int(interval))] = ts or datetime.now()

    def get(self, instrument, interval):
        with self._lock:
            return self._df.get((str(instrument), int(interval)))

    def timestamp(self, instrument, interval):
        with self._lock:
            return self._ts.get((str(instrument), int(interval)))

    def age_seconds(self, instrument, interval, now=None):
        ts = self.timestamp(instrument, interval)
        if ts is None:
            return None
        return ((now or datetime.now()) - ts).total_seconds()


# --------------------------------------------------------------------------- #
# Candle poller
# --------------------------------------------------------------------------- #
class CandlePoller:
    """Polls one candle interval for all subscribed instruments.

    fetch_fn(instrument, segment, interval_min) -> DataFrame.
    """

    def __init__(self, interval_min: int, fetch_fn, cache: CandleCache, name=None,
                 bulk_fetch_fn=None):
        self.interval_min = int(interval_min)
        self.fetch_fn = fetch_fn
        self.cache = cache
        self.bulk_fetch_fn = bulk_fetch_fn
        self.name = name or f"poll-{interval_min}m"
        self._subs: dict = {}            # instrument -> {"segment": str, "who": set}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_boundary = None       # last interval boundary we ticked
        self._thread = None

    # ---- subscription -----------------------------------------------------
    def subscribe(self, instrument, who, segment):
        instrument = str(instrument)
        with self._lock:
            entry = self._subs.setdefault(instrument, {"segment": segment, "who": set()})
            entry["segment"] = segment or entry["segment"]
            entry["who"].add(who)
        logger.debug("%s: subscribe %s by %s", self.name, instrument, who)

    def unsubscribe(self, instrument, who):
        instrument = str(instrument)
        with self._lock:
            entry = self._subs.get(instrument)
            if not entry:
                return
            entry["who"].discard(who)
            if not entry["who"]:
                self._subs.pop(instrument, None)
        logger.debug("%s: unsubscribe %s by %s", self.name, instrument, who)

    def instruments(self):
        with self._lock:
            return list(self._subs.keys())

    def subscriber_count(self, instrument):
        with self._lock:
            e = self._subs.get(str(instrument))
            return len(e["who"]) if e else 0

    # ---- ticking ----------------------------------------------------------
    def _boundary(self, now: datetime):
        """The interval boundary for `now` (minute floored to interval)."""
        floored_min = (now.minute // self.interval_min) * self.interval_min
        return now.replace(minute=floored_min, second=0, microsecond=0)

    def due(self, now: datetime, settle_seconds=30):
        """True once per boundary, a few seconds after the candle has closed."""
        b = self._boundary(now)
        if self._last_boundary == b:
            return False
        return (now - b).total_seconds() >= settle_seconds

    def tick(self, now=None):
        """Fetch the latest candle for every subscribed instrument once.

        If a bulk fetcher is configured, all subscribed instruments are pulled
        in a single (batched) call; otherwise fall back to one fetch per
        instrument via fetch_fn.
        """
        now = now or datetime.now()
        with self._lock:
            targets = [(i, e["segment"]) for i, e in self._subs.items()]
        fetched = 0
        if self.bulk_fetch_fn and targets:
            # Single bulk call for all instruments.
            try:
                results = self.bulk_fetch_fn(targets, self.interval_min)
            except Exception:
                logger.exception("%s: bulk fetch failed", self.name)
                results = {}
            for instrument, df in (results or {}).items():
                if df is not None:
                    self.cache.put(instrument, self.interval_min, df, ts=now)
                    fetched += 1
        else:
            # Fallback: one-by-one (existing logic).
            for instrument, segment in targets:
                try:
                    df = self.fetch_fn(instrument, segment, self.interval_min)
                    if df is not None:
                        self.cache.put(instrument, self.interval_min, df, ts=now)
                        fetched += 1
                except Exception:
                    logger.exception("%s: fetch failed for %s", self.name, instrument)
        self._last_boundary = self._boundary(now)
        if fetched:
            logger.info("%s: refreshed %d instrument(s)", self.name, fetched)
        return fetched

    # ---- thread lifecycle -------------------------------------------------
    def run(self, poll_seconds=10):
        logger.info("%s started (interval=%dm)", self.name, self.interval_min)
        while not self._stop.is_set():
            now = datetime.now()
            if self.instruments() and self.due(now):
                self.tick(now)
            self._stop.wait(poll_seconds)

    def start(self, poll_seconds=10):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run, args=(poll_seconds,),
                                        name=self.name, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


# --------------------------------------------------------------------------- #
# DataProvider
# --------------------------------------------------------------------------- #
class DataProvider:
    """Single read API + poller owner. Strategies subscribe instruments and read
    candles through here instead of calling the broker directly."""

    def __init__(self, scanner=None, fetch_intraday=None, fetch_daily=None,
                 cache=None, start_pollers=False):
        self._scanner = scanner
        self._fetch_intraday = fetch_intraday or self._default_intraday
        self._fetch_daily = fetch_daily or self._default_daily
        self.cache = cache or CandleCache()
        self.poll_5m = CandlePoller(5, self._fetch_intraday, self.cache,
                                    bulk_fetch_fn=self._bulk_intraday)
        self.poll_15m = CandlePoller(15, self._fetch_intraday, self.cache,
                                     bulk_fetch_fn=self._bulk_intraday)
        self._pollers = {5: self.poll_5m, 15: self.poll_15m}
        if start_pollers:
            self.start()

    # ---- default fetchers (reuse the tested momentum candle code) ---------
    def _momentum_fetchers(self):
        if getattr(self, "_mom", None) is None:
            from momentum_strategy import MomentumScanner, MomentumRegimeFilter
            if self._scanner is None:
                raise RuntimeError("DataProvider needs a scanner to fetch candles")
            self._mom = MomentumScanner(self._scanner)
            self._regime = MomentumRegimeFilter(self._scanner)
        return self._mom, self._regime

    def _default_intraday(self, instrument, segment, interval_min):
        mom, _ = self._momentum_fetchers()
        return mom.get_intraday_candles(instrument, segment, interval_minutes=interval_min)

    def _default_daily(self, instrument, segment, interval_min=None):
        _, regime = self._momentum_fetchers()
        return regime.get_daily_candles(instrument, segment)

    # ---- bulk intraday OHLC (Upstox bulk market-quote API) ----------------
    def _bulk_intraday(self, targets, interval_min):
        """
        Calls the Upstox bulk OHLC API for all targets in batches of 500.
        targets = [(instrument_key, segment), ...]
        Returns {instrument_key: DataFrame}.

        Best-effort: any batch that errors or returns non-200 is logged and
        skipped, leaving those instruments to the per-instrument direct-fetch
        fallback used by the read methods.
        """
        import requests
        import pandas as pd

        results = {}
        # Token source: scanner attribute if it carries one, else the shared
        # Upstox token file (the scanner's own .access_token is typically None,
        # the live token lives in the adapter / token store).
        token = getattr(self._scanner, "access_token", None)
        if not token:
            try:
                from upstox_token_manager import load_upstox_token
                token = load_upstox_token()
            except Exception:
                logger.exception("Bulk OHLC: no Upstox token available")
                return results

        batch_size = 500
        interval_map = {5: "5minute", 15: "15minute", 1: "1minute"}
        interval_str = interval_map.get(interval_min, f"{interval_min}minute")

        for i in range(0, len(targets), batch_size):
            batch = targets[i:i + batch_size]
            keys = ",".join(instr for instr, _ in batch)
            url = "https://api.upstox.com/v2/market-quote/ohlc"
            try:
                resp = requests.get(
                    url,
                    params={"instrument_key": keys, "interval": interval_str},
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/json"},
                    timeout=10,
                )
            except Exception:
                logger.exception("Bulk OHLC request error")
                continue
            if resp.status_code != 200:
                logger.warning("Bulk OHLC failed: %s", resp.text)
                continue
            data = resp.json().get("data", {})
            for key, quote in data.items():
                # Convert the OHLC snapshot to a single-row DataFrame matching
                # the existing candle DataFrame format.
                ohlc = quote.get("ohlc", {})
                df = pd.DataFrame([{
                    "open":   ohlc.get("open"),
                    "high":   ohlc.get("high"),
                    "low":    ohlc.get("low"),
                    "close":  ohlc.get("close"),
                    "volume": quote.get("volume", 0),
                    "ltp":    quote.get("last_price"),
                }])
                # API returns keys like "NSE_EQ:SYMBOL"; normalize to the
                # subscription "NSE_EQ|SYMBOL" format.
                results[key.replace(":", "|")] = df
        return results

    # ---- subscription API -------------------------------------------------
    def subscribe(self, instrument, who, interval, segment):
        self._pollers[int(interval)].subscribe(instrument, who, segment)

    def unsubscribe(self, instrument, who, interval):
        self._pollers[int(interval)].unsubscribe(instrument, who)

    def move(self, instrument, who, from_interval, to_interval, segment):
        """Move an instrument between pollers (e.g. B&B 15m breakout -> 5m retest)."""
        self._pollers[int(from_interval)].unsubscribe(instrument, who)
        self._pollers[int(to_interval)].subscribe(instrument, who, segment)

    # ---- reads (cache-first, fall back to a direct fetch on miss) ---------
    def intraday_candles(self, instrument, segment, interval=15, max_age=None):
        df = self.cache.get(instrument, interval)
        if df is not None:
            if max_age is None:
                return df
            age = self.cache.age_seconds(instrument, interval)
            if age is not None and age <= max_age:
                return df
        df = self._fetch_intraday(instrument, segment, interval)
        if df is not None:
            self.cache.put(instrument, interval, df)
        return df

    def daily_candles(self, instrument, segment, max_age=None):
        df = self.cache.get(instrument, 1440)
        if df is not None and (max_age is None or (self.cache.age_seconds(instrument, 1440) or 1e9) <= max_age):
            return df
        df = self._fetch_daily(instrument, segment)
        if df is not None:
            self.cache.put(instrument, 1440, df)
        return df

    # ---- lifecycle --------------------------------------------------------
    def start(self):
        self.poll_5m.start()
        self.poll_15m.start()

    def stop(self):
        self.poll_5m.stop()
        self.poll_15m.stop()
