from __future__ import annotations

import argparse
import os
from pathlib import Path

from trading_plan_backtest import (
    BacktestConfig,
    build_fast_nifty_1m_config,
    build_fast_nifty_5m_config,
    build_nifty_dhan_fetch_config,
    build_nifty_intraday_production_config,
    build_stock_dhan_fetch_config,
    fetch_dhan_intraday_candles,
    load_candles,
    load_env_file,
    parse_timestamp,
    print_diagnostics,
    print_metrics,
    resolve_index_security_id,
    resolve_symbol_security_id,
    run_backtest,
    write_candles_csv,
    write_trades_csv,
)


def build_preset_config(name: str) -> BacktestConfig:
    if name == "15m":
        return build_nifty_intraday_production_config()
    if name == "5m":
        return build_fast_nifty_5m_config()
    if name == "1m":
        return build_fast_nifty_1m_config()
    raise ValueError(f"Unsupported preset: {name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an intraday strategy backtest on local CSV data or Dhan candles.")
    parser.add_argument("--preset", choices=("15m", "5m", "1m"), default="15m", help="Intraday setup preset family.")
    parser.add_argument("--symbol-type", choices=("index", "stock"), default="index", help="How symbol resolution should behave for Dhan fetches.")
    parser.add_argument("--symbol-name", default="NIFTY", help="Display name for logs and Dhan symbol resolution.")
    parser.add_argument("--data", type=Path, help="Optional local candle CSV instead of fetching from Dhan.")
    parser.add_argument("--from-date", required=True, help="Start date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--to-date", required=True, help="End date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--interval", type=int, help="Dhan candle interval in minutes. Defaults to the selected preset base timeframe.")
    parser.add_argument("--security-id", help="Optional Dhan security id. If omitted, the script will try to resolve it from symbol name.")
    parser.add_argument("--exchange-segment", help="Optional Dhan exchange segment override.")
    parser.add_argument("--instrument-type", help="Optional Dhan instrument type override.")
    parser.add_argument("--save-fetched-data", type=Path, help="Optional CSV path to save fetched candles.")
    parser.add_argument("--output", type=Path, default=Path("intraday_backtest_trades.csv"))
    parser.add_argument("--diagnostics", action="store_true")
    return parser


def default_market_args(symbol_type: str) -> tuple[str, str]:
    if symbol_type == "stock":
        return "NSE_EQ", "EQUITY"
    return "IDX_I", "INDEX"


def resolve_security_id(
    symbol_type: str,
    symbol_name: str,
    security_id: str | None,
    exchange_segment: str,
    instrument_type: str,
) -> str | None:
    if security_id:
        return security_id
    if symbol_type == "stock":
        return resolve_symbol_security_id(
            symbol_name,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
        )
    return resolve_index_security_id(
        symbol_name,
        exchange_segment=exchange_segment,
        instrument_type=instrument_type,
    )


def fetch_candles(args: argparse.Namespace, config: BacktestConfig):
    client_id = os.getenv("DHAN_CLIENT_ID")
    access_token = os.getenv("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        raise ValueError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for Dhan fetches.")

    default_exchange_segment, default_instrument_type = default_market_args(args.symbol_type)
    exchange_segment = args.exchange_segment or default_exchange_segment
    instrument_type = args.instrument_type or default_instrument_type
    security_id = resolve_security_id(
        args.symbol_type,
        args.symbol_name,
        args.security_id,
        exchange_segment,
        instrument_type,
    )
    if not security_id:
        raise ValueError(f"Could not resolve a Dhan security id for {args.symbol_name}. Pass --security-id explicitly.")

    interval = args.interval or int(config.base_timeframe.rstrip("m"))
    if args.symbol_type == "stock":
        fetch_config = build_stock_dhan_fetch_config(
            client_id=client_id,
            access_token=access_token,
            interval=interval,
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
        )
    else:
        fetch_config = build_nifty_dhan_fetch_config(
            client_id=client_id,
            access_token=access_token,
            interval=interval,
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
        )

    candles = fetch_dhan_intraday_candles(
        fetch_config,
        parse_timestamp(args.from_date),
        parse_timestamp(args.to_date),
    )
    if args.save_fetched_data:
        write_candles_csv(args.save_fetched_data, candles)
        print(f"Fetched candles written to {args.save_fetched_data}")
    return candles


def main() -> None:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()

    config = build_preset_config(args.preset)

    try:
        candles = load_candles(args.data) if args.data else fetch_candles(args, config)
    except ValueError as exc:
        parser.error(str(exc))

    result = run_backtest(candles, config)
    write_trades_csv(args.output, result.trades)

    print(f"{args.symbol_name} intraday {args.preset} backtest complete")
    print_metrics(result.metrics)
    if args.diagnostics:
        print_diagnostics(result.diagnostics)
    print(f"Trades written to {args.output}")


if __name__ == "__main__":
    main()
