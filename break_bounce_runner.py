#!/usr/bin/env python3
"""
Break and Bounce Strategy Service — Service 4.

Reads IV data from iv_store (written by iv-collector service).
Calls Dhan API for daily candles (premarket) and intraday candles + option
chain (at signal time).

Schedule:
  09:00        run_premarket  (load daily levels, affordability filter)
  09:15–14:30  run_intraday_scan every 5 min
                  - Stocks without a breakout get 15-min breakout check until 11:45
                  - Stocks with a confirmed breakout get 5-min retest check
                    every 5 min until 14:30 (giving late-window breakouts a
                    fair chance to retest)
  15:15        force-exit open positions + daily summary + state reset
"""

import logging
import os
import time
from datetime import datetime, time as dt_time

import pytz
import schedule

import iv_store
from config import Config
from break_bounce_strategy import BreakBounceStrategyRunner

IST = pytz.timezone("Asia/Kolkata")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
os.environ["TZ"] = APP_TIMEZONE
if hasattr(time, "tzset"):
    time.tzset()

Config.ensure_dirs()

# Set DEBUG for break_bounce_strategy to expose per-stock API responses
_log_level = logging.DEBUG if os.getenv("BB_DEBUG", "false").lower() == "true" else logging.INFO

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(Config.LOGS_DIR / "break_bounce.log"),
        logging.StreamHandler(),
    ],
)
logging.getLogger("break_bounce_strategy").setLevel(_log_level)
logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]

# Every 5 min from 9:15 to 14:30 (inclusive).
# The scan itself only accepts new 15-min breakouts until 11:45 — past that, the
# tick is used to keep checking 5-min retest patterns on stocks that already broke out.
INTRADAY_TIMES: list[str] = []
_h, _m = 9, 15
while (_h, _m) <= (14, 30):
    INTRADAY_TIMES.append(f"{_h:02d}:{_m:02d}")
    _m += 5
    if _m >= 60:
        _m -= 60
        _h += 1


def main():
    iv_store.init_db()
    runner = BreakBounceStrategyRunner()

    def _premarket():
        logger.info("=" * 60)
        logger.info("Break & Bounce: premarket scan")
        logger.info("=" * 60)
        runner.run_premarket()

    def _intraday():
        logger.info("Break & Bounce: intraday scan")
        runner.run_intraday_scan()

    def _eod():
        logger.info("Break & Bounce: EOD summary")
        runner.run_eod()

    schedule.clear()
    for day in WEEKDAYS:
        getattr(schedule.every(), day).at("09:00").do(_premarket)
        getattr(schedule.every(), day).at("15:15").do(_eod)
        for t in INTRADAY_TIMES:
            getattr(schedule.every(), day).at(t).do(_intraday)

    logger.info("Break & Bounce strategy scheduler started")
    if schedule.jobs:
        next_run = min(j.next_run for j in schedule.jobs if j.next_run)
        logger.info("Next run: %s", next_run.strftime("%Y-%m-%d %H:%M:%S"))

    # If started mid-session, run premarket immediately so state is ready
    now = datetime.now().time()
    if dt_time(9, 0) <= now <= dt_time(14, 30) and datetime.now().weekday() < 5:
        logger.info("Service started mid-session — running premarket immediately")
        _premarket()

    while True:
        schedule.run_pending()
        time.sleep(15)


if __name__ == "__main__":
    main()
