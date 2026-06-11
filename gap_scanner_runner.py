#!/usr/bin/env python3
"""Extreme Opening (Gap + Range) Scanner Runner (service: gap-scan).

Schedule wrapper around GapScanner. Reads iv_history.db only; no orders.
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
from gap_scanner_config import OUTPUT_CSV, SCAN_TIMES
from gap_scanner import GapScanner

IST = pytz.timezone("Asia/Kolkata")
os.environ["TZ"] = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
if hasattr(time, "tzset"):
    time.tzset()

Config.ensure_dirs()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(Config.LOGS_DIR / "gap_scan.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def run_gap_scan():
    iv_store.init_db()
    scanner = GapScanner()

    logger.info("Gap scan starting")
    df = scanner.scan()

    if not df.empty:
        Path(OUTPUT_CSV).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUTPUT_CSV, index=False)
        logger.info("Gap results saved to %s", OUTPUT_CSV)

    scanner.persist(df)
    scanner.send_telegram(df)
    return df


def main():
    schedule.clear()
    for day in WEEKDAYS:
        for run_time in SCAN_TIMES:
            getattr(schedule.every(), day).at(run_time.strip()).do(run_gap_scan)
            logger.info("Scheduled gap-scan on %s at %s", day, run_time.strip())

    logger.info("Gap scan runner started")
    now = datetime.now().time()
    if dt_time(9, 15) <= now <= dt_time(15, 30) and datetime.now().weekday() < 5:
        logger.info("Starting during market hours — running first scan immediately")
        run_gap_scan()

    while True:
        schedule.run_pending()
        time.sleep(15)


if __name__ == "__main__":
    main()
