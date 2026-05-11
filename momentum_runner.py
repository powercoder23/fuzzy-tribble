#!/usr/bin/env python3
"""
Momentum Strategy Service — Service 2.

Runs the ORB/VWAP momentum strategy.
Reads IV data from iv_store (written by iv-collector service).
Calls Dhan API only for:
  - Daily candles (regime detection at premarket)
  - Option chain at signal confirmation + order execution

Schedule:
  09:00        run_premarket  (VIX check, affordability filter, regime scan)
  09:30–11:30  run_intraday_scan every 5 min
  15:15        daily summary Telegram
"""

import logging
import os
import time
from datetime import datetime, time as dt_time

import pytz
import schedule

import iv_store
from config import Config
from momentum_strategy import MomentumStrategyRunner

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
        logging.FileHandler(Config.LOGS_DIR / "momentum.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]

INTRADAY_TIMES = [
    "09:30", "09:35", "09:40", "09:45", "09:50",
    "09:55", "10:00", "10:05", "10:10", "10:15",
    "10:20", "10:25", "10:30", "10:35", "10:40",
    "10:45", "10:50", "10:55", "11:00", "11:05",
    "11:10", "11:15", "11:20", "11:25", "11:30",
]


def main():
    iv_store.init_db()
    runner = MomentumStrategyRunner()

    def _premarket():
        logger.info("=" * 60)
        logger.info("Momentum: premarket scan")
        logger.info("=" * 60)
        runner.run_premarket()

    def _intraday():
        logger.info("Momentum: intraday scan")
        runner.run_intraday_scan()

    def _daily_summary():
        logger.info("Momentum: daily summary")
        try:
            stats       = runner._journal.get_today_stats()
            risk_summary = runner.risk_manager.summary()
            runner._notifier.send_daily_summary(stats, risk_summary)
            runner.risk_manager.reset_daily()
        except Exception:
            logger.exception("Daily summary failed")

    schedule.clear()
    for day in WEEKDAYS:
        getattr(schedule.every(), day).at("09:00").do(_premarket)
        getattr(schedule.every(), day).at("15:15").do(_daily_summary)
        for t in INTRADAY_TIMES:
            getattr(schedule.every(), day).at(t).do(_intraday)

    logger.info("Momentum strategy scheduler started")
    if schedule.jobs:
        next_run = min(j.next_run for j in schedule.jobs if j.next_run)
        logger.info("Next run: %s", next_run.strftime("%Y-%m-%d %H:%M:%S"))

    while True:
        schedule.run_pending()
        time.sleep(15)


if __name__ == "__main__":
    main()
