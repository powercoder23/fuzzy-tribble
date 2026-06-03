"""
Lightweight Telegram notifier shared by the bhav / deals / vix collectors.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the environment (same as the
IV collector service). No-ops with a log line if either is missing, so it is
always safe to call.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)


def send_telegram(text: str) -> bool:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.info("Telegram not configured — collector confirmation not sent")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram collector send failed: %s", resp.text[:200])
        return resp.ok
    except Exception:
        logger.exception("Telegram collector send exception")
        return False
