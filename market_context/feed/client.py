# -*- coding: utf-8 -*-
"""
market_context/feed/client.py — the platform's ONLY WebSocket connection.

Owns connect / authorise / subscribe / reconnect / staleness detection, and
records every outage into mc_feed_gaps.

WHY WE OWN THE RECONNECT LOOP INSTEAD OF SDK auto_reconnect()
-------------------------------------------------------------
`MarketDataStreamerV3.auto_reconnect(enable, interval, retryCount)` exists and
is deliberately DISABLED here:

  1. It cannot re-run REST re-authorisation. The WSS URL is short-lived and
     this platform's access token rotates daily (upstox_token_manager). A
     reconnect that reuses a dead URL retries forever against a 401.
  2. It gives no hook to record a feed gap. An outage that leaves no trace
     looks, in the data, like a period of perfectly stable market context.
  3. It cannot trigger the REST resync that rebuilds day-cumulative state.

STALENESS BEATS PING/PONG
-------------------------
A dead feed almost always presents as a live TCP connection that has stopped
delivering. Waiting for a socket error can mean ten blind minutes. The
watchdog therefore reconnects on SILENCE, judged on tier-1 instruments only —
NIFTY/BANKNIFTY/VIX always tick during market hours, whereas a tier-3 mid-cap
going quiet for a minute is data, not an outage.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, time as dtime

from market_context import config as cfg
from market_context import store
from market_context.feed import normaliser
from market_context.feed.cache import TickCache
from market_context.feed.subscription import SubscriptionPlan

logger = logging.getLogger(__name__)

STATE_DISCONNECTED = "DISCONNECTED"
STATE_CONNECTING = "CONNECTING"
STATE_STREAMING = "STREAMING"
STATE_BACKOFF = "BACKING_OFF"
STATE_STOPPED = "STOPPED"


def _parse_hhmm(text: str, fallback: dtime) -> dtime:
    try:
        hour, minute = str(text).split(":")
        return dtime(int(hour), int(minute))
    except (TypeError, ValueError):
        return fallback


def market_is_open(now: datetime | None = None) -> bool:
    """IST session window, weekdays only.

    Outside it the client idles rather than reconnect-looping: pointless token
    pressure and overnight log spam otherwise. NSE holidays are NOT known to
    this platform (discount_config.NSE_HOLIDAYS is empty), so a holiday looks
    like a silent trading day — the gap register will show it as one long
    outage, which is honest and harmless.
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    open_t = _parse_hhmm(cfg.MARKET_OPEN, dtime(9, 0))
    close_t = _parse_hhmm(cfg.MARKET_CLOSE, dtime(15, 45))
    return open_t <= now.time() <= close_t


