#!/usr/bin/env python3
"""
Strategy scheduler wrapper.

Keeps the existing token-management flow and runs configured strategies on schedule.
"""

import logging
import os
import time
import argparse
import json
from datetime import datetime
from pathlib import Path

import schedule

from config import Config
from token_manager import TokenManager
from discount import DiscountedPremiumScanner, init_iv_db, migrate_csv_to_sqlite, unwrap_dhan_payload


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

WARMUP_BATCH_SIZE = 50
WARMUP_STATE_FILE = Config.DATA_DIR / "premarket_warmup_state.json"


class WarmupBatchManager:
    def __init__(self, state_file, batch_size=WARMUP_BATCH_SIZE):
        self.state_file = Path(state_file)
        self.batch_size = batch_size

    def _default_state(self):
        return {
            "date": datetime.now().date().isoformat(),
            "next_batch_index": 0,
        }

    def load_state(self):
        if not self.state_file.exists():
            return self._default_state()
        try:
            state = json.loads(self.state_file.read_text())
        except Exception:
            logger.exception("Failed to read warmup batch state; resetting")
            return self._default_state()

        today = datetime.now().date().isoformat()
        if state.get("date") != today:
            return self._default_state()
        return state

    def save_state(self, state):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2))

    def next_batch(self, symbols):
        if not symbols:
            return [], 0, 0, 0

        state = self.load_state()
        total_batches = max((len(symbols) + self.batch_size - 1) // self.batch_size, 1)
        batch_index = state.get("next_batch_index", 0) % total_batches
        start = batch_index * self.batch_size
        end = min(start + self.batch_size, len(symbols))
        next_state = {
            "date": datetime.now().date().isoformat(),
            "next_batch_index": (batch_index + 1) % total_batches,
        }
        self.save_state(next_state)
        return symbols[start:end], batch_index, total_batches, start


class StrategySchedulerApp:
    def __init__(self):
        self.token_manager = TokenManager()
        self.batch_manager = WarmupBatchManager(WARMUP_STATE_FILE)
        self.strategy_jobs = [
            {
                "name": "premarket_warmup",
                "runner": self.run_premarket_warmup,
                "times": ["09:15", "09:23", "09:31", "09:40"],
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

            all_symbols = list(scanner.fno_stocks.items())
            batch_symbols, batch_index, total_batches, start_offset = self.batch_manager.next_batch(all_symbols)
            logger.info(
                "Processing batch %s/%s (%s symbols, offset %s of %s)",
                batch_index + 1,
                total_batches,
                len(batch_symbols),
                start_offset,
                len(all_symbols),
            )

            processed = 0
            skipped = 0
            persisted = 0

            for security_id, security_name in batch_symbols:
                try:
                    if scanner.is_blacklisted(security_id):
                        logger.warning("Skipping blacklisted symbol %s (%s)", security_name, security_id)
                        skipped += 1
                        continue

                    security_segment = "IDX_I" if security_name in ["NIFTY", "BANKNIFTY"] else "NSE_FNO"
                    expiries = scanner.get_expiry_list(security_id, security_segment)
                    if not expiries:
                        logger.warning("Skipping %s due to missing expiries", security_name)
                        scanner.blacklist_symbol(security_id, security_name, "missing expiries")
                        skipped += 1
                        continue

                    expiry = expiries[0]
                    chain_response = scanner.get_option_chain(security_id, security_segment, expiry)
                    if chain_response.get("status") != "success":
                        logger.warning("Skipping %s due to failed option chain fetch", security_name)
                        scanner.blacklist_symbol(security_id, security_name, "option chain failure")
                        skipped += 1
                        continue

                    chain_data = unwrap_dhan_payload(chain_response.get("data") or {})
                    spot_price = chain_data.get("last_price") if isinstance(chain_data, dict) else None
                    option_chain = chain_data.get("oc") if isinstance(chain_data, dict) else None
                    if spot_price is None or not isinstance(option_chain, dict):
                        logger.warning("Skipping %s due to empty option chain payload", security_name)
                        scanner.blacklist_symbol(security_id, security_name, "empty option chain payload")
                        skipped += 1
                        continue

                    atm_context = scanner.extract_atm_reference_ivs(option_chain, spot_price)
                    if not atm_context:
                        logger.info("Skipping %s because ATM context was empty", security_name)
                        skipped += 1
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
                    processed += 1
                    persisted += 1
                except Exception:
                    logger.exception("Premarket warmup failed for %s", security_name)
                    scanner.blacklist_symbol(security_id, security_name, "unexpected warmup exception")
                    skipped += 1

            logger.info(
                "Premarket warmup batch %s/%s complete | processed=%s persisted=%s skipped=%s metrics=%s",
                batch_index + 1,
                total_batches,
                processed,
                persisted,
                skipped,
                scanner.get_warmup_metrics(),
            )
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
