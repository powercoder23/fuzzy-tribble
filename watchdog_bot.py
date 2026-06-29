# -*- coding: utf-8 -*-
"""
watchdog_bot.py — Log watchdog + Claude-powered Discord bot.

Two jobs in one service:

1. WATCHDOG
   Tails all trading containers via Docker SDK. Any line containing
   ERROR / CRITICAL / Traceback / Exception fires a Discord alert.
   Same error from the same container is suppressed for 15 minutes
   to avoid spam. Daily health summary at 08:50 IST.

2. DISCORD BOT
   Listens for @mentions in your Discord server. Routes built-in
   commands (status, trades, iv, scan, errors) or passes free-form
   questions to the Claude API with live context from the DBs.

Environment variables required
──────────────────────────────
DISCORD_BOT_TOKEN    — from Discord Developer Portal
DISCORD_CHANNEL_ID   — channel ID for watchdog alerts (right-click → Copy ID)
ANTHROPIC_API_KEY    — from console.anthropic.com
DATA_DIR             — path to data dir (default: data)

Optional
────────
WATCHDOG_CONTAINERS  — comma-separated container names (default: all 10)
WATCHDOG_DEBOUNCE    — seconds to suppress duplicate errors (default: 900)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import discord
import docker
import requests
import schedule

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("watchdog_bot")

# ── Config ────────────────────────────────────────────────────────────────── #
DISCORD_BOT_TOKEN   = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID  = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
DATA_DIR            = Path(os.getenv("DATA_DIR", "data"))
DEBOUNCE_SEC        = int(os.getenv("WATCHDOG_DEBOUNCE", "900"))

IV_DB = DATA_DIR / "iv_history.db"
PT_DB = DATA_DIR / "paper_trades.db"

ALL_CONTAINERS = [
    "iv-collector", "discount-strategy", "break-bounce-strategy",
    "iv-rank-scanner", "oi-buildup-scanner", "gap-scanner",
    "delivery-surge-scanner", "smart-money-scanner",
    "sonar-scanner", "composite-scanner",
]
WATCH_CONTAINERS = [
    c.strip() for c in
    os.getenv("WATCHDOG_CONTAINERS", ",".join(ALL_CONTAINERS)).split(",")
    if c.strip()
]

ERROR_PATTERNS = ("ERROR", "CRITICAL", "Traceback", "Exception", "FATAL")

# ── DB helpers ────────────────────────────────────────────────────────────── #
def _query(db: Path, sql: str, params=()) -> list[dict]:
    if not db.exists():
        return []
    try:
        with sqlite3.connect(db, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception as e:
        logger.debug("DB query failed: %s", e)
        return []


def _scalar(db: Path, sql: str, params=(), default=None):
    rows = _query(db, sql, params)
    return rows[0][list(rows[0].keys())[0]] if rows else default


# ── Context builder — feeds Claude ───────────────────────────────────────── #
class ContextBuilder:
    """Reads live data from all DBs and returns a compact context string."""

    def build(self, include_errors: list[str] | None = None) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        now   = datetime.now().strftime("%Y-%m-%d %H:%M IST")
        parts = [f"System time: {now}", f"Trading date: {today}"]

        # Paper trades today
        trades = _query(PT_DB, """
            SELECT symbol, side, strike, entry, last_price,
                   realized_pct, realized_rupees, status, exit_reason,
                   opened_at, score, iv, hv, iv_rank
            FROM   paper_trades WHERE date=?
            ORDER  BY opened_at
        """, (today,))
        if trades:
            parts.append(f"\nPaper trades today ({len(trades)}):")
            for t in trades:
                pnl = f"₹{t['realized_rupees']:+.0f} ({t['realized_pct']:+.1f}%)" \
                      if t.get("realized_rupees") is not None else "open"
                parts.append(
                    f"  {t['symbol']} {t['side']} {t['strike']} "
                    f"entry={t['entry']} status={t['status']} pnl={pnl} "
                    f"IVR={t.get('iv_rank')} IV/HV={t.get('iv')}/{t.get('hv')}"
                )
        else:
            parts.append("\nNo paper trades today yet.")

        # 30-day summary
        summary = _query(PT_DB, """
            SELECT COUNT(*) total,
                   SUM(CASE WHEN realized_rupees>0 THEN 1 ELSE 0 END) wins,
                   ROUND(SUM(realized_rupees),0) net_pnl
            FROM   paper_trades
            WHERE  date >= date('now','-30 days') AND status='closed'
        """)
        if summary and summary[0]["total"]:
            s = summary[0]
            wr = round(s["wins"] / s["total"] * 100, 1) if s["total"] else 0
            parts.append(f"\n30-day paper summary: {s['total']} trades | "
                         f"win rate {wr}% | net ₹{s['net_pnl']:+,.0f}")

        # Latest OI buildup signals
        oi = _query(IV_DB, """
            SELECT symbol, bias, classification, price_chg_pct, oi_chg_pct, timestamp
            FROM   oi_buildup_history
            WHERE  date(timestamp,'localtime') = date('now','localtime')
            ORDER  BY timestamp DESC LIMIT 20
        """)
        if oi:
            longs  = [r for r in oi if r["bias"] == "CE"][:6]
            shorts = [r for r in oi if r["bias"] == "PE"][:6]
            if longs:
                parts.append("\nOI long buildup: " +
                    ", ".join(f"{r['symbol']}(px{r['price_chg_pct']:+.1f}%)" for r in longs))
            if shorts:
                parts.append("OI short buildup: " +
                    ", ".join(f"{r['symbol']}(px{r['price_chg_pct']:+.1f}%)" for r in shorts))

        # Latest Sonar signals
        sonar = _query(IV_DB, """
            SELECT symbol, signal, bias, last_price, support, resistance, timestamp
            FROM   sonar_history
            WHERE  date(timestamp,'localtime') = date('now','localtime')
              AND  signal NOT IN ('NONE','')
            ORDER  BY timestamp DESC LIMIT 12
        """)
        if sonar:
            parts.append("\nLatest Sonar signals:")
            for r in sonar:
                parts.append(f"  {r['symbol']} {r['signal']} {r['bias']} "
                             f"last={r['last_price']} S={r['support']} R={r['resistance']}")

        # Recent errors
        if include_errors:
            parts.append("\nRecent errors from watchdog:")
            for e in include_errors[-10:]:
                parts.append(f"  {e}")

        return "\n".join(parts)

    def symbol_context(self, symbol: str) -> str:
        symbol = symbol.upper()
        today  = datetime.now().strftime("%Y-%m-%d")
        parts  = [f"Symbol: {symbol}  Date: {today}"]

        # IV snapshot
        snap = _query(IV_DB, """
            SELECT atm_iv, spot_price, total_call_oi, total_put_oi, timestamp
            FROM   iv_history
            WHERE  symbol=? AND data_type='intraday'
            ORDER  BY timestamp DESC LIMIT 1
        """, (symbol,))
        if snap:
            r   = snap[0]
            pcr = (r["total_put_oi"] / r["total_call_oi"]
                   if r["total_call_oi"] else None)
            pcr_str = f"{pcr:.2f}" if pcr else "N/A"
            parts.append(f"Latest IV: {r['atm_iv']:.1f}%  "
                         f"Spot: ₹{r['spot_price']:.2f}  "
                         f"PCR: {pcr_str}")

        # IVR
        hist = _query(IV_DB, """
            SELECT atm_iv FROM iv_history
            WHERE  symbol=? AND data_type='daily' AND atm_iv BETWEEN 1 AND 200
            ORDER  BY timestamp DESC LIMIT 252
        """, (symbol,))
        if hist:
            vals   = [r["atm_iv"] for r in hist]
            iv_min = min(vals); iv_max = max(vals)
            cur    = vals[0] if vals else None
            rank   = round((cur - iv_min) / (iv_max - iv_min) * 100, 1) \
                     if cur and iv_max > iv_min else None
            parts.append(f"IVR: {rank}  52W range: {iv_min:.1f}–{iv_max:.1f}%  "
                         f"({len(vals)} samples)")

        # OI buildup
        oi = _query(IV_DB, """
            SELECT bias, classification, price_chg_pct, oi_chg_pct, timestamp
            FROM   oi_buildup_history
            WHERE  symbol=? AND date(timestamp,'localtime')=date('now','localtime')
            ORDER  BY timestamp DESC LIMIT 3
        """, (symbol,))
        if oi:
            parts.append("OI buildup today: " +
                " → ".join(f"{r['classification']}(bias={r['bias']})" for r in oi))

        # Sonar
        sonar = _query(IV_DB, """
            SELECT signal, bias, last_price, support, resistance, timestamp
            FROM   sonar_history
            WHERE  symbol=? AND date(timestamp,'localtime')=date('now','localtime')
            ORDER  BY timestamp DESC LIMIT 3
        """, (symbol,))
        if sonar:
            parts.append("Sonar today: " +
                " → ".join(f"{r['signal']}({r['bias']})" for r in sonar))

        return "\n".join(parts)


# ── Claude caller ─────────────────────────────────────────────────────────── #
SYSTEM_PROMPT = """You are the trading assistant for fuzzy-tribble — an automated NSE F&O
options paper trading system running on a NAS. You help the trader understand scanner outputs,
diagnose errors, and analyse trade quality.