class UpstoxFeedClient:
    """Single-socket market data client.

    Fail-soft everywhere: if the SDK is unavailable or the socket cannot be
    established, the client logs, records a gap, and keeps retrying. It never
    raises into the service loop, and the service keeps producing (empty)
    snapshots so downstream get() behaviour is unchanged.
    """

    def __init__(self, plan: SubscriptionPlan, cache: TickCache,
                 db_path: str | None = None, on_tick=None, resyncer=None):
        self.plan = plan
        self.cache = cache
        self.db_path = db_path
        self.on_tick = on_tick
        self._resync = resyncer          # injectable for tests

        self.state = STATE_DISCONNECTED
        self._streamer = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._reconnects = 0
        self._gap_id: int | None = None
        self._gap_started: datetime | None = None
        self._force_reconnect = threading.Event()
        #: instrument_key -> mode currently carried by the socket.
        self._subscribed: dict[str, str] = {}

    # ---- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, name="mc-feed",
                                        daemon=True)
        self._thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog,
                                                 name="mc-watchdog", daemon=True)
        self._watchdog_thread.start()
        logger.info("market_context feed: started (%d instruments)", self.plan.total)

    def stop(self) -> None:
        self._stop.set()
        self._force_reconnect.set()
        self._disconnect_streamer()
        self._close_gap(resync=False)
        self.state = STATE_STOPPED
        logger.info("market_context feed: stopped after %d reconnect(s)", self._reconnects)

    # ---- main loop -------------------------------------------------------- #
    def _run_forever(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if not market_is_open():
                self.state = STATE_DISCONNECTED
                self._disconnect_streamer()
                self._stop.wait(30)
                attempt = 0
                continue
            try:
                self.state = STATE_CONNECTING
                if self._connect_once():
                    attempt = 0
                    self._close_gap(resync=True)
                    self.state = STATE_STREAMING
                    # Block until the watchdog or an error asks for a reconnect.
                    self._force_reconnect.wait()
                    self._force_reconnect.clear()
            except Exception:
                logger.exception("market_context feed: connect cycle failed")

            if self._stop.is_set():
                break
            self._disconnect_streamer()
            self.cache.mark_disconnected()
            self._open_gap("DISCONNECT")
            attempt += 1
            self._reconnects += 1
            delay = self._backoff(attempt)
            self.state = STATE_BACKOFF
            logger.warning("market_context feed: reconnecting in %.1fs (attempt %d)",
                           delay, attempt)
            self._stop.wait(delay)

    def _backoff(self, attempt: int) -> float:
        """Exponential with jitter. Jitter matters once there are multiple
        connections (Plus plan): it prevents synchronised reconnect storms."""
        base = cfg.RECONNECT_BASE_SEC * (cfg.RECONNECT_MULTIPLIER ** max(attempt - 1, 0))
        delay = min(base, cfg.RECONNECT_MAX_SEC)
        jitter = delay * cfg.RECONNECT_JITTER_PCT
        return max(0.5, delay + random.uniform(-jitter, jitter))

    # ---- connection ------------------------------------------------------- #
    def _access_token(self) -> str | None:
        try:
            from upstox_token_manager import load_upstox_token
            return load_upstox_token()
        except Exception:
            logger.exception("market_context feed: could not load Upstox token")
            return None

    def desired_state(self) -> dict:
        """{mode: [keys]} = the static plan PLUS live dynamic requests.

        This is the single definition of "what should the socket be carrying",
        and it is replayed wholesale on every reconnect. Dynamic requests from
        other containers (order_manager, strategies) therefore survive a
        reconnect automatically — they are part of desired state, not an
        incremental patch applied after connect.
        """
        merged = dict(self.plan.keys_by_mode())
        static = {k for keys in merged.values() for k in keys}
        for key, mode in store.active_subscriptions(self.db_path).items():
            if key in static:
                continue                     # already carried by the plan
            merged.setdefault(mode, []).append(key)
        return {m: k for m, k in merged.items() if k}

    def reconcile(self) -> tuple[int, int]:
        """Diff the socket against desired state; subscribe/unsubscribe deltas.

        Called on the service tick, so another container's request takes effect
        within one cadence without anyone holding a reference to this object.
        Returns (added, removed).
        """
        if self._streamer is None or self.state != STATE_STREAMING:
            return 0, 0
        desired = self.desired_state()
        want = {k: m for m, keys in desired.items() for k in keys}
        have = dict(self._subscribed)

        added = removed = 0
        by_mode: dict = {}
        for key, mode in want.items():
            if have.get(key) != mode:
                by_mode.setdefault(mode, []).append(key)
        for mode, keys in by_mode.items():
            try:
                self._streamer.subscribe(list(keys), mode)
                for key in keys:
                    self._subscribed[key] = mode
                added += len(keys)
            except Exception:
                logger.exception("reconcile: subscribe(%s, %d) failed", mode, len(keys))

        stale = [k for k in have if k not in want]
        if stale:
            try:
                self._streamer.unsubscribe(list(stale))
                for key in stale:
                    self._subscribed.pop(key, None)
                removed = len(stale)
            except Exception:
                logger.exception("reconcile: unsubscribe(%d) failed", len(stale))

        if added or removed:
            logger.info("feed reconcile: +%d / -%d (now %d instruments)",
                        added, removed, len(self._subscribed))
        return added, removed

    def _connect_once(self) -> bool:
        """Authorise, open the socket, and subscribe the FULL desired state."""
        token = self._access_token()
        if not token:
            return False

        keys_by_mode = self.desired_state()
        if not keys_by_mode:
            logger.error("market_context feed: empty subscription plan — not connecting")
            return False

        try:
            import upstox_client
        except Exception:
            logger.exception(
                "market_context feed: upstox_client unavailable. The platform has a "
                "recorded history of the Upstox SDK hanging on REST; if the streamer "
                "is also unusable, replace this with a raw `websockets` + protobuf "
                "client. Feed disabled until then.")
            return False

        try:
            configuration = upstox_client.Configuration()
            configuration.access_token = token
            api_client = upstox_client.ApiClient(configuration)

            # Instantiate with the first mode's keys, then add the rest via
            # subscribe(). The SDK requires an initial mode at construction.
            first_mode, first_keys = next(iter(keys_by_mode.items()))
            streamer = upstox_client.MarketDataStreamerV3(
                api_client, list(first_keys), first_mode)

            # Explicitly OFF — see module docstring.
            try:
                streamer.auto_reconnect(False, 0, 0)
            except Exception:
                logger.debug("auto_reconnect toggle unavailable on this SDK build")

            streamer.on("message", self._on_message)
            streamer.on("error", self._on_error)
            streamer.on("close", self._on_close)

            self._streamer = streamer
            self._subscribed = {k: first_mode for k in first_keys}
            streamer.connect()

            # Declarative desired state: replay EVERY mode group on every
            # connect, never an incremental diff.
            for mode, keys in keys_by_mode.items():
                if mode == first_mode:
                    continue
                try:
                    streamer.subscribe(list(keys), mode)
                    self._subscribed.update({k: mode for k in keys})
                except Exception:
                    logger.exception("market_context feed: subscribe(%s, %d keys) failed",
                                     mode, len(keys))

            logger.info("market_context feed: connected — %s",
                        ", ".join(f"{m}:{len(k)}" for m, k in keys_by_mode.items()))
            return True
        except Exception:
            logger.exception("market_context feed: connect failed")
            self._streamer = None
            return False

    def _disconnect_streamer(self) -> None:
        streamer, self._streamer = self._streamer, None
        self._subscribed = {}
        if streamer is None:
            return
        try:
            streamer.disconnect()
        except Exception:
            logger.debug("market_context feed: disconnect raised (ignored)", exc_info=True)

    # ---- callbacks -------------------------------------------------------- #
    def _on_message(self, message) -> None:
        try:
            ticks = normaliser.normalise_message(message)
        except Exception:
            logger.debug("market_context feed: bad frame ignored", exc_info=True)
            return
        for tick in ticks:
            self.cache.update(tick)
            if self.on_tick is not None:
                try:
                    self.on_tick(tick)
                except Exception:
                    logger.debug("on_tick hook raised (ignored)", exc_info=True)

    def _on_error(self, error) -> None:
        logger.warning("market_context feed: socket error: %s", error)
        self._force_reconnect.set()

    def _on_close(self, *args) -> None:
        logger.warning("market_context feed: socket closed %s", args if args else "")
        self._force_reconnect.set()

    # ---- watchdog --------------------------------------------------------- #
    def _watchdog(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(cfg.WS_WATCHDOG_INTERVAL_SEC)
            if self._stop.is_set():
                break
            if self.state != STATE_STREAMING or not market_is_open():
                continue
            if self.cache.stale_tier1(self.plan.tier1_keys(),
                                      cfg.WS_STALE_TIMEOUT_SEC):
                logger.warning(
                    "market_context feed: STALE — no tier-1 frame for >%.0fs; "
                    "forcing reconnect", cfg.WS_STALE_TIMEOUT_SEC)
                self._open_gap("STALE")
                self._force_reconnect.set()

    # ---- gap register ----------------------------------------------------- #
    def _open_gap(self, reason: str) -> None:
        if self._gap_id is not None:
            return                      # already inside an open gap
        self._gap_started = datetime.now()
        self._gap_id = store.open_feed_gap(
            self._gap_started.strftime("%Y-%m-%d %H:%M:%S"), reason,
            instruments=self.plan.total, db_path=self.db_path)

    def _close_gap(self, resync: bool = True) -> None:
        """Close the open gap row and, for a long outage, run REST recovery.

        Recovery matters more than the missing ticks: the cache dropped its
        volume baselines on disconnect, so without a resync every remaining
        bar this session would write NULL volume.
        """
        if self._gap_id is None:
            return
        started = self._gap_started
        ended = datetime.now()
        duration = (ended - started).total_seconds() if started else 0.0

        resynced = False
        if resync and duration >= cfg.RESYNC_THRESHOLD_SEC:
            logger.warning("market_context feed: gap of %.0fs — running REST resync",
                           duration)
            try:
                outcome = self._resyncer().resync(duration, started, ended)
                resynced = not outcome.get("skipped") and (
                    outcome.get("quotes_seeded", 0) > 0
                    or outcome.get("bars_backfilled", 0) > 0)
            except Exception:
                logger.exception("market_context feed: resync raised (non-fatal)")

        store.close_feed_gap(self._gap_id, ended.strftime("%Y-%m-%d %H:%M:%S"),
                             duration, resynced=resynced, db_path=self.db_path)
        if duration >= cfg.RESYNC_THRESHOLD_SEC and not resynced:
            logger.warning("market_context feed: gap of %.0fs closed WITHOUT resync — "
                           "volume will be NULL until the next session", duration)
        self._gap_id = None
        self._gap_started = None

    def _resyncer(self):
        """Built lazily so importing this module never requires the SDK."""
        if self._resync is None:
            from market_context.feed.resync import Resyncer
            self._resync = Resyncer(self.plan, self.cache, db_path=self.db_path)
        return self._resync

    # ---- status ----------------------------------------------------------- #
    def status(self) -> dict:
        return {
            "state": self.state,
            "instruments": self.plan.total,
            "frames": self.cache.frames,
            "reconnects": self._reconnects,
            "last_frame_at": (self.cache.last_frame_at.isoformat()
                              if self.cache.last_frame_at else None),
            "in_gap": self._gap_id is not None,
        }
