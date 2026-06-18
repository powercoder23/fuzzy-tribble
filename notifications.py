"""
Shared alert delivery: Telegram primary, Discord fallback.

Every service in this repo posts the same kind of plain/HTML alert text. This
module centralises delivery so a single Discord webhook can act as a backup
channel whenever Telegram is unreachable or unconfigured.

Configuration (all from the environment, per-call overrides allowed):
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  — primary channel
    DISCORD_WEBHOOK_URL                     — a Discord channel "Incoming
                                              Webhook" URL used only as a fallback

`notify()` is the entry point: it tries Telegram first and only falls back to
Discord if Telegram fails or is not configured. Nothing here ever raises — a
failed alert must never take down a strategy.
"""

import html as _html
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

TELEGRAM_TIMEOUT = 10
DISCORD_TIMEOUT = 10
DISCORD_MAX_CHARS = 2000  # Discord rejects webhook content longer than this


def send_telegram(text, bot_token=None, chat_id=None, parse_mode="HTML"):
    """POST one message to Telegram. Returns True on success; never raises.

    Falls back to TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the environment
    when token/chat_id are not passed explicitly.
    """
    bot_token = bot_token if bot_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return False
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload,
            timeout=TELEGRAM_TIMEOUT,
        )
        if not resp.ok:
            logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
        return resp.ok
    except Exception:
        logger.warning("Telegram send exception", exc_info=True)
        return False


# Telegram-supported tags we map to Discord markdown; everything else is dropped.
_TAG_REPL = {"b": "**", "strong": "**", "i": "*", "em": "*", "u": "__",
             "code": "`", "pre": "```", "s": "~~", "strike": "~~"}
_KNOWN_TAG_RE = re.compile(r"</?(" + "|".join(_TAG_REPL) + r")>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_discord(text):
    """Best-effort Telegram-HTML -> Discord-markdown conversion.

    Plain text (no tags/entities) passes through unchanged.
    """
    out = _KNOWN_TAG_RE.sub(lambda m: _TAG_REPL[m.group(1).lower()], text)
    out = _ANY_TAG_RE.sub("", out)   # strip anything left (e.g. <a href=...>)
    return _html.unescape(out)       # &amp; -> &, &lt; -> < , ...


def _chunk(text, limit=DISCORD_MAX_CHARS):
    """Split text into <=limit pieces, preferring newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks, buf = [], ""
    for line in text.split("\n"):
        while len(line) > limit:          # a single overlong line: hard-split
            chunks.append(line[:limit])
            line = line[limit:]
        if buf and len(buf) + 1 + len(line) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = line if not buf else f"{buf}\n{line}"
    if buf:
        chunks.append(buf)
    return chunks


def send_discord(text, webhook_url=None, convert_html=True):
    """POST a message to a Discord channel webhook. Returns True; never raises.

    Falls back to DISCORD_WEBHOOK_URL from the environment. Long messages are
    split into <=2000-char chunks (Discord's per-message limit).
    """
    webhook_url = webhook_url if webhook_url is not None else os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return False
    body = _html_to_discord(text) if convert_html else text
    try:
        for chunk in _chunk(body):
            resp = requests.post(webhook_url, json={"content": chunk}, timeout=DISCORD_TIMEOUT)
            if not resp.ok:  # Discord returns 204 on success
                logger.warning("Discord send failed: %s %s", resp.status_code, resp.text[:200])
                return False
        return True
    except Exception:
        logger.warning("Discord send exception", exc_info=True)
        return False


def notify(text, *, bot_token=None, chat_id=None, webhook_url=None, parse_mode="HTML"):
    """Deliver `text`, Telegram first and Discord as fallback.

    Discord fires only when Telegram fails or is not configured. Returns True if
    any channel accepted the message.
    """
    if send_telegram(text, bot_token=bot_token, chat_id=chat_id, parse_mode=parse_mode):
        return True
    if send_discord(text, webhook_url=webhook_url, convert_html=bool(parse_mode)):
        logger.info("Alert delivered via Discord fallback")
        return True
    return False
