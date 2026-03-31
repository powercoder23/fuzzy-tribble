#!/usr/bin/env python3
"""
Strategy scheduler wrapper.

Keeps the existing token-management flow and runs configured strategies on schedule.
"""

import logging
import time
from pathlib import Path

import schedule

from config import Config
from token_manager import TokenManager
from discount import DiscountedPremiumScanner


DEFAULT_SCAN_TIMES = [
    "10:01", "11:01", "12:01", "13:01", "14:03", "15:05"
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

    def setup_schedule(self):
        """Register all strategy jobs."""
        schedule.clear()

        for job in self.strategy_jobs:
            for day in WEEKDAYS:
                for run_time in job["times"]:
                    getattr(schedule.every(), day).at(run_time).do(job["runner"])
                    logger.info("Scheduled %s on %s at %s", job["name"], day, run_time)

    def run(self):
        """Start the scheduler loop."""
        self.warm_up_token()
        self.setup_schedule()
        logger.info("Strategy scheduler started")
        logger.info("Configured strategies: %s", ", ".join(job["name"] for job in self.strategy_jobs))

        while True:
            schedule.run_pending()
            time.sleep(30)


def main():
    app = StrategySchedulerApp()
    app.run()


if __name__ == "__main__":
    main()
