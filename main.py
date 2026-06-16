#!/usr/bin/env python3
"""
Strategy scheduler wrapper.

Keeps the existing token-management flow and runs configured strategies on schedule.
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
from directional_iv_runner import run_directional_scan


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


DEFAULT_SCAN_TIMES = generate_interval_times(start="09:30", end="15:15", interval_minutes=15)
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
        self.strategy_jobs = [
            {
                "name": "discount",
                "runner": self.run_discount_scan,
                "times": DEFAULT_SCAN_TIMES,
            }
        ]

    def build_discount_scanner(self):
        """Create the discount scanner."""
        return DiscountedPremiumScanner()

    def run_discount_scan(self):
        """Run the discount scanner once."""
        logger.info("%s", "=" * 70)
        logger.info("Running strategy: discount")
        logger.info("%s", "=" * 70)

        try:
            scanner = DiscountedPremiumScanner()
            opportunities = scanner.scan_all_fno_stocks(min_discount_score=55)
            scanner.generate_report(opportunities)

            if not opportunities.empty:
                output_path = Config.DATA_DIR / "discounted_premiums.csv"
                opportunities.to_csv(output_path, index=False)
                logger.info("Results saved to %s", output_path)

            scanner.send_telegram_summary(opportunities)
            return opportunities
        except Exception:
            logger.exception("Discount strategy failed")
            return None

    def run_directional_iv_scan(self):
        """Run the directional IV scan once."""
        logger.info("%s", "=" * 70)
        logger.info("Running strategy: directional_iv")
        logger.info("%s", "=" * 70)

        try:
            opportunities = run_directional_scan()
            return opportunities
        except Exception:
            logger.exception("Directional IV strategy failed")
            return None

    def setup_schedule(self):
        """Register all strategy jobs."""
        schedule.clear()

        for job in self.strategy_jobs:
            for day in WEEKDAYS:
                for run_time in job["times"]:
                    getattr(schedule.every(), day).at(run_time).do(job["runner"])
                    logger.info("Scheduled %s on %s at %s", job["name"], day, run_time)

    def run(self, run_now=False, exit_after_run=False):
        """Start the scheduler loop, with optional immediate execution."""
        self.setup_schedule()
        logger.info("Strategy scheduler started")
        logger.info("Scheduler timezone: %s", APP_TIMEZONE)
        logger.info("Current local time: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if schedule.jobs:
            next_run = min(job.next_run for job in schedule.jobs if job.next_run is not None)
            logger.info("Next scheduled run: %s", next_run.strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("Configured strategies: %s", ", ".join(job["name"] for job in self.strategy_jobs))

        if run_now:
            logger.info("Immediate run requested")
            self.run_discount_scan()
            if exit_after_run:
                logger.info("Exiting after immediate run")
                return

        while True:
            schedule.run_pending()
            time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="Strategy scheduler")
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run the discount scan immediately before entering the scheduler loop",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run the discount scan immediately and exit without waiting for the next schedule",
    )
    args = parser.parse_args()

    app = StrategySchedulerApp()
    app.run(run_now=args.run_now or args.once, exit_after_run=args.once)


if __name__ == "__main__":
    main()
