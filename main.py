#!/usr/bin/env python3
"""
Strategy scheduler wrapper.

Keeps the existing token-management flow and runs configured strategies on schedule.
"""

import logging
import os
import time
import argparse
from datetime import datetime, time as dt_time
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

class StrategySchedulerApp:
    def __init__(self):
        self.token_manager = TokenManager()
        self.strategy_jobs = [
            {
                "name": "premarket_warmup",
                "runner": self.run_premarket_warmup,
                "times": ["09:15"],
            },
            {
                "name": "discount",
                "runner": self.run_discount_scan,
                "times": DEFAULT_SCAN_TIMES,
            }
        ]

    @staticmethod
    def _warmup_security_segment(security_name):
        return "IDX_I" if security_name in ["NIFTY", "BANKNIFTY"] else "NSE_FNO"

    def process_symbol_if_allowed(
        self,
        scanner,
        security_id,
        security_name,
        expiry_cache,
        option_chain_cache,
        last_fetched,
        warmup_failures,
    ):
        symbol_key = str(security_id)
        if scanner.is_blacklisted(security_id):
            logger.warning("Skipping blacklisted symbol %s (%s)", security_name, security_id)
            return False

        security_segment = self._warmup_security_segment(security_name)

        expiry = expiry_cache.get(symbol_key)
        if expiry is None:
            expiries = scanner.get_expiry_list(security_id, security_segment)
            if not expiries:
                logger.info("Skipping %s - no expiries available", security_name)
                return False
            expiry = expiries[0]
            expiry_cache[symbol_key] = expiry

        now_ts = time.time()
        cached_entry = option_chain_cache.get(symbol_key)
        if cached_entry:
            cached_ts, cached_data = cached_entry
            if now_ts - cached_ts < 3:
                chain_data = cached_data
            else:
                chain_data = None
        else:
            chain_data = None

        if chain_data is None:
            last_time = last_fetched.get(symbol_key, 0)
            if now_ts - last_time < 3:
                logger.info("Skipping %s due to rate limit", security_name)
                return False

            logger.info("Fetching %s", security_name)
            try:
                chain_response = scanner.dhan.option_chain(
                    under_security_id=security_id,
                    under_exchange_segment=security_segment,
                    expiry=expiry,
                )
            except Exception:
                failure_count = warmup_failures.get(symbol_key, 0) + 1
                warmup_failures[symbol_key] = failure_count
                logger.exception("Premarket warmup fetch failed for %s", security_name)
                if failure_count >= 3:
                    scanner.blacklist_symbol(security_id, security_name, "option chain failure")
                return False

            if not isinstance(chain_response, dict) or chain_response.get("status") != "success":
                failure_count = warmup_failures.get(symbol_key, 0) + 1
                warmup_failures[symbol_key] = failure_count
                logger.warning(
                    "Skipping %s due to failed option chain fetch (failure %s/3)",
                    security_name,
                    failure_count,
                )
                if failure_count >= 3:
                    scanner.blacklist_symbol(security_id, security_name, "option chain failure")
                return False

            chain_data = unwrap_dhan_payload(chain_response.get("data") or {})
            if not isinstance(chain_data, dict):
                failure_count = warmup_failures.get(symbol_key, 0) + 1
                warmup_failures[symbol_key] = failure_count
                logger.warning(
                    "Skipping %s due to empty option chain payload (failure %s/3)",
                    security_name,
                    failure_count,
                )
                if failure_count >= 3:
                    scanner.blacklist_symbol(security_id, security_name, "empty option chain payload")
                return False

            last_fetched[symbol_key] = now_ts
            option_chain_cache[symbol_key] = (now_ts, chain_data)

        spot_price = chain_data.get("last_price") if isinstance(chain_data, dict) else None
        option_chain = chain_data.get("oc") if isinstance(chain_data, dict) else None
        if spot_price is None or not isinstance(option_chain, dict) or not option_chain:
            failure_count = warmup_failures.get(symbol_key, 0) + 1
            warmup_failures[symbol_key] = failure_count
            logger.warning(
                "Skipping %s due to empty option chain payload (failure %s/3)",
                security_name,
                failure_count,
            )
            if failure_count >= 3:
                scanner.blacklist_symbol(security_id, security_name, "empty option chain payload")
            return False

        warmup_failures[symbol_key] = 0
        atm_context = scanner.extract_atm_reference_ivs(option_chain, spot_price)
        chain_metrics = scanner.extract_chain_metrics(option_chain)
        if not atm_context or atm_context.get("atm_iv") is None:
            logger.info("Skipping %s because ATM context was empty", security_name)
            return False

        scanner.persist_iv_snapshot(
            security_id=security_id,
            exchange_segment=security_segment,
            security_name=security_name,
            expiry=expiry,
            spot_price=spot_price,
            atm_context=atm_context,
            chain_metrics=chain_metrics,
            store_intraday=True,
        )
        logger.info("Persisted IV snapshot for %s", security_name)
        return True

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

    def run_premarket_warmup(self, ignore_time_window=False, max_cycles=None):
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
            if not all_symbols:
                logger.warning("Premarket warmup skipped because no F&O symbols are available")
                return False

            start_time = dt_time(9, 15)
            end_time = dt_time(9, 50)
            warmup_failures = scanner.runtime_state.setdefault("warmup_failures", {})
            expiry_cache = {}
            option_chain_cache = {}
            last_fetched = {}
            symbols = list(all_symbols)
            index = 0
            total = len(symbols)
            cycle_count = 0

            while True:
                now = datetime.now().time()
                if not ignore_time_window:
                    if now < start_time:
                        time.sleep(0.1)
                        continue
                    if now >= end_time:
                        break

                security_id, security_name = symbols[index]
                try:
                    self.process_symbol_if_allowed(
                        scanner=scanner,
                        security_id=security_id,
                        security_name=security_name,
                        expiry_cache=expiry_cache,
                        option_chain_cache=option_chain_cache,
                        last_fetched=last_fetched,
                        warmup_failures=warmup_failures,
                    )
                except Exception:
                    failure_count = warmup_failures.get(str(security_id), 0) + 1
                    warmup_failures[str(security_id)] = failure_count
                    logger.exception("Premarket warmup failed for %s", security_name)
                    if failure_count >= 3:
                        scanner.blacklist_symbol(security_id, security_name, "unexpected warmup exception")

                index += 1
                if index >= total:
                    index = 0
                    cycle_count += 1
                    logger.info("Completed full cycle, restarting from beginning")
                    logger.info("Completed full cycle | metrics=%s", scanner.get_warmup_metrics())
                    if max_cycles is not None and cycle_count >= max_cycles:
                        break

                time.sleep(0.1)
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
    parser.add_argument(
        "--warmup",
        action="store_true",
        help="Run premarket warmup immediately (dev mode)",
    )
    parser.add_argument(
        "--warmup-cycles",
        type=int,
        default=None,
        help="Stop warmup after the given number of full symbol cycles",
    )
    args = parser.parse_args()

    init_iv_db()
    migrate_csv_to_sqlite()

    app = StrategySchedulerApp()
    if args.warmup:
        logger.info("Running warmup in dev mode")
        app.run_premarket_warmup(ignore_time_window=True, max_cycles=args.warmup_cycles)
        return

    app.run(run_now=args.run_now or args.once, exit_after_run=args.once)


if __name__ == "__main__":
    main()
