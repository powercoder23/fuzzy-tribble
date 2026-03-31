from __future__ import annotations

import argparse
import os
from pathlib import Path

from trading_plan_backtest import (
    build_best_stock_strategy_config,
    build_stock_dhan_fetch_config,
    fetch_dhan_intraday_candles,
    load_candles,
    load_env_file,
    parse_timestamp,
    print_diagnostics,
    print_metrics,
    resolve_symbol_security_id,
    run_backtest,
    write_candles_csv,
    write_trades_csv,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the stock strategy as a Dhan-backed backtest.")
    parser.add_argument("--symbol-name", required=True, help="Underlying stock symbol, for example RELIANCE or HDFCBANK.")
    parser.add_argument("--data", type=Path, help="Optional local candle CSV instead of fetching from Dhan.")
    parser.add_argument("--from-date", required=True, help="Start date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--to-date", required=True, help="End date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--security-id", help="Optional Dhan security id for the stock.")
    parser.add_argument("--exchange-segment", default="NSE_EQ")
    parser.add_argument("--instrument-type", default="EQUITY")
    parser.add_argument("--save-fetched-data", type=Path)
    parser.add_argument("--output", type=Path, default=Path("stock_backtest_trades.csv"))
    parser.add_argument("--diagnostics", action="store_true")
    return parser


def main() -> None:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()

    if args.data:
        candles = load_candles(args.data)
    else:
        client_id = os.getenv("DHAN_CLIENT_ID")
        access_token = os.getenv("DHAN_ACCESS_TOKEN")
        if not client_id or not access_token:
            parser.error("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for Dhan fetches.")
        security_id = args.security_id or resolve_symbol_security_id(
            args.symbol_name,
            exchange_segment=args.exchange_segment,
            instrument_type=args.instrument_type,
        )
        if not security_id:
            parser.error(f"Could not resolve a Dhan security id for {args.symbol_name}. Pass --security-id explicitly.")
        fetch_config = build_stock_dhan_fetch_config(
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
        if args.save_fetched_data:
            write_candles_csv(args.save_fetched_data, candles)
            print(f"Fetched candles written to {args.save_fetched_data}")

    config = build_best_stock_strategy_config()
    result = run_backtest(candles, config)
    write_trades_csv(args.output, result.trades)

    print(f"{args.symbol_name} stock backtest complete")
    print_metrics(result.metrics)
    if args.diagnostics:
        print_diagnostics(result.diagnostics)
    print(f"Trades written to {args.output}")


if __name__ == "__main__":
    main()
