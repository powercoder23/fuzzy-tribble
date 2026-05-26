#!/usr/bin/env python3
"""
IV Collector Service — Service 1.

Sole responsibility: fetch option chain data for every F&O stock and persist
IV snapshots to iv_history.db via iv_store.

Runs continuously on weekdays:
  08:45       build EOD watchlist (scored + filtered stock list for next session)
  09:15–09:50 warmup pass: rapid sweep of ALL stocks, 1.5s between each
  09:50–15:30 intraday pass: full sweep every ~15 min, 2s between each stock

Neither strategy service (discount, momentum) should write IV data.
They only read from iv_store.
"""

import logging
import os
import time
from collections import Counter, deque
from datetime import datetime, time as dt_time
from pathlib import Path

import requests

import pytz

from config import Config
from discount import (
    DiscountedPremiumScanner,
    unwrap_dhan_payload,
    get_trading_days_to_expiry,
)
import iv_store

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
        logging.FileHandler(Config.LOGS_DIR / "iv_collector.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ── timing ────────────────────────────────────────────────────────────────────
WARMUP_START   = dt_time(9, 15)
WARMUP_END     = dt_time(9, 50)
INTRADAY_END   = dt_time(15, 30)
EOD_TIME       = dt_time(8, 45)

WARMUP_SLEEP   = 1.5   # seconds between stocks during 9:15-9:50
INTRADAY_SLEEP = 2.0   # seconds between stocks during intraday pass
EOD_SLEEP      = 0.5   # seconds between stocks during EOD watchlist build


class IVCollector:

    def __init__(self):
        self._scanner: DiscountedPremiumScanner = None
        self._expiry_cache: dict = {}
        # EOD tracking — reset each calendar day
        self._pass_log: list[dict]  = []   # [{kind, time, saved, total}]
        self._fail_counts: Counter  = Counter()  # symbol → total fail count today
        self._eod_sent: bool        = False

    # ── scanner lifecycle ─────────────────────────────────────────────────────

    def _ensure_scanner(self) -> DiscountedPremiumScanner:
        if self._scanner is None:
            self._scanner = self._create_scanner()
            logger.info("IVCollector: scanner initialised with %d FNO symbols",
                        len(self._scanner.fno_stocks))
        return self._scanner

    def _refresh_scanner(self) -> DiscountedPremiumScanner:
        """Rebuild scanner after token refresh — call once per pass."""
        self._scanner = self._create_scanner()
        return self._scanner

    def _create_scanner(self) -> DiscountedPremiumScanner:
        return DiscountedPremiumScanner(store_intraday=True)

    # ── expiry resolution ─────────────────────────────────────────────────────

    def _resolve_expiry(self, scanner: DiscountedPremiumScanner,
                        security_id, symbol: str) -> str | None:
        key = str(security_id)
        if key in self._expiry_cache:
            return self._expiry_cache[key]

        segment = "IDX_I" if symbol in {"NIFTY", "BANKNIFTY"} else "NSE_FNO"
        expiries = scanner.get_expiry_list(security_id, segment)
        if not expiries:
            return None

        chosen = None
        for exp in expiries:
            if get_trading_days_to_expiry(exp) >= 7:
                chosen = exp
                break
        if not chosen:
            chosen = expiries[min(1, len(expiries) - 1)]

        self._expiry_cache[key] = chosen
        # Share same expiry across all NSE_FNO stocks (same calendar)
        if segment == "NSE_FNO":
            self._expiry_cache["_nse_fno"] = chosen
        return chosen

    # ── single stock IV fetch + save ──────────────────────────────────────────

    def _collect_one(self, scanner: DiscountedPremiumScanner,
                     security_id, symbol: str,
                     data_type: str = "intraday",
                     snapshot_dt: datetime = None,
                     max_retries: int = 2) -> bool:
        """
        Fetch option chain for one stock, extract ATM IV, save via iv_store.
        Returns True on success, False on failure (caller decides whether to retry).
        """
        if snapshot_dt is None:
            snapshot_dt = datetime.now()

        # Reuse cached NSE_FNO expiry to avoid one extra API call per stock
        segment = "IDX_I" if symbol in {"NIFTY", "BANKNIFTY"} else "NSE_FNO"
        if segment == "NSE_FNO" and "_nse_fno" in self._expiry_cache:
            expiry = self._expiry_cache["_nse_fno"]
        else:
            expiry = self._resolve_expiry(scanner, security_id, symbol)

        if not expiry:
            logger.debug("No expiry for %s — skipping", symbol)
            return False

        last_err = None
        for attempt in range(max_retries):
            try:
                chain_resp = scanner.get_option_chain(security_id, segment, expiry)
                if not isinstance(chain_resp, dict) or chain_resp.get("status") != "success":
                    raise ValueError("bad chain response")

                chain_data   = unwrap_dhan_payload(chain_resp.get("data") or {})
                spot_price   = chain_data.get("last_price")
                option_chain = chain_data.get("oc")

                if spot_price is None or not isinstance(option_chain, dict) or not option_chain:
                    raise ValueError("empty chain payload")

                atm_ctx     = scanner.extract_atm_reference_ivs(option_chain, spot_price)
                chain_metrics = scanner.extract_chain_metrics(option_chain)

                if atm_ctx.get("atm_iv") is None:
                    raise ValueError("missing ATM IV")

                saved = iv_store.save_snapshot(
                    security_id         = str(security_id),
                    symbol              = symbol,
                    timestamp           = snapshot_dt,
                    spot_price          = spot_price,
                    atm_strike          = atm_ctx.get("atm_strike"),
                    atm_iv              = atm_ctx.get("atm_iv"),
                    atm_call_iv         = atm_ctx.get("atm_call_iv"),
                    atm_put_iv          = atm_ctx.get("atm_put_iv"),
                    atm_call_oi         = atm_ctx.get("atm_call_oi"),
                    atm_put_oi          = atm_ctx.get("atm_put_oi"),
                    total_call_oi       = chain_metrics.get("total_call_oi"),
                    total_put_oi        = chain_metrics.get("total_put_oi"),
                    total_call_volume   = chain_metrics.get("total_call_volume"),
                    total_put_volume    = chain_metrics.get("total_put_volume"),
                    max_oi_strike_call  = chain_metrics.get("max_oi_strike_call"),
                    max_oi_strike_put   = chain_metrics.get("max_oi_strike_put"),
                    data_type           = data_type,
                )

                # Save daily record once per day (first successful fetch)
                if data_type == "intraday" and not iv_store.daily_snapshot_exists_today(str(security_id)):
                    iv_store.save_snapshot(
                        security_id         = str(security_id),
                        symbol              = symbol,
                        timestamp           = snapshot_dt,
                        spot_price          = spot_price,
                        atm_strike          = atm_ctx.get("atm_strike"),
                        atm_iv              = atm_ctx.get("atm_iv"),
                        atm_call_iv         = atm_ctx.get("atm_call_iv"),
                        atm_put_iv          = atm_ctx.get("atm_put_iv"),
                        atm_call_oi         = atm_ctx.get("atm_call_oi"),
                        atm_put_oi          = atm_ctx.get("atm_put_oi"),
                        total_call_oi       = chain_metrics.get("total_call_oi"),
                        total_put_oi        = chain_metrics.get("total_put_oi"),
                        total_call_volume   = chain_metrics.get("total_call_volume"),
                        total_put_volume    = chain_metrics.get("total_put_volume"),
                        max_oi_strike_call  = chain_metrics.get("max_oi_strike_call"),
                        max_oi_strike_put   = chain_metrics.get("max_oi_strike_put"),
                        data_type           = "daily",
                    )

                logger.debug("IV saved | %s | iv=%.1f | saved=%s", symbol, atm_ctx["atm_iv"], saved)
                return True

            except Exception as exc:
                last_err = exc
                if attempt < max_retries - 1:
                    backoff = 5.0 if "too many requests" in str(exc).lower() else 1.5
                    time.sleep(backoff)

        logger.warning("_collect_one gave up | %s | %s", symbol, last_err)
        return False

    # ── full sweep passes ─────────────────────────────────────────────────────

    def run_warmup_pass(self, ignore_time_window: bool = False) -> int:
        """
        9:15–9:50: rapid sweep of ALL FNO stocks.
        Returns number of successful IV saves.
        """
        scanner = self._ensure_scanner()
        all_symbols = list(scanner.fno_stocks.items())
        if not all_symbols:
            logger.warning("Warmup pass skipped — no FNO symbols")
            return 0

        queue   = deque(all_symbols)
        saved   = 0
        skipped = 0

        logger.info("Warmup pass starting | symbols=%d", len(all_symbols))
        while queue:
            now = datetime.now().time()
            if not ignore_time_window:
                if now < WARMUP_START:
                    time.sleep(1)
                    continue
                if now >= WARMUP_END:
                    break

            security_id, symbol = queue.popleft()
            snapshot_dt = _floor_to_five_minutes()
            ok = self._collect_one(scanner, security_id, symbol,
                                   data_type="intraday", snapshot_dt=snapshot_dt)
            if ok:
                saved += 1
            else:
                skipped += 1
                self._fail_counts[symbol] += 1

            if queue:
                time.sleep(WARMUP_SLEEP)

        self._pass_log.append({
            "kind":  "Warmup",
            "time":  datetime.now().strftime("%H:%M"),
            "saved": saved,
            "total": len(all_symbols),
        })
        logger.info("Warmup pass done | saved=%d skipped=%d", saved, skipped)
        return saved

    def run_intraday_pass(self) -> int:
        """
        One full sweep of all FNO stocks during market hours.
        Called repeatedly from the main loop every ~15 minutes.
        Returns number of successful IV saves.
        """
        scanner  = self._refresh_scanner()
        all_syms = list(scanner.fno_stocks.items())
        if not all_syms:
            return 0

        snapshot_dt = _floor_to_five_minutes()
        saved = 0
        logger.info("Intraday IV pass starting | symbols=%d | ts=%s",
                    len(all_syms), snapshot_dt.strftime("%H:%M"))

        for security_id, symbol in all_syms:
            now = datetime.now().time()
            if now >= INTRADAY_END:
                logger.info("Intraday pass stopping — market close")
                break
            ok = self._collect_one(scanner, security_id, symbol,
                                   data_type="intraday", snapshot_dt=snapshot_dt)
            if ok:
                saved += 1
            else:
                self._fail_counts[symbol] += 1
            time.sleep(INTRADAY_SLEEP)

        self._pass_log.append({
            "kind":  "Intraday",
            "time":  snapshot_dt.strftime("%H:%M"),
            "saved": saved,
            "total": len(all_syms),
        })
        logger.info("Intraday IV pass done | saved=%d / %d", saved, len(all_syms))
        return saved

    # ── telegram ──────────────────────────────────────────────────────────────

    def _send_telegram(self, text: str) -> bool:
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            logger.info("Telegram not configured — EOD summary not sent")
            return False
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if not resp.ok:
                logger.warning("Telegram EOD send failed: %s", resp.text[:200])
            return resp.ok
        except Exception:
            logger.exception("Telegram EOD send exception")
            return False

    def _build_eod_message(self, universe_size: int) -> str:
        stats    = iv_store.get_eod_stats()
        now_str  = datetime.now().strftime("%a, %d %b %Y  |  %H:%M IST")

        total_snaps = stats.get("intraday_snapshots_today", 0)
        total_fails = sum(self._fail_counts.values())
        daily_saves = stats.get("daily_symbols_today", 0)
        passes_run  = len(self._pass_log)
        pass_rate   = (
            f"{100 * total_snaps / (total_snaps + total_fails):.1f}%"
            if (total_snaps + total_fails) else "—"
        )

        lines = [
            "📊 <b>IV Collector — EOD Report</b>",
            f"📅 {now_str}",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "📡 <b>Today's Collection</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"Universe        : {universe_size} FNO stocks",
            f"Passes run      : {passes_run}",
            "",
            f"✅ Snapshots OK : {total_snaps:,}",
            f"❌ Failed       : {total_fails:,}",
            f"📋 Daily saves  : {daily_saves} / {universe_size} stocks",
            "",
            f"Success rate    : {pass_rate}",
        ]

        # Pass timeline — show first, last, and any with failures
        if self._pass_log:
            lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━━",
                      "🕐 <b>Pass Timeline</b>",
                      "━━━━━━━━━━━━━━━━━━━━━━━━━━"]
            shown_indices = {0, len(self._pass_log) - 1}
            for i, p in enumerate(self._pass_log):
                if p["saved"] < p["total"]:
                    shown_indices.add(i)

            prev_shown = True
            for i, p in enumerate(self._pass_log):
                if i in shown_indices:
                    icon   = "✅" if p["saved"] == p["total"] else "⚠️"
                    label  = f"{p['time']} {p['kind']:<8}"
                    result = f"{icon} {p['saved']:>3} / {p['total']}"
                    suffix = "  ← last pass" if i == len(self._pass_log) - 1 else ""
                    lines.append(f"{label}  {result}{suffix}")
                    prev_shown = True
                elif prev_shown:
                    lines.append("  ...")
                    prev_shown = False

        # History DB stats
        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "🗄️ <b>History DB</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"Symbols tracked      : {stats.get('symbols_with_history', 0)}",
            f"Avg history depth    : {stats.get('avg_history_days', 0)} days",
            f"Min history (symbol) : {stats.get('min_history_days', 0)} days"
            f"  ({stats.get('min_history_symbol', '—')})",
            f"Total intraday rows  : {stats.get('total_intraday_rows', 0):,}",
            f"Total daily rows     : {stats.get('total_daily_rows', 0):,}",
        ]

        # Worst offenders (3+ failures)
        offenders = [(sym, cnt) for sym, cnt in self._fail_counts.most_common()
                     if cnt >= 3]
        if offenders:
            lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━━",
                      "⚠️ <b>Worst Offenders Today</b>",
                      "━━━━━━━━━━━━━━━━━━━━━━━━━━"]
            for sym, cnt in offenders[:8]:
                lines.append(f"{sym:<12} — {cnt} fails")

        return "\n".join(lines)

    def _send_eod_summary(self, universe_size: int) -> None:
        try:
            msg = self._build_eod_message(universe_size)
            ok  = self._send_telegram(msg)
            logger.info("EOD summary %s", "sent" if ok else "logged (Telegram not configured)")
            logger.info("\n%s", msg)
        except Exception:
            logger.exception("EOD summary failed")

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Main event loop. Runs forever on weekdays.

        Timeline:
          before 09:15  → idle (60s sleep)
          09:15–09:50   → continuous warmup sweep (WARMUP_SLEEP between stocks)
          09:50–15:30   → intraday passes every 1 hour
          after 15:30   → idle (60s sleep)
        """
        iv_store.init_db()
        scanner = self._ensure_scanner()
        logger.info("IVCollector started | fno_symbols=%d", len(scanner.fno_stocks))

        _intraday_last_pass: datetime = None
        INTRADAY_INTERVAL = 60 * 60  # 1 hour between full sweeps
        EOD_REPORT_TIME   = dt_time(15, 35)
        _last_reset_date  = datetime.now().date()

        while True:
            now = datetime.now()
            t   = now.time()

            # Reset daily tracking at midnight
            if now.date() != _last_reset_date:
                self._pass_log    = []
                self._fail_counts = Counter()
                self._eod_sent    = False
                _last_reset_date  = now.date()

            if now.weekday() >= 5:
                logger.info("Weekend — sleeping 10 min")
                time.sleep(600)
                continue

            if t < WARMUP_START:
                logger.debug("Pre-market idle | time=%s", t.strftime("%H:%M"))
                time.sleep(60)

            elif WARMUP_START <= t < WARMUP_END:
                self.run_warmup_pass()

            elif WARMUP_END <= t < INTRADAY_END:
                if (_intraday_last_pass is None or
                        (now - _intraday_last_pass).total_seconds() >= INTRADAY_INTERVAL):
                    self.run_intraday_pass()
                    _intraday_last_pass = now
                else:
                    time.sleep(30)

            else:
                if not self._eod_sent and t >= EOD_REPORT_TIME:
                    universe_size = len(scanner.fno_stocks) if scanner else 0
                    self._send_eod_summary(universe_size)
                    self._eod_sent = True

                logger.debug("Post-market idle | time=%s", t.strftime("%H:%M"))
                _intraday_last_pass = None
                time.sleep(60)


# ── helpers ───────────────────────────────────────────────────────────────────

def _floor_to_five_minutes() -> datetime:
    v = datetime.now()
    return v.replace(minute=(v.minute // 5) * 5, second=0, microsecond=0)


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="IV Collector Service")
    parser.add_argument("--warmup-once", action="store_true",
                        help="Run one warmup pass ignoring time window and exit")
    parser.add_argument("--intraday-once", action="store_true",
                        help="Run one intraday pass and exit")
    args = parser.parse_args()

    iv_store.init_db()
    collector = IVCollector()

    if args.warmup_once:
        collector.run_warmup_pass(ignore_time_window=True)
        return
    if args.intraday_once:
        collector.run_intraday_pass()
        return

    collector.run()


if __name__ == "__main__":
    main()
