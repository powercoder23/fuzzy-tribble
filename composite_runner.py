#!/usr/bin/env python3
"""Composite Conviction Scanner Runner (service: composite).

Schedule-based wrapper around CompositeScanner — same shape as
iv_rank_runner.py / smart_money_runner.py. Reads iv_history.db only
(the persisted *_history tables); never places orders.

Runs EOD by default (after smart-money + delivery-surge have populated their
tables). Set CMP_INTRADAY_TIMES to also run a live, stale-overlay read.
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
from composite_config import OUTPUT_CSV, SCAN_TIMES, INTRADAY_TIMES
from composite_scanner import CompositeScanner

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
        logging.FileHandler(Config.LOGS_DIR / "composite.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def run_composite_scan():
    iv_store.init_db()
    scanner = CompositeScanner()

    logger.info("Composite conviction scan starting")
    df = scanner.scan()

    if not df.empty:
        Path(OUTPUT_CSV).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUTPUT_CSV, index=False)
        logger.info("Composite results saved to %s", OUTPUT_CSV)

    scanner.persist(df)
    scanner.send_telegram(df)
    return df


def main():
    schedule.clear()

    all_times = list(SCAN_TIMES) + list(INTRADAY_TIMES)
    for day in WEEKDAYS:
        for run_time in all_times:
            t = run_time.strip()
            if not t:
                continue
            getattr(schedule.every(), day).at(t).do(run_composite_scan)
            logger.info("Scheduled composite on %s at %s", day, t)

    logger.info("Composite runner started")

    # If started after the first EOD slot on a weekday, run once now so the
    # latest factor tables are fused immediately.
    now = datetime.now().time()
    first_slot = min(all_times) if all_times else "20:15"
    try:
        fh, fm = map(int, first_slot.strip().split(":"))
    except ValueError:
        fh, fm = 20, 15
    if datetime.now().weekday() < 5 and now >= dt_time(fh, fm):
        logger.info("Started after first slot — running composite scan immediately")
        run_composite_scan()

    while True:
        schedule.run_pending()
        time.sleep(15)


if __name__ == "__main__":
    main()
