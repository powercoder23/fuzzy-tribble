from __future__ import annotations

import argparse
import csv
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import List

from trading_plan_backtest import (
    Trade,
    build_best_nifty_strategy_config,
    build_nifty_dhan_fetch_config,
    fetch_dhan_intraday_candles,
    load_candles,
    load_env_file,
    parse_timestamp,
    print_metrics,
    resolve_index_security_id,
    run_backtest,
    write_trades_csv,
)


def build_config(variant: str):
    base = replace(build_best_nifty_strategy_config(), bias_timeframes=("1w", "1d"), allowed_zone_kinds=("fvg",))
    strict = replace(base, confirmation_lookback_bars=2)
    variants = {
        "combo_base_sq_1520": replace(base, force_intraday_exit=True, square_off_time="15:20"),
        "combo_strict_sq_1520": replace(strict, force_intraday_exit=True, square_off_time="15:20"),
    }
    if variant not in variants:
        raise ValueError(f"Unknown variant: {variant}")
    return variants[variant]


def filter_trades_for_day(trades: List[Trade], trade_date: datetime.date) -> List[Trade]:
    return [trade for trade in trades if trade.entry_time.date() == trade_date]


def compute_day_metrics(trades: List[Trade]) -> dict[str, float]:
    closed = [trade for trade in trades if trade.exit_price is not None]
    wins = [trade for trade in closed if (trade.pnl_r or 0.0) > 0]
    total_r = sum(trade.pnl_r or 0.0 for trade in closed)
    return {
        "trades": float(len(closed)),
        "win_rate": (len(wins) / len(closed) * 100.0) if closed else 0.0,
        "avg_r": (total_r / len(closed)) if closed else 0.0,
        "total_r": total_r,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Test one intraday trading day using historical context from the best NIFTY intraday combo.")
    parser.add_argument("--trade-date", required=True, help="Trading day to report in YYYY-MM-DD format.")
    parser.add_argument("--variant", default="combo_base_sq_1520", choices=("combo_base_sq_1520", "combo_strict_sq_1520"))
    parser.add_argument("--data", type=Path, help="Optional local candle CSV instead of fetching from Dhan.")
    parser.add_argument("--from-date", required=True, help="Historical context start in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--to-date", required=True, help="Historical context end in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--symbol-name", default="NIFTY")
    parser.add_argument("--security-id")
    parser.add_argument("--exchange-segment", default="IDX_I")
    parser.add_argument("--instrument-type", default="INDEX")
    parser.add_argument("--output", type=Path, default=Path("nifty_single_day_intraday_trades.csv"))
    return parser


def write_summary_csv(path: Path, trades: List[Trade]) -> None:
    if not trades:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["message"])
            writer.writerow(["No trades for selected day"])
        return
    write_trades_csv(path, trades)


def main() -> None:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()

    trade_date = datetime.strptime(args.trade_date, "%Y-%m-%d").date()
    config = build_config(args.variant)

    if args.data:
        candles = load_candles(args.data)
    else:
        client_id = os.getenv("DHAN_CLIENT_ID")
        access_token = os.getenv("DHAN_ACCESS_TOKEN")
        if not client_id or not access_token:
            parser.error("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for Dhan fetches.")
        security_id = args.security_id or resolve_index_security_id(
            args.symbol_name,
            exchange_segment=args.exchange_segment,
            instrument_type=args.instrument_type,
        )
        if not security_id:
            parser.error(f"Could not resolve a Dhan security id for {args.symbol_name}. Pass --security-id explicitly.")
        fetch_config = build_nifty_dhan_fetch_config(
            client_id=client_id,
            access_token=access_token,
            interval=args.interval,
            security_id=security_id,
            exchange_segment=args.exchange_segment,
            instrument_type=args.instrument_type,
        )
        candles = fetch_dhan_intraday_candles(
            fetch_config,
            parse_timestamp(args.from_date),
            parse_timestamp(args.to_date),
        )

    result = run_backtest(candles, config)
    day_trades = filter_trades_for_day(result.trades, trade_date)
    metrics = compute_day_metrics(day_trades)
    write_summary_csv(args.output, day_trades)

    print(f"{args.symbol_name} single-day intraday test for {trade_date.isoformat()} using {args.variant}")
    print_metrics(metrics)
    print(f"Day trades written to {args.output}")


if __name__ == "__main__":
    main()
