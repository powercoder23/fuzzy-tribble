# -*- coding: utf-8 -*-
"""
vol_expansion_runner.py — service wrapper for the Volatility-Expansion paper
strategy (dashboard section II: 4-day IV-slope expansion).

Schedule (IST):
  SCAN_TIMES              run_scan — find + book buy-zone expansion trades
  every MONITOR_INTERVAL  re-price + exit-manage OPEN positions (shared book)
  SQUARE_OFF (15:20)      force-close any open positions
  EOD_SUMMARY_AT          realized-P&L summary

Books through the SHARED OrderManager, so it self-manages its trades even when
the discount container is down (unlike relying on main.py's monitor alone).
No real orders are ever placed — paper only.
"""
import logging
import time
from datetime import datetime, timedelta

import schedule

import vol_expansion_config as CFG
from vol_expansion_strategy import VolExpansionStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("vol_expansion_runner")

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def _interval_times(start: str, end: str, minutes: int) -> list:
    out, cur = [], datetime.strptime(start, "%H:%M")
    end_dt = datetime.strptime(end, "%H:%M")
    while cur <= end_dt:
        out.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=minutes)
    return out


class VolExpansionRunner:
    def __init__(self):
        self.strategy = None

    def _ensure(self):
        if self.strategy is None:
            self.strategy = VolExpansionStrategy()
            logger.info("VolExpansionRunner initialised (MODE=%s, buy_zone_only=%s)",
                        CFG.MODE, CFG.BUY_ZONE_ONLY)

    def run_scan(self):
        try:
            self._ensure()
            booked = self.strategy.run_scan(now=datetime.now())
            if booked:
                logger.info("VolExp scan: %d signal(s) %s", len(booked),
                            "booked" if CFG.MODE == "paper" else "alerted")
        except Exception:
            logger.exception("VolExp run_scan failed")

    def run_monitor(self):
        try:
            self._ensure()
            self.strategy.order_manager.track(self.strategy.scanner, now=datetime.now())
        except Exception:
            logger.exception("VolExp monitor failed")

    def run_square_off(self):
        try:
            self._ensure()
            self.strategy.order_manager.square_off_all(self.strategy.scanner, now=datetime.now())
        except Exception:
            logger.exception("VolExp square-off failed")

    def run_eod(self):
        try:
            self._ensure()
            self.strategy.order_manager.eod(self.strategy.scanner, now=datetime.now())
        except Exception:
            logger.exception("VolExp EOD failed")


def main():
    if CFG.MODE == "off":
        logger.info("VOL_EXP_MODE=off — strategy disabled; runner idling.")
    runner = VolExpansionRunner()
    monitor_times = _interval_times(CFG.SCAN_TIMES[0].strip(), CFG.MONITOR_UNTIL,
                                    CFG.MONITOR_INTERVAL_MIN)

    schedule.clear()
    for day in WEEKDAYS:
        for t in CFG.SCAN_TIMES:
            getattr(schedule.every(), day).at(t.strip()).do(runner.run_scan)
        for t in monitor_times:
            getattr(schedule.every(), day).at(t).do(runner.run_monitor)
        getattr(schedule.every(), day).at(CFG.SQUARE_OFF).do(runner.run_square_off)
        getattr(schedule.every(), day).at(CFG.EOD_SUMMARY_AT).do(runner.run_eod)

    logger.info("VolExpansion scheduled | scans=%s | monitor every %dm until %s",
                CFG.SCAN_TIMES, CFG.MONITOR_INTERVAL_MIN, CFG.MONITOR_UNTIL)
    while True:
        schedule.run_pending()
        time.sleep(20)


if __name__ == "__main__":
    main()
