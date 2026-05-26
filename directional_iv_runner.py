#!/usr/bin/env python3
"""Directional IV Strategy Runner."""

import logging
import os
import time
from datetime import datetime, time as dt_time
from pathlib import Path

import pytz
import schedule

from config import Config
import iv_store
from directional_iv_config import OUTPUT_CSV
from directional_iv_strategy import DirectionalIVScanner

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
        logging.FileHandler(Config.LOGS_DIR / "directional_iv.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
SCAN_TIMES = ["09:45", "11:15", "13:15", "14:45", "15:05"]


def run_directional_scan():
    iv_store.init_db()
    scanner = DirectionalIVScanner()

    logger.info("Directional IV scan starting")
    opportunities = scanner.scan_all_underlyings()
    scanner.generate_report(opportunities)

    if not opportunities.empty:
        Path(OUTPUT_CSV).parent.mkdir(parents=True, exist_ok=True)
        opportunities.to_csv(OUTPUT_CSV, index=False)
        logger.info("Results saved to %s", OUTPUT_CSV)

    scanner.send_telegram_summary(opportunities)
    return opportunities


def main():
    schedule.clear()

    for day in WEEKDAYS:
        for run_time in SCAN_TIMES:
            getattr(schedule.every(), day).at(run_time).do(lambda: run_directional_scan())
            logger.info("Scheduled directional_iv on %s at %s", day, run_time)

    logger.info("Directional IV runner started")

    now = datetime.now().time()
    if now >= dt_time(9, 15) and now <= dt_time(15, 30) and datetime.now().weekday() < 5:
        logger.info("Starting during market hours — running first scan immediately")
        run_directional_scan()

    while True:
        schedule.run_pending()
        time.sleep(15)


if __name__ == "__main__":
    main()
