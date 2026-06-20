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
from order_manager import OrderManager
from trade_suggester import TradeSuggester
from cycle_gate import CycleGate


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


# Scanner cadence: find + book NEW trades every scan_interval_min (15) until 15:15.
SCAN_TIMES = generate_interval_times(
    start=INTRADAY["session_start"],
    end="15:15",
    interval_minutes=INTRADAY["scan_interval_min"],
)
# Order-manager cadence: re-price + exit-manage OPEN positions every
# monitor_interval_min (5) until square-off. Runs independently of the scan.
MONITOR_TIMES = generate_interval_times(
    start=INTRADAY["session_start"],
    end=INTRADAY.get("monitor_until", "15:20"),
    interval_minutes=INTRADAY.get("monitor_interval_min", 5),
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
        self._order_manager = None
        self._suggester = None
        self._cycle_gate = None
        self._lot_fn = None

    # --- lazy singletons ----------------------------------------------------
    def scanner(self):
        if self._scanner is None:
            self._scanner = DiscountedPremiumScanner()
        return self._scanner

    def order_manager(self):
        if self._order_manager is None:
            self._order_manager = OrderManager()
        return self._order_manager

    def suggester(self):
        if self._suggester is None:
            self._suggester = TradeSuggester()
        return self._suggester

    def cycle_gate(self):
        if self._cycle_gate is None:
            self._cycle_gate = CycleGate()
        return self._cycle_gate

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
    def run_scan_cycle(self):
        """Scanner job (every scan_interval_min, 15): find signals and SUBMIT the
        top picks to the OrderManager. Does NOT manage open positions — that is
        the OrderManager's job on its own cadence."""
        logger.info("%s", "=" * 70)
        logger.info("Discount scan (find + book new trades)")
        logger.info("%s", "=" * 70)
        try:
            now = datetime.now()
            if now.strftime("%H:%M") >= INTRADAY["no_entry_after"]:
                logger.info("Past entry cutoff %s — no new trades", INTRADAY["no_entry_after"])
                return

            scanner = self.scanner()
            # Paper trading — keep the entry bar low so we take more trades.
            opportunities = scanner.scan_all_fno_stocks(min_discount_score=45)
            if opportunities is not None and not opportunities.empty:
                output_path = Config.DATA_DIR / "discounted_premiums.csv"
                opportunities.to_csv(output_path, index=False)
                logger.info("Scan results saved to %s", output_path)

            # Booking a trade == handing it to the OrderManager.
            self.order_manager().submit_signals(
                opportunities, now=now, lot_size_fn=self.lot_fn()
            )

            # Emit a fused suggestion list only once per COMPLETED cycle of the
            # repeated scans (gap/oi/iv), not on every discount tick.
            try:
                if self.cycle_gate().ready_and_mark():
                    self.suggester().suggest_and_alert(opportunities)
            except Exception:
                logger.exception("Trade suggester failed (non-fatal)")
        except Exception:
            logger.exception("Discount scan cycle failed")

    def run_monitor_cycle(self):
        """OrderManager job (every monitor_interval_min, 5): re-price and
        exit-manage ALL open positions, independent of the scan schedule."""
        try:
            self.order_manager().track(self.scanner(), now=datetime.now())
        except Exception:
            logger.exception("OrderManager track cycle failed")

    def run_square_off(self):
        """Force-close any open positions at the square-off time."""
        logger.info("Square-off (%s): closing any open positions", INTRADAY["square_off"])
        try:
            self.order_manager().square_off_all(self.scanner(), now=datetime.now())
        except Exception:
            logger.exception("Square-off failed")

    def run_eod_summary(self):
        """Square off any stragglers and send the EOD paper-P&L summary."""
        logger.info("EOD summary (%s)", INTRADAY["eod_summary_at"])
        try:
            self.order_manager().eod(self.scanner())
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
        """Register the scan cycle (15 min), the OrderManager track cycle
        (5 min), plus square-off and EOD jobs."""
        schedule.clear()
        for day in WEEKDAYS:
            for run_time in SCAN_TIMES:
                getattr(schedule.every(), day).at(run_time).do(self.run_scan_cycle)
            for run_time in MONITOR_TIMES:
                getattr(schedule.every(), day).at(run_time).do(self.run_monitor_cycle)
            getattr(schedule.every(), day).at(INTRADAY["square_off"]).do(self.run_square_off)
            getattr(schedule.every(), day).at(INTRADAY["eod_summary_at"]).do(self.run_eod_summary)
        logger.info(
            "Scheduled discount scan %s..15:15 every %smin | OrderManager track every %smin "
            "until %s | square-off %s | EOD %s",
            INTRADAY["session_start"], INTRADAY["scan_interval_min"],
            INTRADAY.get("monitor_interval_min", 5), INTRADAY.get("monitor_until", "15:20"),
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
            self.run_monitor_cycle()   # manage any open positions first
            self.run_scan_cycle()      # then look for + book new trades
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
