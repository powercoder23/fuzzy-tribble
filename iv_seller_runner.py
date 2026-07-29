#!/usr/bin/env python3
"""IV Seller Strategy Runner — Service: iv-seller.

Sells option premium (short strangle / short straddle) on names whose IV is
rich relative to their own recent history, using collected IV
(collectors/iv_store.py -> iv_history.db) rather than buying it.

Scan cadence mirrors directional_iv_runner.py. Booking follows the same
"shared OrderManager" pattern break_bounce_strategy.py already uses: this
runner keeps its own selection (IV percentile, structure, strikes) and its own
daily/per-symbol combo caps, then hands each leg to
OrderManager.submit_external_signal so it lands in the same paper_trades.db,
monitor/exit cadence, EOD summary, and dashboard as every other strategy.

Monitoring/exit-management is NOT run from this process: the discount
container's run_monitor_cycle (every INTRADAY["monitor_interval_min"]) already
re-prices every OPEN row in paper_trades.db regardless of which strategy
opened it (confirmed: break_bounce_strategy.py relies on the same shared
cadence and only adds its own EOD square-off as a backstop, which this runner
mirrors below).
"""

import logging
import os
import time
import uuid
from datetime import datetime, time as dt_time

import pytz

from config import Config
from collectors import iv_store
from discount_config import INTRADAY
import iv_seller_config as cfg
from iv_seller_strategy import IVSellerScanner

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
        logging.FileHandler(Config.LOGS_DIR / "iv_seller.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
SCAN_TIMES = ["09:45", "11:15", "13:15"]
EOD_TIME = "15:15"


class IVSellerRunner:
    def __init__(self):
        self.scanner = IVSellerScanner()
        self._order_manager = None
        self._lot_fn = None

    def _om(self):
        if self._order_manager is None:
            import order_manager
            self._order_manager = order_manager.OrderManager()
        return self._order_manager

    def lot_fn(self):
        if self._lot_fn is None:
            try:
                from momentum_strategy import ScripMasterLotSizer
                sizer = ScripMasterLotSizer()
                self._lot_fn = sizer.get
            except Exception:
                logger.exception("Lot sizer unavailable; iv-seller falls back to 1x lot")
                self._lot_fn = (lambda _s: 1)
        return self._lot_fn

    def _combo_caps_ok(self, combos_booked_today, symbol_booked_today, symbol):
        if combos_booked_today >= cfg.MAX_COMBOS_PER_DAY:
            return False
        if symbol_booked_today.get(symbol, 0) >= cfg.MAX_COMBOS_PER_SYMBOL_PER_DAY:
            return False
        return True

    def run_scan(self):
        now = datetime.now()
        if now.strftime("%H:%M") >= INTRADAY["no_entry_after"]:
            logger.info("Past entry cutoff %s — no new IV-seller combos", INTRADAY["no_entry_after"])
            return

        logger.info("=" * 70)
        logger.info("IV Seller scan (find + book short strangle/straddle combos)")
        logger.info("=" * 70)
        try:
            combos = self.scanner.scan_all_underlyings()
        except Exception:
            logger.exception("IV seller scan failed")
            return

        if not combos:
            logger.info("IV seller: no qualifying combos this cycle")
            return

        om = self._om()
        lot_fn = self.lot_fn()
        combos_booked_today = 0
        symbol_booked_today: dict = {}

        for combo in combos:
            if not self._combo_caps_ok(combos_booked_today, symbol_booked_today, combo["symbol"]):
                logger.info("IV seller: cap reached — skipping remaining combos")
                break

            combo_id = f"{combo['symbol']}_{now.date().isoformat()}_{uuid.uuid4().hex[:8]}"
            lot = lot_fn(combo["symbol"]) or 1
            booked_legs = 0
            for leg in combo["legs"]:
                sig = dict(leg)
                sig["lot_size"] = lot
                sig["combo_id"] = combo_id
                try:
                    booked = om.submit_external_signal(sig, now=now)
                except Exception:
                    logger.exception("IV seller: booking failed for %s %s", combo["symbol"], leg["side"])
                    booked = None
                if booked:
                    booked_legs += 1
                    logger.info("IV seller: booked %s %s %s @ %.2f [%s] combo=%s",
                                combo["symbol"], leg["side"], leg["strike"], leg["entry"],
                                combo["structure"], combo_id)
                else:
                    logger.info("IV seller: leg not booked (gate/guard) %s %s", combo["symbol"], leg["side"])

            if booked_legs:
                combos_booked_today += 1
                symbol_booked_today[combo["symbol"]] = symbol_booked_today.get(combo["symbol"], 0) + 1

    def run_eod(self):
        """Backstop square-off, mirroring break_bounce_strategy.run_eod — the
        discount container's own 15:20 square-off is the primary backstop; this
        just closes any iv-seller legs slightly earlier in case that container
        is unavailable."""
        logger.info("IV Seller EOD (%s): closing any open combos", EOD_TIME)
        try:
            om = self._om()
            om.square_off_all(self.scanner.scanner, now=datetime.now())
        except Exception:
            logger.exception("IV seller EOD square-off failed (non-fatal)")


def main():
    import schedule

    iv_store.init_db()
    runner = IVSellerRunner()

    schedule.clear()
    for day in WEEKDAYS:
        for t in SCAN_TIMES:
            getattr(schedule.every(), day).at(t).do(runner.run_scan)
        getattr(schedule.every(), day).at(EOD_TIME).do(runner.run_eod)

    logger.info("IV Seller strategy scheduler started")
    if schedule.jobs:
        next_run = min(j.next_run for j in schedule.jobs if j.next_run)
        logger.info("Next run: %s", next_run.strftime("%Y-%m-%d %H:%M:%S"))

    now = datetime.now().time()
    if dt_time(9, 45) <= now <= dt_time(15, 0) and datetime.now().weekday() < 5:
        logger.info("Service started mid-session — running scan immediately")
        runner.run_scan()

    while True:
        schedule.run_pending()
        time.sleep(15)


if __name__ == "__main__":
    main()
