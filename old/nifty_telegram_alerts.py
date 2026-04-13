from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from trading_plan_backtest import (
    Candle,
    Trade,
    build_best_nifty_strategy_config,
    build_nifty_dhan_fetch_config,
    fetch_dhan_intraday_candles,
    find_latest_entry_signal,
    load_candles,
    load_env_file,
    resolve_index_security_id,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch index strategy signals and send Telegram alerts.")
    parser.add_argument("--data", type=Path, help="Optional local candle CSV for dry testing instead of Dhan fetch.")
    parser.add_argument("--lookback-days", type=int, default=365, help="How much history to fetch for signal context.")
    parser.add_argument("--interval", type=int, default=15, help="Dhan candle interval in minutes.")
    parser.add_argument("--symbol-name", default="NIFTY", help="Display name used in alert messages.")
    parser.add_argument("--security-id", help="Optional Dhan security id. If omitted, the script will try to resolve it from symbol name.")
    parser.add_argument("--exchange-segment", default="IDX_I")
    parser.add_argument("--instrument-type", default="INDEX")
    parser.add_argument("--poll-seconds", type=int, default=300, help="Polling interval when not using --once.")
    parser.add_argument("--state-file", type=Path, default=Path("nifty_alert_state.json"))
    parser.add_argument("--telegram-bot-token", help="Optional override for TELEGRAM_BOT_TOKEN.")
    parser.add_argument("--telegram-chat-id", help="Optional override for TELEGRAM_CHAT_ID.")
    parser.add_argument("--once", action="store_true", help="Run one evaluation and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending them.")
    return parser


def completed_candles(candles: list[Candle], interval_minutes: int, now: Optional[datetime] = None) -> list[Candle]:
    if not candles:
        return []
    now = now or datetime.now()
    cutoff = now - timedelta(minutes=interval_minutes)
    return [candle for candle in candles if candle.timestamp <= cutoff]


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def format_signal_message(trade: Trade, symbol_name: str) -> str:
    direction = "LONG" if trade.direction == "bullish" else "SHORT"
    zone_text = "unknown zone"
    if trade.setup_zone:
        zone_text = f"{trade.setup_zone.kind} on {trade.setup_zone.timeframe}"
    return (
        f"{symbol_name} Strategy Alert\n"
        f"Signal: {direction}\n"
        f"Time: {trade.entry_time:%Y-%m-%d %H:%M}\n"
        f"Entry: {trade.entry_price:.2f}\n"
        f"Stop: {trade.stop_price:.2f}\n"
        f"Target: {trade.target_price:.2f}\n"
        f"Setup: {zone_text}\n"
        f"Config: 1h POI | RR 2.0 | 09:15-10:45 | BOS/CHOCH"
    )


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        url=f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Telegram send failed with status {response.status}")


def fetch_or_load_candles(args: argparse.Namespace) -> list[Candle]:
    if args.data:
        return load_candles(args.data)
    client_id = os.getenv("DHAN_CLIENT_ID")
    access_token = os.getenv("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        raise RuntimeError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for Dhan fetches.")
    security_id = args.security_id or resolve_index_security_id(
        args.symbol_name,
        exchange_segment=args.exchange_segment,
        instrument_type=args.instrument_type,
    )
    if not security_id:
        raise RuntimeError(f"Could not resolve a Dhan security id for {args.symbol_name}. Pass --security-id explicitly.")
    end = datetime.now()
    start = end - timedelta(days=args.lookback_days)
    fetch_config = build_nifty_dhan_fetch_config(
        client_id=client_id,
        access_token=access_token,
        interval=args.interval,
        security_id=security_id,
        exchange_segment=args.exchange_segment,
        instrument_type=args.instrument_type,
    )
    fetch_config.batch_pause_seconds = 1.0
    fetch_config.retry_pause_seconds = 3.0
    fetch_config.max_retries = 4
    return fetch_dhan_intraday_candles(fetch_config, start, end)


def evaluate_and_alert(args: argparse.Namespace) -> None:
    candles = completed_candles(fetch_or_load_candles(args), args.interval)
    if not candles:
        print("No completed candles available.")
        return

    config = build_best_nifty_strategy_config()
    signal = find_latest_entry_signal(candles, config)
    if not signal:
        print(f"No new signal on latest completed candle {candles[-1].timestamp:%Y-%m-%d %H:%M}.")
        return

    state = load_state(args.state_file)
    signal_key = f"{signal.entry_time.isoformat()}|{signal.direction}|{signal.entry_price:.2f}"
    if state.get("last_alert_key") == signal_key:
        print("Latest signal already alerted.")
        return

    message = format_signal_message(signal, args.symbol_name)
    if args.dry_run:
        print(message)
    else:
        bot_token = args.telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = args.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required unless --dry-run is used.")
        send_telegram_message(bot_token, chat_id, message)
        print("Telegram alert sent.")

    state["last_alert_key"] = signal_key
    state["last_alert_time"] = datetime.now().isoformat()
    save_state(args.state_file, state)


def main() -> None:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()

    if args.once:
        evaluate_and_alert(args)
        return

    while True:
        try:
            evaluate_and_alert(args)
        except Exception as exc:
            print(f"Alert loop error: {exc}")
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
