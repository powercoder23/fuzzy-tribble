#!/usr/bin/env python3
"""
Strategy scheduler wrapper.

Runs the volatility-only discount scanner intraday. Every 5 minutes it:
  1. re-prices open paper trades and advances their exit state machine, then
  2. (until the entry cutoff) opens new paper trades from the top Volatility
     Expansion Play signals and fires per-signal Telegram alerts.
Forces square-off at INTRADAY["square_off"] (15:20) and sends an EOD paper-P&L
summary at INTRADAY["eod_summary_at"] (15:25). No live orders are placed.
"""

import logging
import os
import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import schedule

from config import Config
from discount import DiscountedPremiumScanner
from discount_config import INTRADAY
from directional_iv_runner import run_directional_scan
import paper_trader


APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")

# Force the process to use the configured timezone instead of the container default.
os.environ["TZ"] = APP_TIMEZONE
if hasattr(time, "tzset"):
    time.tzset()


def generate_interval_times(start, end, interval_minutes):
    """Return a list of "HH:MM" strings from start to end (inclusive) at the
    given interval. start/end are "HH:MM" strings."""
    start_dt = datetime.strptime(start, "%H:%M")
    end_dt = datetime.strptime(end, "%H:%M")
    times = []
    current = start_dt
    while current <= end_dt:
        times.append(current.strftime("%H:%M"))
        current += timedelta(minutes=interval_minutes)
    return times


# Intraday 5-min cadence from session start to just before square-off.
SCAN_TIMES = generate_interval_times(
    start=INTRADAY["session_start"],
    end="15:15",
    interval_minutes=INTRADAY["scan_interval_min"],
)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


Config.ensure_dirs()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(Config.LOGS_DIR / "scheduler.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


class StrategySchedulerApp:
    def __init__(self):
        # Persistent singletons reused across cycles (the scanner re-prices open
        # paper trades, so it must outlive a single scan).
        self._scanner = None
        self._book = None
        self._lot_fn = None

    # --- lazy singletons ----------------------------------------------------
    def scanner(self):
        if self._scanner is None:
            self._scanner = DiscountedPremiumScanner()
        return self._scanner

    def book(self):
        if self._book is None:
            self._book = paper_trader.PaperTradeBook()
        return self._book

    def lot_fn(self):
        if self._lot_fn is None:
            try:
                from momentum_strategy import ScripMasterLotSizer
                sizer = ScripMasterLotSizer()
                self._lot_fn = sizer.get
            except Exception:
                logger.exception("Lot sizer unavailable; paper P&L falls back to 1x lot")
                self._lot_fn = (lambda _s: 1)
        return self._lot_fn

    # --- jobs ---------------------------------------------------------------
    def run_discount_cycle(self):
        """5-min cycle: manage open paper trades, then (pre-cutoff) open new ones."""
        logger.info("%s", "=" * 70)
        logger.info("Discount cycle (monitor open trades + scan for new)")
        logger.info("%s", "=" * 70)
        try:
            scanner = self.scanner()
            book = self.book()
            now = datetime.now()

            # 1. Re-price and exit-manage open paper trades first (cheap, <=5).
            paper_trader.monitor(book, scanner, now=now)

            # 2. Open new paper trades only before the entry cutoff.
            if now.strftime("%H:%M") < INTRADAY["no_entry_after"]:
                opportunities = scanner.scan_all_fno_stocks(min_discount_score=55)
                if opportunities is not None and not opportunities.empty:
                    output_path = Config.DATA_DIR / "discounted_premiums.csv"
                    opportunities.to_csv(output_path, index=False)
                    logger.info("Scan results saved to %s", output_path)
                paper_trader.process_signals(
                    book, opportunities, now=now, lot_size_fn=self.lot_fn()
                )
            else:
                logger.info("Past entry cutoff %s — monitoring only", INTRADAY["no_entry_after"])
        except Exception:
            logger.exception("Discount cycle failed")

    def run_square_off(self):
        """Force-close any open paper trades at the square-off time."""
        logger.info("Square-off (%s): closing any open paper trades", INTRADAY["square_off"])
        try:
            paper_trader.monitor(self.book(), self.scanner(), square_off=True)
        except Exception:
            logger.exception("Square-off failed")

    def run_eod_summary(self):
        """Square off any stragglers and send the EOD paper-P&L summary."""
        logger.info("EOD summary (%s)", INTRADAY["eod_summary_at"])
        try:
            paper_trader.run_eod(self.book(), self.scanner())
        except Exception:
            logger.exception("EOD summary failed")

    def run_directional_iv_scan(self):
        """Run the directional IV scan once (kept available, not scheduled)."""
        try:
            return run_directional_scan()
        except Exception:
            logger.exception("Directional IV strategy failed")
            return None

    # --- scheduling ---------------------------------------------------------
    def setup_schedule(self):
        """Register the 5-min cycle plus square-off and EOD jobs."""
        schedule.clear()
        for day in WEEKDAYS:
            for run_time in SCAN_TIMES:
                getattr(schedule.every(), day).at(run_time).do(self.run_discount_cycle)
            getattr(schedule.every(), day).at(INTRADAY["square_off"]).do(self.run_square_off)
            getattr(schedule.every(), day).at(INTRADAY["eod_summary_at"]).do(self.run_eod_summary)
        logger.info(
            "Scheduled discount cycle %s..15:15 every %smin | square-off %s | EOD %s",
            INTRADAY["session_start"], INTRADAY["scan_interval_min"],
            INTRADAY["square_off"], INTRADAY["eod_summary_at"],
        )

    def run(self, run_now=False, exit_after_run=False):
        """Start the scheduler loop, with optional immediate execution."""
        self.setup_schedule()
        logger.info("Strategy scheduler started")
        logger.info("Scheduler timezone: %s", APP_TIMEZONE)
        logger.info("Current local time: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if schedule.jobs:
            next_run = min(job.next_run for job in schedule.jobs if job.next_run is not None)
            logger.info("Next scheduled run: %s", next_run.strftime("%Y-%m-%d %H:%M:%S"))

        if run_now:
            logger.info("Immediate run requested")
            self.run_discount_cycle()
            if exit_after_run:
                logger.info("Exiting after immediate run")
                return

        while True:
            schedule.run_pending()
            time.sleep(20)


def main():
    parser = argparse.ArgumentParser(description="Strategy scheduler")
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run one discount cycle immediately before entering the scheduler loop",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one discount cycle immediately and exit without waiting for the next schedule",
    )
    parser.add_argument(
        "--auto-loop",
        action="store_true",
        help="No-op; the scheduler loop is the default. Kept so the Dockerfile "
             "default CMD (python main.py --auto-loop) runs without error.",
    )
    args = parser.parse_args()

    app = StrategySchedulerApp()
    app.run(run_now=args.run_now or args.once, exit_after_run=args.once)


if __name__ == "__main__":
    main()
