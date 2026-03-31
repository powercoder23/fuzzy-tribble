from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from trading_plan_backtest import (
    Candle,
    Trade,
    build_dhan_client,
    build_nifty_dhan_fetch_config,
    build_nifty_intraday_production_config,
    fetch_dhan_intraday_candles,
    find_latest_entry_signal,
    infer_candle_interval_minutes,
    load_candles,
    load_env_file,
    resolve_index_security_id,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch the validated NIFTY intraday production strategy and send Telegram alerts.")
    parser.add_argument("--data", type=Path, help="Optional local candle CSV for dry testing instead of Dhan fetch.")
    parser.add_argument("--lookback-days", type=int, default=400, help="How much history to fetch for signal context.")
    parser.add_argument("--interval", type=int, default=15, help="Dhan candle interval in minutes.")
    parser.add_argument("--symbol-name", default="NIFTY", help="Display name used in alert messages.")
    parser.add_argument("--security-id", help="Optional Dhan security id. If omitted, the script will try to resolve it from symbol name.")
    parser.add_argument("--exchange-segment", default="IDX_I")
    parser.add_argument("--instrument-type", default="INDEX")
    parser.add_argument("--poll-seconds", type=int, default=300, help="Polling cadence in seconds when not using --once. Default 300 means every 5 minutes.")
    parser.add_argument("--poll-delay-seconds", type=int, default=30, help="Extra delay after each wall-clock polling boundary. Default 30 means runs at times like 09:35:30.")
    parser.add_argument("--state-file", type=Path, default=Path("nifty_intraday_production_alert_state.json"))
    parser.add_argument("--telegram-bot-token", help="Optional override for TELEGRAM_BOT_TOKEN.")
    parser.add_argument("--telegram-chat-id", help="Optional override for TELEGRAM_CHAT_ID.")
    parser.add_argument("--expiry", help="Optional option expiry override in YYYY-MM-DD.")
    parser.add_argument("--strike-step", type=int, default=50, help="Strike rounding step for ATM selection.")
    parser.add_argument("--option-premium-override", type=float, help="Optional option premium override for dry tests.")
    parser.add_argument("--once", action="store_true", help="Run one evaluation and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending them.")
    return parser


def completed_candles(candles: list[Candle], interval_minutes: int, now: Optional[datetime] = None) -> list[Candle]:
    if not candles:
        return []
    now = now or datetime.now()
    cutoff = now - timedelta(minutes=interval_minutes)
    return [candle for candle in candles if candle.timestamp <= cutoff]


def next_poll_time(now: Optional[datetime], cadence_seconds: int, delay_seconds: int) -> datetime:
    now = now or datetime.now()
    if cadence_seconds <= 0:
        raise ValueError("poll cadence must be greater than zero seconds")
    if delay_seconds < 0:
        raise ValueError("poll delay cannot be negative")

    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_seconds = int((now - start_of_day).total_seconds())
    next_boundary_seconds = ((elapsed_seconds // cadence_seconds) + 1) * cadence_seconds
    return start_of_day + timedelta(seconds=next_boundary_seconds + delay_seconds)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def round_to_step(value: float, step: int) -> int:
    return int(round(value / step) * step)


def normalize_expiry_value(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    candidates = (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
        "%d %b %Y",
        "%d %B %Y",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    )
    for pattern in candidates:
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    if "T" in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return None
    return None


def extract_first_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "expiryList", "expiries", "results", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for value in payload.values():
            if isinstance(value, list):
                return value
    return []


def choose_nearest_expiry(payload: Any, fallback_date: datetime) -> str:
    expiries: list[str] = []
    for item in extract_first_list(payload):
        normalized = normalize_expiry_value(item)
        if normalized:
            expiries.append(normalized)
            continue
        if isinstance(item, dict):
            for value in item.values():
                normalized = normalize_expiry_value(value)
                if normalized:
                    expiries.append(normalized)
                    break
    if not expiries:
        raise RuntimeError("No option expiry dates were returned by Dhan.")
    expiries = sorted(set(expiries))
    fallback_iso = fallback_date.date().isoformat()
    for expiry in expiries:
        if expiry >= fallback_iso:
            return expiry
    return expiries[-1]


def collect_option_contracts(payload: Any) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        candidate = payload.get("data")
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, dict):
                    contracts.append(item)
        for key in ("oc", "optionChain", "chain", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, dict):
                for strike_value, strike_payload in candidate.items():
                    if not isinstance(strike_payload, dict):
                        continue
                    for side_key, side_payload in strike_payload.items():
                        if not isinstance(side_payload, dict):
                            continue
                        merged = dict(side_payload)
                        merged.setdefault("strikePrice", strike_payload.get("strikePrice", strike_value))
                        merged.setdefault("optionType", side_key)
                        contracts.append(merged)
        for value in payload.values():
            if isinstance(value, dict):
                contracts.extend(collect_option_contracts(value))
    elif isinstance(payload, list):
        for item in payload:
            contracts.extend(collect_option_contracts(item))
    return contracts


def parse_option_side(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    if text in {"CALL", "CE", "C"}:
        return "CE"
    if text in {"PUT", "PE", "P"}:
        return "PE"
    return None


def parse_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_price(value: float) -> str:
    text = f"{value:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def parse_contract_snapshot(contract: dict[str, Any]) -> Optional[dict[str, Any]]:
    option_type = None
    for key in ("optionType", "option_type", "type", "instrumentType", "right"):
        option_type = parse_option_side(contract.get(key))
        if option_type:
            break
    strike = None
    for key in ("strikePrice", "strike", "strike_price", "StrikePrice"):
        strike = parse_float(contract.get(key))
        if strike is not None:
            break
    security_id = None
    for key in ("securityId", "security_id", "drvSecurityId", "SecurityId"):
        value = contract.get(key)
        if value not in (None, ""):
            security_id = str(value)
            break
    premium = None
    for key in ("lastPrice", "ltp", "LTP", "lastTradedPrice", "premium"):
        premium = parse_float(contract.get(key))
        if premium is not None:
            break
    if option_type and strike is not None:
        return {
            "option_type": option_type,
            "strike": strike,
            "security_id": security_id,
            "premium": premium,
        }
    return None


def extract_ltp(payload: Any, fallback_security_id: str | None = None) -> Optional[float]:
    if isinstance(payload, dict):
        for key in ("LTP", "ltp", "last_price", "lastPrice", "lastTradedPrice"):
            value = parse_float(payload.get(key))
            if value is not None:
                return value
        if fallback_security_id:
            value = payload.get(fallback_security_id)
            if value is not None:
                nested = extract_ltp(value, fallback_security_id=None)
                if nested is not None:
                    return nested
        for value in payload.values():
            nested = extract_ltp(value, fallback_security_id=fallback_security_id)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = extract_ltp(item, fallback_security_id=fallback_security_id)
            if nested is not None:
                return nested
    return None


def resolve_option_contract(
    args: argparse.Namespace,
    spot_price: float,
    signal_time: datetime,
    direction: str,
) -> dict[str, Any]:
    client_id = os.getenv("DHAN_CLIENT_ID")
    access_token = os.getenv("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        raise RuntimeError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for live option contract lookup.")

    security_id = args.security_id or resolve_index_security_id(
        args.symbol_name,
        exchange_segment=args.exchange_segment,
        instrument_type=args.instrument_type,
    )
    if not security_id:
        raise RuntimeError(f"Could not resolve a Dhan security id for {args.symbol_name}. Pass --security-id explicitly.")

    fetch_config = build_nifty_dhan_fetch_config(
        client_id=client_id,
        access_token=access_token,
        interval=args.interval,
        security_id=security_id,
        exchange_segment=args.exchange_segment,
        instrument_type=args.instrument_type,
    )
    client = build_dhan_client(fetch_config)
    expiry = args.expiry or choose_nearest_expiry(
        client.expiry_list(security_id, args.exchange_segment),
        signal_time,
    )
    option_type = "CE" if direction == "bullish" else "PE"
    atm_strike = round_to_step(spot_price, args.strike_step)

    chain = client.option_chain(security_id, args.exchange_segment, expiry)
    snapshots = [item for item in (parse_contract_snapshot(contract) for contract in collect_option_contracts(chain)) if item]
    candidates = [
        snapshot
        for snapshot in snapshots
        if snapshot["option_type"] == option_type and int(round(snapshot["strike"])) == atm_strike
    ]
    if not candidates:
        raise RuntimeError(f"Could not find ATM {option_type} contract for strike {atm_strike} in Dhan option chain.")

    contract = candidates[0]
    premium = contract["premium"]
    if premium is None and contract["security_id"]:
        premium = extract_ltp(client.ticker_data({"NSE_FNO": [int(contract["security_id"])]}), contract["security_id"])
    if premium is None and args.option_premium_override is not None:
        premium = args.option_premium_override
    if premium is None:
        raise RuntimeError("Could not resolve the ATM option premium from Dhan. Use --option-premium-override for dry tests.")

    return {
        "expiry": expiry,
        "strike": atm_strike,
        "option_type": option_type,
        "security_id": contract["security_id"],
        "premium": premium,
    }


def build_option_trade_plan(args: argparse.Namespace, trade: Trade, candles: list[Candle]) -> dict[str, Any]:
    signal_candle = next((candle for candle in candles if candle.timestamp == trade.entry_time), None)
    spot_price = signal_candle.close if signal_candle else trade.entry_price
    if args.option_premium_override is not None and args.dry_run:
        return {
            "expiry": args.expiry,
            "strike": round_to_step(spot_price, args.strike_step),
            "option_type": "CE" if trade.direction == "bullish" else "PE",
            "security_id": None,
            "premium": args.option_premium_override,
        }
    return resolve_option_contract(args, spot_price, trade.entry_time, trade.direction)


def format_signal_message(trade: Trade, symbol_name: str, option_plan: dict[str, Any]) -> str:
    headline = f"BUY {symbol_name} {int(option_plan['strike'])} {option_plan['option_type']} @ {format_price(float(option_plan['premium']))}"
    zone_text = "unknown zone"
    if trade.setup_zone:
        zone_text = f"{trade.setup_zone.kind} on {trade.setup_zone.timeframe}"
    return (
        f"{headline}\n"
        f"{symbol_name} Intraday Alert\n"
        f"Signal Time: {trade.entry_time:%Y-%m-%d %H:%M}\n"
        f"Spot Entry: {format_price(trade.entry_price)}\n"
        f"Option Premium: {format_price(float(option_plan['premium']))}\n"
        f"Expiry: {option_plan['expiry'] or 'nearest'}\n"
        f"Structure: {'BULLISH' if trade.direction == 'bullish' else 'BEARISH'}\n"
        f"Stop: {format_price(trade.stop_price)}\n"
        f"Target: {format_price(trade.target_price)}\n"
        f"Square Off: 15:20\n"
        f"Setup: {zone_text}\n"
        f"Config: 2-layer bias | FVG only | 1h POI | BOS/CHOCH | 09:15-12:00"
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
    candles = fetch_or_load_candles(args)
    effective_interval = args.interval or infer_candle_interval_minutes(candles) or 15
    candles = completed_candles(candles, effective_interval)
    if not candles:
        print("No completed candles available.")
        return

    config = build_nifty_intraday_production_config()
    print(f"Evaluating strategy on latest completed candle at {candles[-1].timestamp:%Y-%m-%d %H:%M} with {len(candles)} total candles...") 
    signal = find_latest_entry_signal(candles, config)
    if not signal:
        print(f"No new signal on latest completed candle {candles[-1].timestamp:%Y-%m-%d %H:%M}.")
        return

    state = load_state(args.state_file)
    signal_key = f"{signal.entry_time.isoformat()}|{signal.direction}|{signal.entry_price:.2f}"
    if state.get("last_alert_key") == signal_key:
        print("Latest signal already alerted.")
        return

    option_plan = build_option_trade_plan(args, signal, candles)
    message = format_signal_message(signal, args.symbol_name, option_plan)
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
    state["last_option_plan"] = {
        "strike": int(option_plan["strike"]),
        "option_type": option_plan["option_type"],
        "premium": round(float(option_plan["premium"]), 2),
        "expiry": option_plan["expiry"],
    }
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
        scheduled_at = next_poll_time(datetime.now(), args.poll_seconds, args.poll_delay_seconds)
        sleep_seconds = max(0.0, (scheduled_at - datetime.now()).total_seconds())
        print(f"Next evaluation scheduled at {scheduled_at:%Y-%m-%d %H:%M:%S}.")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
