#!/usr/bin/env python3
"""Smart-Money (Bulk/Block) Scanner Runner (service: smart-money).

Schedule wrapper around SmartMoneyScanner. Reads iv_history.db only; no orders.
Runs in the evening, after the deals collector publishes bulk/block deals.
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
from smart_money_config import OUTPUT_CSV, SCAN_TIMES
from smart_money_scanner import SmartMoneyScanner

IST = pytz.timezone("Asia/Kolkata")
os.environ["TZ"] = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
if hasattr(time, "tzset"):
    time.tzset()

Config.ensure_dirs()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(Config.LOGS_DIR / "smart_money.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def run_smart_money_scan():
    iv_store.init_db()
    scanner = SmartMoneyScanner()

    logger.info("Smart-money scan starting")
    df = scanner.scan()

    if not df.empty:
        Path(OUTPUT_CSV).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUTPUT_CSV, index=False)
        logger.info("Smart-money results saved to %s", OUTPUT_CSV)

    scanner.persist(df)
    scanner.send_telegram(df)
    return df


def main():
    schedule.clear()
    for day in WEEKDAYS:
        for run_time in SCAN_TIMES:
            getattr(schedule.every(), day).at(run_time.strip()).do(run_smart_money_scan)
            logger.info("Scheduled smart-money on %s at %s", day, run_time.strip())

    logger.info("Smart-money runner started")
    now = datetime.now().time()
    if now >= dt_time(16, 0) and datetime.now().weekday() < 5:
        logger.info("Past first evening slot — running once on startup")
        run_smart_money_scan()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
