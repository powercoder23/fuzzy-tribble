#!/usr/bin/env python3
"""
Runs at container startup before any service.
Ensures a valid Upstox access token exists on the shared data volume
so all services can call load_upstox_token() without triggering Selenium.
"""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _refresh_instruments_if_needed():
    """Refresh the Upstox instruments DB (complete.db) once per calendar day."""
    import os
    from pathlib import Path
    db = Path(__file__).parent / "data" / "complete.db"
    flag = Path(__file__).parent / "data" / ".instruments_refreshed_date"
    today = __import__("datetime").date.today().isoformat()
    if flag.exists() and flag.read_text().strip() == today:
        logger.info("Upstox instruments DB already refreshed today.")
        return
    try:
        from complete_json_tosqlite import run as refresh_instruments
        refresh_instruments()
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(today)
        logger.info("Upstox instruments DB refreshed.")
    except Exception as exc:
        logger.warning("Instruments DB refresh failed (non-fatal): %s", exc)


def main():
    import os
    if os.getenv("DATA_PROVIDER", "dhan").lower() != "upstox":
        logger.info("DATA_PROVIDER is not 'upstox' — skipping Upstox init.")
        return

    _refresh_instruments_if_needed()

    try:
        from upstox_token_manager import load_upstox_token
        token = load_upstox_token()
        logger.info("Upstox token ready (length=%d).", len(token))
    except Exception as e:
        logger.error("Upstox token init failed: %s", e)
        # Do not block the service — log and continue.
        sys.exit(0)


if __name__ == "__main__":
    main()
