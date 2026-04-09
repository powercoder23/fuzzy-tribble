#!/usr/bin/env python3
"""
Strategy scheduler wrapper.

Keeps the existing token-management flow and runs configured strategies on schedule.
"""

import logging
import os
import time
import argparse
from datetime import datetime
from pathlib import Path

import schedule

from config import Config
from token_manager import TokenManager
from discount import DiscountedPremiumScanner, init_iv_db, migrate_csv_to_sqlite


APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")

# Force the process to use the configured timezone instead of the container default.
os.environ["TZ"] = APP_TIMEZONE
if hasattr(time, "tzset"):
    time.tzset()

DEFAULT_SCAN_TIMES = [
    "09:50", "10:10", "11:30", "13:30", "15:05", "15:25"
]
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
        self.token_manager = TokenManager()
        self.strategy_jobs = [
            {
                "name": "premarket_warmup",
                "runner": self.run_premarket_warmup,
                "times": ["09:15", "09:20", "09:25", "09:30", "09:35", "09:40", "09:45"],
            },
            {
                "name": "discount",
                "runner": self.run_discount_scan,
                "times": DEFAULT_SCAN_TIMES,
            }
        ]

    def build_discount_scanner(self):
        """Create the discount scanner with a valid token."""
        token = self.token_manager.get_valid_token()
        if not token:
            raise RuntimeError("Failed to get valid token")

        logger.info("Token obtained (first 10 chars): %s...", token[:10])
        return DiscountedPremiumScanner(
            hardtoken=token,
            client_id=Config.DHAN_CLIENT_ID,
        )

    def warm_up_token(self):
        """Ensure a valid token exists before the scheduler starts waiting."""
        token = self.token_manager.get_valid_token()
        if not token:
            raise RuntimeError("Failed to warm up access token")

        logger.info("Startup token is ready")
        return token

    def run_discount_scan(self):
        """Run the discount scanner once."""
        logger.info("%s", "=" * 70)
        logger.info("Running strategy: discount")
        logger.info("%s", "=" * 70)

        try:
            token = self.token_manager.refresh_if_needed()
            scanner = DiscountedPremiumScanner(
                hardtoken=token,
                client_id=Config.DHAN_CLIENT_ID,
            )

            opportunities = scanner.scan_all_fno_stocks(min_discount_score=55)
            scanner.generate_report(opportunities)

            if not opportunities.empty:
                output_path = Path("discounted_premiums.csv")
                opportunities.to_csv(output_path, index=False)
                logger.info("Results saved to %s", output_path)

            scanner.send_telegram_summary(opportunities)
            return opportunities
        except Exception:
            logger.exception("Discount strategy failed")
            return None

    def run_premarket_warmup(self):
        """Collect premarket ATM IV snapshots without running the scanner."""
        logger.info("%s", "=" * 70)
        logger.info("Running strategy: premarket_warmup")
        logger.info("%s", "=" * 70)

        try:
            token = self.token_manager.refresh_if_needed()
            scanner = DiscountedPremiumScanner(
                hardtoken=token,
                client_id=Config.DHAN_CLIENT_ID,
                store_intraday=True,
            )

            for security_id, security_name in list(scanner.fno_stocks.items())[:10]:
                try:
                    security_segment = "IDX_I" if security_name in ["NIFTY", "BANKNIFTY"] else "NSE_FNO"
                    expiries = scanner.get_expiry_list(security_id, security_segment)
                    if not expiries:
                        continue

                    expiry = expiries[0]
                    chain_response = scanner.get_option_chain(security_id, security_segment, expiry)
                    if chain_response.get("status") != "success":
                        continue

                    chain_data = chain_response.get("data") or {}
                    chain_data = chain_data.get("data", chain_data) if isinstance(chain_data, dict) else chain_data
                    spot_price = chain_data.get("last_price") if isinstance(chain_data, dict) else None
                    option_chain = chain_data.get("oc") if isinstance(chain_data, dict) else None
                    if spot_price is None or not isinstance(option_chain, dict):
                        continue

                    atm_context = scanner.extract_atm_reference_ivs(option_chain, spot_price)
                    if not atm_context:
                        continue

                    scanner.persist_iv_snapshot(
                        security_id=security_id,
                        exchange_segment=security_segment,
                        security_name=security_name,
                        expiry=expiry,
                        spot_price=spot_price,
                        atm_context=atm_context,
                        store_intraday=True,
                    )
                except Exception:
                    logger.exception("Premarket warmup failed for %s", security_name)

            return True
        except Exception:
            logger.exception("Premarket warmup failed")
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
        self.warm_up_token()
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

    init_iv_db()
    migrate_csv_to_sqlite()

    app = StrategySchedulerApp()
    app.run(run_now=args.run_now or args.once, exit_after_run=args.once)


if __name__ == "__main__":
    main()
