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
from collections import deque

import schedule

from config import Config
from token_manager import TokenManager
from discount import (
    DiscountedPremiumScanner,
    get_trading_days_to_expiry,
    init_iv_db,
    migrate_csv_to_sqlite,
    unwrap_dhan_payload,
)


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
        ]
        logger.info("Legacy scanner disabled - using trigger-based system")

    @staticmethod
    def _warmup_security_segment(security_name):
        return "IDX_I" if security_name in ["NIFTY", "BANKNIFTY"] else "NSE_FNO"

    @staticmethod
    def _validate_warmup_chain(scanner, chain_data):
        if not isinstance(chain_data, dict):
            return False, "empty option chain payload"
        spot_price = chain_data.get("last_price")
        option_chain = chain_data.get("oc")
        if spot_price is None or not isinstance(option_chain, dict) or not option_chain:
            return False, "empty option chain payload"
        if len(option_chain) < 20:
            return False, f"incomplete option chain ({len(option_chain)} strikes)"
        atm_context = scanner.extract_atm_reference_ivs(option_chain, spot_price)
        if not atm_context or atm_context.get("atm_iv") is None:
            return False, "missing ATM IV"
        return True, None

    def process_symbol_with_retry(
        self,
        scanner,
        security_id,
        security_name,
        expiry_cache,
        max_retries=3,
    ):
        symbol_key = str(security_id)
        security_segment = self._warmup_security_segment(security_name)

        expiry = expiry_cache.get(symbol_key)
        if expiry is None:
            expiries = scanner.get_expiry_list(security_id, security_segment)
            if not expiries:
                logger.info("Skipping %s - no expiries available", security_name)
                return False
            selected_expiry = None

            for i, exp in enumerate(expiries):
                dte = get_trading_days_to_expiry(exp)

                if dte >= 7:
                    selected_expiry = exp
                    break

            if not selected_expiry:
                selected_expiry = expiries[min(1, len(expiries) - 1)]

            expiry = selected_expiry
            expiry_cache[symbol_key] = expiry

        dte = get_trading_days_to_expiry(expiry)
        logger.info(f"Selected expiry: {expiry} (DTE: {dte})")

        last_error = None
        for attempt in range(max_retries):
            logger.info("Warmup fetch %s | attempt %s/%s", security_name, attempt + 1, max_retries)
            try:
                chain_response = scanner.get_option_chain(security_id, security_segment, expiry)
                if not isinstance(chain_response, dict) or chain_response.get("status") != "success":
                    raise ValueError("invalid option chain response")
                chain_data = unwrap_dhan_payload(chain_response.get("data") or {})
                is_valid, reason = self._validate_warmup_chain(scanner, chain_data)
                if not is_valid:
                    raise ValueError(reason)

                spot_price = chain_data.get("last_price")
                option_chain = chain_data.get("oc")
                atm_context = scanner.extract_atm_reference_ivs(option_chain, spot_price)
                chain_metrics = scanner.extract_chain_metrics(option_chain)
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
            except Exception as exc:
                last_error = exc
                scanner.runtime_state.get("option_chain", {}).pop((str(security_id), security_segment, expiry), None)
                logger.warning(
                    "Warmup fetch failed for %s on attempt %s/%s: %s",
                    security_name,
                    attempt + 1,
                    max_retries,
                    exc,
                )
                if attempt < max_retries - 1:
                    time.sleep(min(3.0, 1.5 + (attempt * 0.75)))

        logger.warning("Warmup retries exhausted for %s: %s", security_name, last_error)
        return False

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

            opportunities = scanner.scan_all_fno_stocks(min_discount_score=40)
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

    def run_auto_watchlist_eod(self):
        """Build the automated NSE F&O watchlist for the next active session."""
        logger.info("%s", "=" * 70)
        logger.info("Running strategy: auto_watchlist_eod")
        logger.info("%s", "=" * 70)
        try:
            token = self.token_manager.refresh_if_needed()
            scanner = DiscountedPremiumScanner(
                hardtoken=token,
                client_id=Config.DHAN_CLIENT_ID,
            )
            return scanner.build_watchlist_eod()
        except Exception:
            logger.exception("Automated watchlist build failed")
            return []

    def run_auto_warmup_cycle(self):
        """Run one automated warmup cycle over the persisted watchlist."""
        logger.info("%s", "=" * 70)
        logger.info("Running strategy: auto_warmup_cycle")
        logger.info("%s", "=" * 70)
        try:
            token = self.token_manager.refresh_if_needed()
            scanner = DiscountedPremiumScanner(
                hardtoken=token,
                client_id=Config.DHAN_CLIENT_ID,
                store_intraday=True,
            )
            return scanner.run_warmup_cycle()
        except Exception:
            logger.exception("Automated warmup cycle failed")
            return []

    def run_auto_active_scanner(self):
        """Run one automated active scanner cycle."""
        logger.info("%s", "=" * 70)
        logger.info("Running strategy: auto_active_scanner")
        logger.info("%s", "=" * 70)
        try:
            token = self.token_manager.refresh_if_needed()
            scanner = DiscountedPremiumScanner(
                hardtoken=token,
                client_id=Config.DHAN_CLIENT_ID,
                store_intraday=True,
            )
            return scanner.run_active_scanner()
        except Exception:
            logger.exception("Automated active scanner failed")
            return []

    def run_auto_loop(self, exit_after_one_cycle=False):
        """Clock-aware automated loop: watchlist before 09:15, warmup until 09:50, active scan until 15:20."""
        self.warm_up_token()
        logger.info("Automated strategy loop started")
        while True:
            now = datetime.now().time()
            if now < dt_time(9, 15):
                self.run_auto_watchlist_eod()
            elif dt_time(9, 15) <= now < dt_time(9, 50):
                self.run_auto_warmup_cycle()
            elif dt_time(9, 50) <= now <= dt_time(15, 20):
                self.run_auto_active_scanner()
            else:
                logger.info("Automated loop idle outside trading window")

            if exit_after_one_cycle:
                return
            time.sleep(300)

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
            expiry_cache = {}
            symbols_queue = deque(all_symbols)
            cycle_count = 0
            processed_in_cycle = 0

            while symbols_queue:
                now = datetime.now().time()
                if not ignore_time_window:
                    if now < start_time:
                        time.sleep(0.1)
                        continue
                    if now >= end_time:
                        break

                security_id, security_name = symbols_queue.popleft()
                try:
                    success = self.process_symbol_with_retry(
                        scanner=scanner,
                        security_id=security_id,
                        security_name=security_name,
                        expiry_cache=expiry_cache,
                    )
                    if success:
                        logger.info("Warmup completed for %s | remaining=%s", security_name, len(symbols_queue))
                    else:
                        symbols_queue.append((security_id, security_name))
                except Exception:
                    logger.exception("Warmup processing failed for %s", security_name)
                    symbols_queue.append((security_id, security_name))

                processed_in_cycle += 1
                if processed_in_cycle >= len(all_symbols):
                    processed_in_cycle = 0
                    cycle_count += 1
                    logger.info("Completed full cycle, restarting from beginning")
                    logger.info("Completed full cycle | metrics=%s", scanner.get_warmup_metrics())
                    if max_cycles is not None and cycle_count >= max_cycles:
                        break

                if symbols_queue:
                    time.sleep(1.5)
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
            logger.info("Legacy scanner disabled - using trigger-based system")
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
    parser.add_argument(
        "--auto-loop",
        action="store_true",
        help="Run the automated IV-compression options system loop",
    )
    parser.add_argument(
        "--auto-once",
        action="store_true",
        help="Run one clock-aware automated system cycle and exit",
    )
    parser.add_argument(
        "--build-watchlist",
        action="store_true",
        help="Build the automated F&O watchlist immediately and exit",
    )
    parser.add_argument(
        "--active-scan",
        action="store_true",
        help="Run one automated active scanner cycle immediately and exit",
    )
    parser.add_argument(
        "--backtest-auto",
        type=int,
        default=None,
        metavar="DAYS",
        help="Backtest the automated strategy over the given number of days",
    )
    args = parser.parse_args()

    init_iv_db()
    migrate_csv_to_sqlite()

    app = StrategySchedulerApp()
    if args.build_watchlist:
        app.run_auto_watchlist_eod()
        return
    if args.active_scan:
        app.run_auto_active_scanner()
        return
    if args.backtest_auto is not None:
        token = app.token_manager.refresh_if_needed()
        scanner = DiscountedPremiumScanner(
            hardtoken=token,
            client_id=Config.DHAN_CLIENT_ID,
        )
        scanner.backtest_strategy(days=args.backtest_auto)
        return
    if args.auto_loop or args.auto_once:
        app.run_auto_loop(exit_after_one_cycle=args.auto_once)
        return
    if args.warmup:
        logger.info("Running warmup in dev mode")
        app.run_premarket_warmup(ignore_time_window=True, max_cycles=args.warmup_cycles)
        return

    app.run(run_now=args.run_now or args.once, exit_after_run=args.once)


if __name__ == "__main__":
    main()
