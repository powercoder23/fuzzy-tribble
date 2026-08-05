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
  09:35–15:10  run_monitor every MOMENTUM_MONITOR_INTERVAL_MIN (shared book)
  15:15        run_square_off + daily summary Telegram
  15:20        run_paper_eod (shared-book realized P&L)

Scanning stops at 11:30 but positions live until square-off, so the monitor
runs on its own cadence long after the last scan. Without it a booked trade
would never fill, stop out or hit a target. Skipped entirely when
MOMENTUM_PAPER_MODE=off.
"""

import logging
import os
import time
from datetime import datetime, timedelta, time as dt_time

import pytz
import schedule

from collectors import iv_store
from config import Config
from momentum_config import PAPER
from momentum_strategy import MomentumStrategyRunner, interval_times

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

    def _monitor():
        try:
            runner.run_monitor()
        except Exception:
            logger.exception("Momentum monitor failed")

    def _square_off():
        logger.info("Momentum: square-off")
        try:
            runner.run_square_off()
        except Exception:
            logger.exception("Momentum square-off failed")

    def _paper_eod():
        logger.info("Momentum: paper-book EOD")
        try:
            runner.run_paper_eod()
        except Exception:
            logger.exception("Momentum paper EOD failed")

    def _daily_summary():
        logger.info("Momentum: daily summary")
        try:
            stats       = runner._journal.get_today_stats()
            risk_summary = runner.risk_manager.summary()
            runner._notifier.send_daily_summary(stats, risk_summary)
            runner.risk_manager.reset_daily()
        except Exception:
            logger.exception("Daily summary failed")

    paper_on = PAPER["mode"] == "paper"
    monitor_times = interval_times(
        PAPER["monitor_from"], PAPER["monitor_until"],
        PAPER["monitor_interval_min"]) if paper_on else []

    schedule.clear()
    for day in WEEKDAYS:
        getattr(schedule.every(), day).at("09:00").do(_premarket)
        for t in INTRADAY_TIMES:
            getattr(schedule.every(), day).at(t).do(_intraday)
        for t in monitor_times:
            getattr(schedule.every(), day).at(t).do(_monitor)
        if paper_on:
            # Square-off BEFORE the EOD summary, or the summary reports
            # positions that are about to be closed as still open.
            getattr(schedule.every(), day).at(PAPER["square_off"]).do(_square_off)
            getattr(schedule.every(), day).at(PAPER["eod_summary_at"]).do(_paper_eod)
        getattr(schedule.every(), day).at("15:15").do(_daily_summary)

    logger.info("Momentum strategy scheduler started | paper=%s | monitor %s..%s every %dm (%d slots)",
                PAPER["mode"], PAPER["monitor_from"], PAPER["monitor_until"],
                PAPER["monitor_interval_min"], len(monitor_times))
    if schedule.jobs:
        next_run = min(j.next_run for j in schedule.jobs if j.next_run)
        logger.info("Next run: %s", next_run.strftime("%Y-%m-%d %H:%M:%S"))

    # If we started late (after 9:00 AM), premarket slot is already gone.
    # Run it immediately so intraday scans have affordability + regime data.
    now = datetime.now().time()
    if dt_time(9, 0) <= now <= dt_time(11, 30) and datetime.now().weekday() < 5:
        logger.info("Service started mid-session — running premarket immediately")
        _premarket()

    while True:
        schedule.run_pending()
        time.sleep(15)


if __name__ == "__main__":
    main()
