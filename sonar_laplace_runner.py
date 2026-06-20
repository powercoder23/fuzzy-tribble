#!/usr/bin/env python3
"""Sonar-Laplace Scanner Runner (service: sonar).

Schedule wrapper around SonarScanner — same shape as the other screeners.
Reads iv_history.db only; never places orders.
"""

import logging
import os
import time
from datetime import datetime, time as dt_time
from pathlib import Path

import pytz
import schedule

from config import Config
from collectors import iv_store
from sonar_laplace_config import OUTPUT_CSV, SCAN_TIMES
from sonar_laplace_scanner import SonarScanner

IST = pytz.timezone("Asia/Kolkata")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
os.environ["TZ"] = APP_TIMEZONE
if hasattr(time, "tzset"):
    time.tzset()

Config.ensure_dirs()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(Config.LOGS_DIR / "sonar.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def run_sonar_scan():
    iv_store.init_db()
    # Sonar now reads 5-min candles from the shared DataProvider instead of the
    # iv_history spot snapshots. Pollers are not started here (start_pollers=False)
    # — reads fall back to a direct fetch per name through the provider.
    from discount import DiscountedPremiumScanner
    from data_provider import DataProvider
    provider = DataProvider(DiscountedPremiumScanner(), start_pollers=False)
    scanner = SonarScanner(data_provider=provider)
    logger.info("Sonar-Laplace scan starting")
    df = scanner.scan()
    if not df.empty:
        Path(OUTPUT_CSV).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUTPUT_CSV, index=False)
        logger.info("Sonar results saved to %s", OUTPUT_CSV)
    scanner.persist(df)
    scanner.send_telegram(df)
    return df


def main():
    schedule.clear()
    for day in WEEKDAYS:
        for run_time in SCAN_TIMES:
            getattr(schedule.every(), day).at(run_time.strip()).do(run_sonar_scan)
            logger.info("Scheduled sonar on %s at %s", day, run_time.strip())
    logger.info("Sonar-Laplace runner started")

    now = datetime.now().time()
    if dt_time(9, 15) <= now <= dt_time(15, 30) and datetime.now().weekday() < 5:
        logger.info("Started during market hours — running first scan immediately")
        run_sonar_scan()

    while True:
        schedule.run_pending()
        time.sleep(15)


if __name__ == "__main__":
    main()
