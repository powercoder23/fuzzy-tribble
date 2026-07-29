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
from collectors import iv_store
from directional_iv_config import OUTPUT_CSV
from directional_iv_strategy import DirectionalIVScanner
from order_manager import OrderManager

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

# How many top directional-IV candidates to submit as paper trades per scan.
MAX_PAPER_TRADES_PER_SCAN = int(os.getenv("DIV_MAX_PAPER_PER_SCAN", "3"))


def _make_signal(row: dict) -> dict:
    """Map a directional-IV candidate row to the paper-trade signal shape."""
    return {
        "symbol":               row.get("symbol"),
        "security_id":          str(row.get("security_id", "")),
        "exchange_segment":     row.get("exchange_segment", "NSE_FNO"),
        "side":                 row.get("type"),          # "CALL" or "PUT"
        "strike":               row.get("strike"),
        "expiry":               row.get("expiry"),
        "spot":                 row.get("spot"),
        "entry":                row.get("entry"),
        "sl":                   row.get("stop_loss"),
        "t1":                   row.get("target"),
        "t2":                   row.get("target"),
        "t1_book_fraction":     1.0,
        "iv":                   row.get("iv"),
        "hv":                   row.get("hv"),
        "iv_rank":              row.get("iv_rank"),
        "oi":                   row.get("oi"),
        "volume":               row.get("volume"),
        "score":                row.get("score"),
        "dte":                  row.get("expiry_dte"),
        "bid":                  row.get("bid"),
        "ask":                  row.get("ask"),
        "strategy":             "Directional IV",
        # Directional IV has its own delta/IV/trend gates — skip the
        # discount buyer-quality gate and the shared ₹1500 risk cap
        # (delta-tiered sizing in directional_iv_config handles risk).
        "skip_risk_cap":        True,
        "skip_pre_market_gate": True,
    }


def run_directional_scan():
    iv_store.init_db()
    scanner = DirectionalIVScanner()
    om = OrderManager()

    logger.info("Directional IV scan starting")
    opportunities = scanner.scan_all_underlyings()
    scanner.generate_report(opportunities)

    if not opportunities.empty:
        Path(OUTPUT_CSV).parent.mkdir(parents=True, exist_ok=True)
        opportunities.to_csv(OUTPUT_CSV, index=False)
        logger.info("Results saved to %s", OUTPUT_CSV)

    scanner.send_telegram_summary(opportunities)

    # Paper-trade the top candidates as debit spreads.
    if not opportunities.empty:
        rows = opportunities.to_dict("records")
        submitted = 0
        for row in rows:
            if submitted >= MAX_PAPER_TRADES_PER_SCAN:
                break
            sig = _make_signal(row)
            if not sig.get("symbol") or not sig.get("entry") or not sig.get("t1"):
                continue
            chain_data = getattr(scanner, "_chain_cache", {}).get(
                str(row.get("security_id", "")))
            booked = om.submit_with_hedge(sig, chain_data=chain_data)
            if booked:
                submitted += 1
                logger.info("Directional IV: booked %d leg(s) for %s %s K%.0f",
                            len(booked), sig["symbol"], sig["side"],
                            float(sig.get("strike") or 0))

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
