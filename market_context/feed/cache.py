# -*- coding: utf-8 -*-
"""
market_context/feed/cache.py — in-memory last-value cache + 1-minute bar
aggregation.

Two layers, both bounded:

  L1  TickCache        {instrument_key: Tick}   O(1) current state.
                       ~100 keys x a few hundred bytes. Never grows.
  L2  BarAggregator    the OPEN 1-minute bar per instrument, flushed on the
                       minute boundary into mc_bars_1m.

NEVER WRITE PER TICK. At ~100 instruments ticking several times a second,
per-tick persistence would be thousands of rows/second and would destroy
SQLite. Ingest is event-driven; persistence is wall-clock.

THE CUMULATIVE-VOLUME TRAP
--------------------------
Upstox `vtt` is volume-traded-TODAY, cumulative. A bar's volume is therefore a
DELTA (vtt_end - vtt_start), which means the first bar after a connect or a
reconnect has no known baseline. That bar's volume is written as NULL, never
as 0.

This matters more than it looks: volume breadth is up-volume / total-volume,
so a false zero would silently understate participation on exactly the bars
following an outage — biasing the data in the direction of "quiet market"
precisely when something went wrong.
"""

from __future__ import annotations

import logging
from contextlib import closing
import threading
from dataclasses import dataclass, field
from datetime import datetime

from market_context import store
from market_context.feed.normaliser import Tick

logger = logging.getLogger(__name__)


