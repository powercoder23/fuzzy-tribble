"""
Backfill delivery_daily with historical NSE bhavcopy data, far beyond the
2026-06-05 start of live collection.

Why this is safe to run against NSE's servers:
  - One CSV per trading day covers ALL symbols — a full year of backfill is
    only ~250 requests total, not per-symbol. This is not a scrape, it's a
    slow trickle of daily archive downloads.
  - Reuses collectors.bhav_collector.BhavCollector unchanged — same
    session-priming (hit nseindia.com homepage first, like a browser would)
    and same User-Agent/Referer headers already proven safe by the live
    daily collector.
  - Resumable and idempotent: BhavCollector._already_saved() skips any date
    already in delivery_daily WITHOUT making a request, so stopping and
    re-running only ever fetches the gaps.

Extra caution added on top of BhavCollector for a multi-month backfill
(the live collector only ever does ONE request per run, so it never needed
this):
  - Skips Sat/Sun client-side (no request sent).
  - Randomized delay between requests (default 5-12s) — no bursts.
  - A longer rest every N requests (default: 60s every 20 requests).
  - Exponential backoff on failure (30s/60s/120s), and the whole run stops
    itself after too many consecutive failures instead of grinding through
    a dead date range (e.g. once history runs off the end of what NSE's
    archive retains).

Usage:
    python -m research.backfill_bhavcopy --start 2025-01-01
    python -m research.backfill_bhavcopy --start 2025-01-01 --end 2026-06-04
"""

import argparse
import logging
import random
import time
from datetime import date, timedelta

from collectors.bhav_collector import BhavCollector, BhavDataNotReady
from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_bhavcopy")

MIN_DELAY_S = 5
MAX_DELAY_S = 12
REST_EVERY = 20
REST_SECONDS = 60
MAX_CONSECUTIVE_FAILURES = 5
BACKOFF_SCHEDULE = [30, 60, 120]


def daterange_desc(start: date, end: date):
    """Yield dates from end back to start (newest first), Mon-Fri only."""
    d = end
    while d >= start:
        if d.weekday() < 5:  # 0=Mon .. 4=Fri
            yield d
        d -= timedelta(days=1)


def fetch_with_backoff(collector: BhavCollector, d: date) -> str:
    """Returns one of: 'saved', 'skipped_already_saved', 'no_data', 'failed'."""
    if collector._already_saved(d):
        return "skipped_already_saved"

    last_exc = None
    for attempt, backoff in enumerate([0] + BACKOFF_SCHEDULE):
        if backoff:
            logger.warning("  retry %d/%d for %s after %ds backoff",
                            attempt, len(BACKOFF_SCHEDULE), d.isoformat(), backoff)
            time.sleep(backoff)
        try:
            df = collector.fetch(d)
            inserted, total = collector.save(df, d)
            logger.info("  %s: inserted %d/%d rows", d.isoformat(), inserted, total)
            return "saved"
        except BhavDataNotReady:
            logger.info("  %s: no delivery data (holiday / weekend-adjacent) — skipping", d.isoformat())
            return "no_data"
        except Exception as exc:
            last_exc = exc
            continue

    logger.error("  %s: failed after retries (%s)", d.isoformat(), last_exc)
    return "failed"


def run(start: date, end: date) -> None:
    collector = BhavCollector(Config)
    collector._init_table()

    counts = {"saved": 0, "skipped_already_saved": 0, "no_data": 0, "failed": 0}
    consecutive_failures = 0
    requests_made = 0

    for d in daterange_desc(start, end):
        result = fetch_with_backoff(collector, d)
        counts[result] += 1

        if result == "failed":
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "Aborting: %d consecutive failures — likely past NSE archive "
                    "retention or a real block. Stopped at %s.",
                    consecutive_failures, d.isoformat(),
                )
                break
        else:
            consecutive_failures = 0

        if result != "skipped_already_saved":
            requests_made += 1
            if requests_made % REST_EVERY == 0:
                logger.info("... resting %ds after %d requests", REST_SECONDS, requests_made)
                time.sleep(REST_SECONDS)
            else:
                time.sleep(random.uniform(MIN_DELAY_S, MAX_DELAY_S))

    logger.info("Backfill done. %s", counts)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD, oldest date to backfill")
    ap.add_argument("--end", default=None,
                     help="YYYY-MM-DD, newest date to backfill (default: 2026-06-04, "
                          "the day before live collection started)")
    args = ap.parse_args()

    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end) if args.end else date(2026, 6, 4)
    run(start_d, end_d)