Be concise and direct — this is a Discord chat. Use plain text, no markdown headers.
Max 400 words per response. Lead with the key insight, then supporting detail.
When analyzing trades, always reference IV/HV ratio, OTM%, OI direction, and Sonar.
If asked about an error, explain what broke and give the fix command."""

def ask_claude(query: str, context: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "ANTHROPIC_API_KEY not set — cannot reach Claude."
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 600,
                "system": SYSTEM_PROMPT,
                "messages": [{
                    "role": "user",
                    "content": f"LIVE CONTEXT:\n{context}\n\nQUESTION: {query}",
                }],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]
    except Exception as e:
        logger.error("Claude API error: %s", e)
        return f"Claude API error: {e}"


# ── Log watchdog ──────────────────────────────────────────────────────────── #
class LogWatchdog:
    def __init__(self, bot: "TradingBot"):
        self.bot      = bot
        self._docker  = None
        self._last    : dict[str, float] = defaultdict(float)   # dedup
        self._errors  : list[str] = []                          # for Claude context
        self._threads : list[threading.Thread] = []

    def start(self):
        try:
            self._docker = docker.from_env()
            logger.info("Docker SDK connected")
        except Exception as e:
            logger.error("Docker SDK unavailable: %s — watchdog disabled", e)
            return

        for name in WATCH_CONTAINERS:
            t = threading.Thread(target=self._watch, args=(name,),
                                 daemon=True, name=f"watch-{name}")
            t.start()
            self._threads.append(t)
        logger.info("Watchdog watching %d containers", len(WATCH_CONTAINERS))

    def _watch(self, container_name: str):
        while True:
            try:
                container = self._docker.containers.get(container_name)
                logger.info("Tailing logs: %s", container_name)
                for raw in container.logs(stream=True, follow=True, tail=0):
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if any(p in line for p in ERROR_PATTERNS):
                        self._handle_error(container_name, line)
            except docker.errors.NotFound:
                logger.warning("Container not found: %s — retry in 60s", container_name)
                time.sleep(60)
            except Exception as e:
                logger.error("Watch error on %s: %s — retry in 30s", container_name, e)
                time.sleep(30)

    def _handle_error(self, container: str, line: str):
        key = f"{container}:{line[:80]}"
        now = time.monotonic()
        if now - self._last[key] < DEBOUNCE_SEC:
            return
        self._last[key] = now

        ts  = datetime.now().strftime("%H:%M:%S")
        msg = f"🚨 **{container}** `{ts}`\n```{line[:300]}```"
        self._errors.append(f"[{ts}] {container}: {line[:120]}")
        if len(self._errors) > 50:
            self._errors = self._errors[-50:]

        asyncio.run_coroutine_threadsafe(
            self.bot.send_alert(msg), self.bot.loop
        )

    def recent_errors(self) -> list[str]:
        return list(self._errors)


# ── Discord bot ───────────────────────────────────────────────────────────── #
class TradingBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.ctx     = ContextBuilder()
        self.watchdog: LogWatchdog | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    async def on_ready(self):
        self.loop = asyncio.get_event_loop()
        logger.info("Discord bot ready as %s", self.user)
        await self.send_alert(
            f"✅ **fuzzy-tribble watchdog online** — "
            f"watching {len(WATCH_CONTAINERS)} containers | "
            f"{datetime.now().strftime('%H:%M IST')}"
        )

    async def send_alert(self, text: str):
        if not DISCORD_CHANNEL_ID:
            return
        try:
            channel = self.get_channel(DISCORD_CHANNEL_ID)
            if channel:
                await channel.send(text[:1990])
        except Exception as e:
            logger.error("Discord send failed: %s", e)

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if self.user not in message.mentions:
            return

        # Strip the @mention to get clean query
        query = message.content
        for mention in [f"<@{self.user.id}>", f"<@!{self.user.id}>"]:
            query = query.replace(mention, "").strip()

        if not query:
            await message.reply(
                "**Commands:**\n"
                "`status` — health check all containers\n"
                "`trades` — today's paper trades\n"
                "`iv SYMBOL` — IV rank and stats\n"
                "`scan SYMBOL` — all signals for a stock\n"
                "`errors` — recent errors\n"
                "Or just ask me anything about the scanners."
            )
            return

        cmd  = query.lower().split()[0]
        args = query.split()[1:]

        async with message.channel.typing():
            if cmd == "status":
                await message.reply(await self._status())

            elif cmd == "trades":
                await message.reply(self._trades_summary())

            elif cmd in ("iv", "scan") and args:
                symbol = args[0].upper()
                ctx    = self.ctx.symbol_context(symbol)
                reply  = await asyncio.to_thread(
                    ask_claude, f"Give me a full analysis for {symbol}", ctx
                )
                await message.reply(f"**{symbol}**\n{reply}")

            elif cmd == "errors":
                errs = self.watchdog.recent_errors() if self.watchdog else []
                if errs:
                    text = "\n".join(errs[-10:])
                    await message.reply(f"**Recent errors:**\n```{text[:1800]}```")
                else:
                    await message.reply("✅ No errors recorded since startup.")

            else:
                # Free-form → Claude
                errors = self.watchdog.recent_errors() if self.watchdog else []
                ctx    = self.ctx.build(include_errors=errors)
                reply  = await asyncio.to_thread(ask_claude, query, ctx)
                await message.reply(reply)

    async def _status(self) -> str:
        lines = ["**System Status**"]
        try:
            client = docker.from_env()
            for name in WATCH_CONTAINERS:
                try:
                    c = client.containers.get(name)
                    status = c.status
                    icon   = "🟢" if status == "running" else "🔴"
                    lines.append(f"{icon} `{name}` — {status}")
                except docker.errors.NotFound:
                    lines.append(f"🔴 `{name}` — not found")
        except Exception as e:
            lines.append(f"Docker unavailable: {e}")

        # IV collector data freshness
        last = _scalar(IV_DB,
            "SELECT MAX(timestamp) FROM iv_history WHERE data_type='intraday'")
        if last:
            age = (datetime.now() - datetime.fromisoformat(last)).seconds // 60
            icon = "🟢" if age < 10 else "🟡" if age < 30 else "🔴"
            lines.append(f"\n{icon} Last IV snapshot: {age}m ago")
        else:
            lines.append("\n🔴 No IV data today")

        today    = datetime.now().strftime("%Y-%m-%d")
        pt_count = _scalar(PT_DB,
            "SELECT COUNT(*) FROM paper_trades WHERE date=?", (today,), 0)
        lines.append(f"📋 Paper trades today: {pt_count}")

        return "\n".join(lines)

    def _trades_summary(self) -> str:
        today  = datetime.now().strftime("%Y-%m-%d")
        trades = _query(PT_DB, """
            SELECT symbol, side, strike, entry, last_price,
                   realized_pct, realized_rupees, status
            FROM   paper_trades WHERE date=?
            ORDER  BY opened_at
        """, (today,))

        if not trades:
            return "No paper trades today."

        lines = [f"**Paper Trades — {today}**"]
        total_pnl = 0
        for t in trades:
            icon = "🟩" if (t.get("realized_rupees") or 0) > 0 \
                   else "🟥" if t["status"] == "closed" else "🔲"
            pnl  = f"₹{t['realized_rupees']:+.0f} ({t['realized_pct']:+.1f}%)" \
                   if t.get("realized_rupees") is not None else "open"
            lines.append(
                f"{icon} **{t['symbol']}** {t['side']} {t['strike']} "
                f"@ ₹{t['entry']} → {pnl}"
            )
            total_pnl += t.get("realized_rupees") or 0

        lines.append(f"\nNet: ₹{total_pnl:+,.0f}")
        return "\n".join(lines)


# ── Health summary (08:50 daily) ──────────────────────────────────────────── #
def _post_morning_health(bot: TradingBot):
    async def _do():
        msg = await bot._status()
        await bot.send_alert(f"☀️ **Morning health check**\n{msg}")
    asyncio.run_coroutine_threadsafe(_do(), bot.loop)


def _run_scheduler(bot: TradingBot):
    schedule.every().day.at("08:50").do(_post_morning_health, bot)
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── Entry point ───────────────────────────────────────────────────────────── #
def main():
    if not DISCORD_BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN not set — exiting")
        return
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — Claude responses disabled")

    bot      = TradingBot()
    watchdog = LogWatchdog(bot)
    bot.watchdog = watchdog

    # Start watchdog threads
    watchdog.start()

    # Start scheduler thread
    sched_thread = threading.Thread(target=_run_scheduler, args=(bot,),
                                    daemon=True, name="scheduler")
    sched_thread.start()

    # Run Discord bot (blocking)
    logger.info("Starting Discord bot…")
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