def floor_minute(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:00")


@dataclass
class _OpenBar:
    """One in-progress 1-minute bar."""

    instrument_key: str
    ts: str
    open: float
    high: float
    low: float
    close: float
    tick_count: int = 0
    vwap: float | None = None
    oi: float | None = None
    oi_chg: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None
    volume_start: float | None = None      # vtt at bar open
    volume_end: float | None = None        # vtt at last tick
    baseline_known: bool = False           # False => volume delta unknowable

    def update(self, tick: Tick) -> None:
        price = tick.ltp
        if price is not None:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
            self.close = price
        self.tick_count += 1
        for attr in ("vwap", "oi", "bid", "ask", "bid_qty", "ask_qty"):
            value = getattr(tick, attr)
            if value is not None:
                setattr(self, attr, value)
        if tick.oi_chg_pct is not None:
            self.oi_chg = tick.oi_chg_pct
        if tick.volume_today is not None:
            # volume_start is seeded by the CACHE from the previous bar's last
            # vtt, never from this bar's own first tick — see TickCache.update.
            self.volume_end = tick.volume_today

    @property
    def volume(self) -> float | None:
        """Bar volume, or None when the baseline is unknown.

        Also returns None on a negative delta — that means `vtt` reset (a new
        session, or a bad frame), and a negative volume is worse than a gap.
        """
        if not self.baseline_known:
            return None
        if self.volume_start is None or self.volume_end is None:
            return None
        delta = self.volume_end - self.volume_start
        return delta if delta >= 0 else None

    def as_row(self) -> tuple:
        return (self.instrument_key, self.ts, self.open, self.high, self.low,
                self.close, self.volume, self.oi, self.oi_chg, self.vwap,
                self.bid, self.ask, self.bid_qty, self.ask_qty, self.tick_count)


class TickCache:
    """Last value per instrument + the open 1-minute bar per instrument."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last: dict[str, Tick] = {}
        self._bars: dict[str, _OpenBar] = {}
        #: Bars whose minute has rolled over but which have not been persisted
        #: yet. Without this, a bar opened at 10:00 was DISCARDED the moment a
        #: 10:01 tick arrived, so every bar that rolled over between two flush
        #: ticks was lost — and bars and flushes are both on a 60s cadence.
        self._completed: list[_OpenBar] = []
        #: Last cumulative vtt per instrument. This is the baseline for the
        #: NEXT bar's volume delta. Cleared on disconnect, which is what makes
        #: the first post-gap bar write NULL volume instead of a delta
        #: spanning the outage.
        self._last_vtt: dict[str, float] = {}
        #: Arrival time of the last REAL socket frame per instrument. Written
        #: only by update(), never by seed(): a REST-resynced value must NOT
        #: make a dead socket look alive to the staleness watchdog.
        self._last_wire_at: dict[str, datetime] = {}
        self._last_frame_at: datetime | None = None
        self._frames = 0

    # ---- ingest ----------------------------------------------------------- #
    def update(self, tick: Tick) -> None:
        key = tick.instrument_key
        with self._lock:
            self._last[key] = tick
            self._last_wire_at[key] = tick.received_at
            self._last_frame_at = tick.received_at
            self._frames += 1

            ts = floor_minute(tick.received_at)
            bar = self._bars.get(key)
            if bar is None or bar.ts != ts:
                if tick.ltp is None:
                    return          # cannot open a bar without a price
                if bar is not None:
                    self._completed.append(bar)     # roll off, never drop
                # Baseline is the PREVIOUS bar's last vtt, not this bar's
                # first tick: the volume traded between the previous tick and
                # this bar's open belongs to this bar.
                prev_vtt = self._last_vtt.get(key)
                bar = _OpenBar(
                    instrument_key=key, ts=ts,
                    open=tick.ltp, high=tick.ltp, low=tick.ltp, close=tick.ltp,
                    volume_start=prev_vtt,
                    baseline_known=prev_vtt is not None,
                )
                self._bars[key] = bar
            bar.update(tick)
            if tick.volume_today is not None:
                self._last_vtt[key] = tick.volume_today

    def seed(self, tick: Tick) -> None:
        """Install a REST-recovered value without treating it as a live frame.

        Used by the resync path after an outage. It restores two things the
        socket alone cannot rebuild:

          * the day-cumulative state (vtt / OI / day OHLC), and
          * the volume BASELINE, so the next bar's delta is computable again
            instead of NULL for the rest of the session.

        It deliberately does NOT touch _last_wire_at, _last_frame_at or the
        frame counter. Seeding must never make a dead socket look alive — the
        watchdog has to keep reconnecting until real frames resume.
        """
        with self._lock:
            self._last[tick.instrument_key] = tick
            if tick.volume_today is not None:
                self._last_vtt[tick.instrument_key] = tick.volume_today

    def mark_disconnected(self) -> None:
        """Drop volume baselines so the first bar after reconnect writes NULL
        volume instead of a delta spanning the outage.

        A successful resync re-seeds them; without one they stay cleared and
        the affected bars honestly record NULL.
        """
        with self._lock:
            self._last_vtt.clear()

    # ---- reads ------------------------------------------------------------ #
    def last(self, instrument_key: str) -> Tick | None:
        return self._last.get(instrument_key)

    def snapshot(self) -> dict[str, Tick]:
        with self._lock:
            return dict(self._last)

    @property
    def last_frame_at(self) -> datetime | None:
        return self._last_frame_at

    @property
    def frames(self) -> int:
        return self._frames

    def seconds_since_frame(self, instrument_key: str,
                            now: datetime | None = None) -> float | None:
        """Age of the last REAL socket frame. Ignores REST-seeded values."""
        seen = self._last_wire_at.get(instrument_key)
        if seen is None:
            return None
        return ((now or datetime.now()) - seen).total_seconds()

    def stale_tier1(self, keys, timeout_sec: float,
                    now: datetime | None = None) -> bool:
        """True when EVERY tier-1 instrument has gone silent past the timeout.

        Judged on tier 1 only, and requires ALL of them: one index pausing is
        normal, all of them pausing during market hours is a dead socket. A
        never-seen instrument does not count as stale — that is a startup
        state, handled by the connect path.
        """
        if not keys:
            return False
        now = now or datetime.now()
        seen_any = False
        for key in keys:
            age = self.seconds_since_frame(key, now)
            if age is None:
                continue
            seen_any = True
            if age <= timeout_sec:
                return False
        return seen_any

    # ---- flush ------------------------------------------------------------ #
    def flush_completed_bars(self, now: datetime | None = None,
                             db_path: str | None = None) -> int:
        """Persist every bar whose minute has closed. Returns rows written.

        One batched transaction per flush. INSERT OR IGNORE on
        (instrument_key, ts) makes it idempotent: a restart mid-minute cannot
        double-write.
        """
        now = now or datetime.now()
        current = floor_minute(now)
        with self._lock:
            # Bars already rolled off by a newer tick, PLUS any still-open bar
            # whose minute has passed (an instrument that stopped ticking).
            done = self._completed
            self._completed = []
            for key, bar in list(self._bars.items()):
                if bar.ts != current:
                    done.append(bar)
                    self._bars.pop(key, None)
        if not done:
            return 0

        rows = [bar.as_row() for bar in done]
        try:
            with closing(store.connect(db_path)) as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO mc_bars_1m "
                    "(instrument_key, ts, open, high, low, close, volume, oi,"
                    " oi_chg, vwap, bid, ask, bid_qty, ask_qty, tick_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()
        except Exception:
            logger.exception("bar flush failed (%d rows dropped)", len(rows))
            return 0
        return len(rows)
