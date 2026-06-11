#!/usr/bin/env python3
"""Delivery-% Surge Scanner Runner (service: delivery-surge).

Schedule wrapper around DeliverySurgeScanner. Reads iv_history.db only; no orders.
Runs in the evening, after the bhav collector populates delivery_daily.
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
from delivery_surge_config import OUTPUT_CSV, SCAN_TIMES
from delivery_surge_scanner import DeliverySurgeScanner

IST = pytz.timezone("Asia/Kolkata")
os.environ["TZ"] = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
if hasattr(time, "tzset"):
    time.tzset()

Config.ensure_dirs()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(Config.LOGS_DIR / "delivery_surge.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def run_delivery_surge_scan():
    iv_store.init_db()
    scanner = DeliverySurgeScanner()

    logger.info("Delivery-surge scan starting")
    df = scanner.scan()

    if not df.empty:
        Path(OUTPUT_CSV).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUTPUT_CSV, index=False)
        logger.info("Delivery-surge results saved to %s", OUTPUT_CSV)

    scanner.persist(df)
    scanner.send_telegram(df)
    return df


def main():
    schedule.clear()
    for day in WEEKDAYS:
        for run_time in SCAN_TIMES:
            getattr(schedule.every(), day).at(run_time.strip()).do(run_delivery_surge_scan)
            logger.info("Scheduled delivery-surge on %s at %s", day, run_time.strip())

    logger.info("Delivery-surge runner started")
    # Evening signal — run once on startup if it's already past the first slot.
    now = datetime.now().time()
    if now >= dt_time(19, 30) and datetime.now().weekday() < 5:
        logger.info("Past first evening slot — running once on startup")
        run_delivery_surge_scan()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
