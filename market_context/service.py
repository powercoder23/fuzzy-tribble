# -*- coding: utf-8 -*-
"""
market_context/service.py — the market-context daemon.

Owns the platform's only WebSocket, and is the sole writer of the mc_* tables.

    python -m market_context.service

PHASE 1 — OBSERVATIONAL ONLY (operator decision 2026-08-03). This process does
not veto entries, change exits, stop-losses, targets or sizing. It collects and
persists so the effect of market context on expectancy can be MEASURED before
it is ever allowed to influence a decision.

LOGGING IS SET UP DELIBERATELY, NOT BY HABIT
--------------------------------------------
Two real logging defects in this platform informed this:

  * `logs/scheduler.log` has been 0 bytes since 2026-03-31 while the discount
    service booked 356 trades — the single largest trade source produces no
    persistent log, and nothing noticed.
  * `logs/directional_iv.log` is 24 MB and full of DISCOUNT output, because
    directional_iv_runner.py calls logging.basicConfig() on the ROOT logger
    and discount.py's records propagate into it.

So this service (a) configures a NAMED logger, never the root, (b) verifies at
startup that its log file is actually writable and alerts if not, and (c)
emits a periodic heartbeat so a silent death is visible.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from market_context import config as cfg
from market_context import store
from market_context.collect import snapshots
from market_context.feed import probe, subscription
from market_context.feed.cache import TickCache
from market_context.feed.client import UpstoxFeedClient, market_is_open
from market_context.regime.engine import RegimeEngine

LOG_DIR = Path(os.getenv("LOGS_DIR", "logs"))
LOG_FILE = LOG_DIR / "market_context.log"

logger = logging.getLogger("market_context")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging() -> bool:
    """Configure the NAMED 'market_context' logger. Returns log-file writability.

    propagate=False keeps our records out of any root handler another module
    may have installed, and keeps other modules' records out of our file.
    """
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    writable = False
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=20 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        handler.emit(logging.LogRecord(
            "market_context", logging.INFO, __file__, 0,
            "log file writability check", None, None))
        handler.flush()
        writable = LOG_FILE.exists() and LOG_FILE.stat().st_size > 0
    except Exception:
        logger.exception("could not attach file handler at %s", LOG_FILE)

    if not writable:
        logger.error("LOG FILE NOT WRITABLE at %s — this service will run but "
                     "leave no persistent log", LOG_FILE)
        _alert(f"⚠️ <b>market-context</b>: log file not writable at "
               f"{LOG_FILE}. Service is running blind.",
               key="log_not_writable")
    return writable


#: Cooldown marker directory. Must survive process death — an in-process
#: dedup set is useless against a crash-loop, which is exactly when identical
#: alerts are generated (six in six minutes on 2026-08-04).
ALERT_STATE_DIR = LOG_DIR / ".market_context_alerts"
ALERT_COOLDOWN_SEC = float(os.getenv("MC_ALERT_COOLDOWN_SEC", "3600"))


def _alert(message: str, key: str | None = None,
           cooldown_sec: float | None = None) -> bool:
    """Send an alert, optionally rate-limited by `key` ACROSS RESTARTS.

    Returns True if it was sent. When `key` is given, a marker file records
    the last send; repeats inside the cooldown are suppressed. Without this a
    restart loop turns one problem into one alert per restart, which trains
    the reader to ignore the channel.
    """
    if key:
        try:
            ALERT_STATE_DIR.mkdir(parents=True, exist_ok=True)
            marker = ALERT_STATE_DIR / f"{key}.last"
            window = ALERT_COOLDOWN_SEC if cooldown_sec is None else cooldown_sec
            if marker.exists():
                age = time.time() - marker.stat().st_mtime
                if age < window:
                    logger.warning(
                        "alert '%s' suppressed (%.0fs into a %.0fs cooldown): %s",
                        key, age, window, message.replace("\n", " ")[:160])
                    return False
            marker.touch()
        except Exception:
            logger.debug("alert cooldown check failed; sending anyway",
                         exc_info=True)
    try:
        import notifications
        notifications.notify(message)
        return True
    except Exception:
        logger.debug("alert could not be sent", exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class MarketContextService:

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path
        self.cache = TickCache()
        self.plan = None
        self.feed: UpstoxFeedClient | None = None
        self.regime = RegimeEngine(db_path=db_path)
        self._last_regime: dict | None = None
        self._stop = threading.Event()
        self._plan_day: str | None = None
        self._last_snapshot = datetime.min
        self._last_flush = datetime.min
        self._last_heartbeat = datetime.min
        self._last_quote_flush = datetime.min
        self._pruned_day: str | None = None

    # ---- recovery --------------------------------------------------------- #
    def _on_quarantine(self, moved_path) -> None:
        """Report an automatic corruption recovery.

        Deliberately NOT rate-limited by the ordinary cooldown key alone: this
        is a data-loss event and the operator needs to know the session's
        history is gone. It is keyed per-day so a pathological filesystem
        cannot spam, but it will still fire once tomorrow if it recurs.
        """
        logger.error("market-context recovered from a corrupt database; "
                     "prior contents preserved at %s", moved_path)
        _alert(
            "⚠️ <b>market-context</b>: database was unusable and has "
            "been quarantined + recreated.\n"
            f"Old file kept at <code>{moved_path}</code>.\n"
            "Today's collected context up to this point is LOST. Service is "
            "running normally again.",
            key=f"db_quarantine_{datetime.now():%Y%m%d}",
        )

    # ---- plan ------------------------------------------------------------- #
    def build_plan(self, detected_connections: int | None = None) -> None:
        """(Re)build and persist the subscription plan.

        Rebuilt once per day because both inputs move: futures roll to a new
        expiry each month, and the liquidity ranking that picks tier 3/4
        changes daily. Membership is deliberately NOT recomputed intraday — a
        name entering the top-59 at 13:00 is not worth a resubscribe, and
        churn would make breadth non-comparable across the session.

        The connection allowance is MEASURED, not assumed (see feed/probe.py),
        so the same image sizes itself on Standard and on Plus.
        """
        if detected_connections is None:
            detected_connections = probe.detect_connections(self.db_path)
        self.plan = subscription.build_plan(detected_connections)
        written = subscription.persist(self.plan, self.db_path)
        self._plan_day = datetime.now().date().isoformat()
        logger.info("subscription plan built: %s (%d rows persisted)",
                    self.plan.summary(), written)
        if self.plan.budget.breadth_is_subsample:
            logger.info("breadth will be a %d-name PARTIAL-UNIVERSE subsample, not "
                        "full market breadth — recorded as is_subsample=1 and "
                        "capped at confidence %.2f",
                        self.plan.budget.tier3, cfg.CONF_SUBSAMPLE_CEILING)

    def _maybe_rebuild_plan(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self._plan_day == today:
            return
        logger.info("new session — rebuilding subscription plan")
        was_running = self.feed is not None
        if was_running:
            self.feed.stop()
        self.build_plan()
        if was_running:
            self._start_feed()

    # ---- feed ------------------------------------------------------------- #
    def _start_feed(self) -> None:
        self.feed = UpstoxFeedClient(self.plan, self.cache, db_path=self.db_path)
        self.feed.start()

    # ---- periodic work ---------------------------------------------------- #
    def _tick(self, now: datetime) -> None:
        if (now - self._last_flush).total_seconds() >= cfg.BAR_FLUSH_INTERVAL_SEC:
            self._last_flush = now
            written = self.cache.flush_completed_bars(now, self.db_path)
            if written:
                logger.debug("flushed %d bar(s)", written)

        # Honour dynamic subscribe/unsubscribe requests from other containers
        # (order_manager, strategies) — see store.request_subscription.
        if self.feed is not None:
            try:
                self.feed.reconcile()
            except Exception:
                logger.exception("subscription reconcile failed (non-fatal)")

        if (now - self._last_quote_flush).total_seconds() >= cfg.LIVE_QUOTE_FLUSH_INTERVAL_SEC:
            self._last_quote_flush = now
            self._flush_live_quotes(now)

        if (now - self._last_snapshot).total_seconds() >= cfg.SNAPSHOT_INTERVAL_SEC:
            self._last_snapshot = now
            # Order matters: the collectors write mc_vix / mc_futures /
            # mc_breadth / mc_sector, and the feature builder reads them back.
            try:
                snapshots.collect_all(self.cache, self.plan, now, self.db_path)
            except Exception:
                logger.exception("snapshot collection failed (non-fatal)")
            try:
                result = self.regime.run(cache=self.cache, now=now)
                if result:
                    self._last_regime = result
                    if result.get("transitioned"):
                        logger.info("REGIME TRANSITION | %s",
                                    self.regime.summary(result))
            except Exception:
                logger.exception("regime classification failed (non-fatal)")

        if (now - self._last_heartbeat).total_seconds() >= cfg.HEARTBEAT_LOG_MINUTES * 60:
            self._last_heartbeat = now
            self._heartbeat()

        today = now.date().isoformat()
        if not market_is_open(now) and self._pruned_day != today and now.hour >= 16:
            self._pruned_day = today
            deleted = store.prune(self.db_path)
            logger.info("retention prune: %s", deleted)

    def _flush_live_quotes(self, now: datetime) -> None:
        """Persist the latest tick for every DYNAMICALLY-subscribed instrument
        (open positions etc.) into mc_live_quotes — the fast-monitoring path
        order_manager/paper_trader read via market_context.get_quote(),
        instead of the old 5-minute REST poll. Static regime-plan instruments
        already have mc_bars_1m/snapshots and don't need this. Non-fatal."""
        try:
            dynamic_keys = store.active_subscriptions(self.db_path)
            if not dynamic_keys:
                return
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            rows = []
            for key in dynamic_keys:
                tick = self.cache.last(key)
                if tick is not None and tick.ltp is not None:
                    rows.append((key, tick.ltp, ts))
            if rows:
                store.upsert_live_quotes(rows, self.db_path)
        except Exception:
            logger.exception("live-quote flush failed (non-fatal)")

    def _heartbeat(self) -> None:
        """Periodic proof-of-life. A service that dies silently is the failure
        mode this platform has already suffered twice."""
        status = self.feed.status() if self.feed else {"state": "NOT_STARTED"}
        vix = self.cache.last("NSE_INDEX|India VIX")
        logger.info(
            "heartbeat | state=%s instruments=%s frames=%s reconnects=%s "
            "in_gap=%s vix=%s mode=%s influences_trading=%s",
            status.get("state"), status.get("instruments"), status.get("frames"),
            status.get("reconnects"), status.get("in_gap"),
            f"{vix.ltp:.2f}" if vix and vix.ltp else "n/a",
            cfg.effective_mode(), cfg.influences_trading(),
        )
        if self._last_regime:
            logger.info("heartbeat | %s", self.regime.summary(self._last_regime))

    # ---- lifecycle -------------------------------------------------------- #
    def run(self) -> None:
        logger.info("market-context starting | mode=%s | plan_tier=%s | db=%s",
                    cfg.effective_mode(), cfg.PLAN_TIER, cfg.DB_PATH)
        logger.info("PHASE 1: observational only — influences_trading=%s",
                    cfg.influences_trading())

        if cfg.effective_mode() == "off":
            logger.warning("MC_MODE=off — service will idle without collecting")

        store.init_db(self.db_path, on_quarantine=self._on_quarantine)
        self.build_plan()

        if self.plan.total == 0:
            logger.error("subscription plan is empty — check data/complete.db. "
                         "Service will idle and retry at the next session.")
        elif cfg.effective_mode() != "off":
            self._start_feed()

        while not self._stop.is_set():
            now = datetime.now()
            try:
                self._maybe_rebuild_plan(now)
                if cfg.effective_mode() != "off":
                    self._tick(now)
            except Exception:
                logger.exception("service loop iteration failed (continuing)")
            self._stop.wait(5)

        self.shutdown()

    def shutdown(self) -> None:
        logger.info("market-context shutting down")
        if self.feed:
            self.feed.stop()
        try:
            written = self.cache.flush_completed_bars(
                datetime.now() + timedelta(minutes=1), self.db_path)
            if written:
                logger.info("flushed %d final bar(s)", written)
        except Exception:
            logger.exception("final bar flush failed")

    def request_stop(self, *_args) -> None:
        self._stop.set()


def main() -> int:
    setup_logging()
    service = MarketContextService()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, service.request_stop)
        except (ValueError, OSError):
            pass        # not the main thread, or unsupported on this platform
    try:
        service.run()
    except KeyboardInterrupt:
        service.request_stop()
        service.shutdown()
    except Exception:
        logger.exception("market-context died unexpectedly")
        # Rate-limited by key: `restart: unless-stopped` means a fatal error
        # produces one alert PER RESTART otherwise, which is how a single
        # corrupt file became six identical alerts in six minutes.
        _alert("\U0001F6D1 <b>market-context</b> died unexpectedly — see "
               "logs/market_context.log",
               key="fatal")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
