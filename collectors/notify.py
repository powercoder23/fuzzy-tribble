"""
Lightweight alert helper shared by the bhav / deals / vix collectors.

Delegates to the shared `notifications` module: Telegram primary
(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID), Discord fallback (DISCORD_WEBHOOK_URL).
No-ops with a log line if no channel is configured, so it is always safe to call.
"""

import logging

import notifications

logger = logging.getLogger(__name__)


def send_telegram(text: str) -> bool:
    if notifications.notify(text):
        return True
    logger.info("No alert channel configured — collector confirmation not sent")
    return False
