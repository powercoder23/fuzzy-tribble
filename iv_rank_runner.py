#!/usr/bin/env python3
"""IV Rank Scanner Runner (service: iv-rank).

Schedule-based wrapper around IVRankScanner — same shape as
directional_iv_runner.py. Reads iv_history.db only; never places orders.
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
from iv_rank_config import OUTPUT_CSV, SCAN_TIMES
from iv_rank_scanner import IVRankScanner

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
        logging.FileHandler(Config.LOGS_DIR / "iv_rank.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def run_iv_rank_scan():
    iv_store.init_db()
    scanner = IVRankScanner()

    logger.info("IV Rank scan starting")
    df = scanner.scan()

    if not df.empty:
        Path(OUTPUT_CSV).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUTPUT_CSV, index=False)
        logger.info("IV Rank results saved to %s", OUTPUT_CSV)

    scanner.persist(df)
    scanner.send_telegram(df)
    return df


def main():
    schedule.clear()

    for day in WEEKDAYS:
        for run_time in SCAN_TIMES:
            getattr(schedule.every(), day).at(run_time.strip()).do(run_iv_rank_scan)
            logger.info("Scheduled iv-rank on %s at %s", day, run_time.strip())

    logger.info("IV Rank runner started")

    now = datetime.now().time()
    if dt_time(9, 15) <= now <= dt_time(15, 30) and datetime.now().weekday() < 5:
        logger.info("Starting during market hours — running first scan immediately")
        run_iv_rank_scan()

    while True:
        schedule.run_pending()
        time.sleep(15)


if __name__ == "__main__":
    main()
