from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from typing import Dict, List

from trading_plan_backtest import (
    build_best_stock_strategy_config,
    build_stock_dhan_fetch_config,
    fetch_dhan_intraday_candles,
    load_env_file,
    parse_string_list,
    parse_timestamp,
    resolve_symbol_security_id,
    run_backtest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch-backtest a list of F&O stocks using Dhan data.")
    parser.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated underlying stock symbols, for example RELIANCE,HDFCBANK,ICICIBANK,SBIN,INFY,TCS.",
    )
    parser.add_argument("--from-date", required=True, help="Start date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--to-date", required=True, help="End date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--exchange-segment", default="NSE_EQ")
    parser.add_argument("--instrument-type", default="EQUITY")
    parser.add_argument("--output", type=Path, default=Path("stock_batch_comparison.csv"))
    return parser


def write_rows(path: Path, rows: List[Dict[str, str | float]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()

    client_id = os.getenv("DHAN_CLIENT_ID")
    access_token = os.getenv("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        parser.error("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for Dhan fetches.")

    config = build_best_stock_strategy_config()
    rows: List[Dict[str, str | float]] = []

    for symbol in parse_string_list(args.symbols):
        security_id = resolve_symbol_security_id(
            symbol,
            exchange_segment=args.exchange_segment,
            instrument_type=args.instrument_type,
        )
        if not security_id:
            rows.append(
                {
                    "symbol": symbol,
                    "security_id": "",
                    "status": "security_id_not_found",
                    "trades": 0,
                    "win_rate": 0.0,
                    "avg_r": 0.0,
                    "total_r": 0.0,
                    "return_pct": 0.0,
                }
            )
            continue

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
        result = run_backtest(candles, config)
        rows.append(
            {
                "symbol": symbol,
                "security_id": security_id,
                "status": "ok",
                "trades": int(result.metrics["trades"]),
                "win_rate": round(result.metrics["win_rate"], 2),
                "avg_r": round(result.metrics["avg_r"], 2),
                "total_r": round(result.metrics["total_r"], 2),
                "return_pct": round(result.metrics["return_pct"], 2),
            }
        )
        print(
            f"{symbol}: trades={int(result.metrics['trades'])}, "
            f"win_rate={result.metrics['win_rate']:.2f}%, total_r={result.metrics['total_r']:.2f}, "
            f"return_pct={result.metrics['return_pct']:.2f}%"
        )

    rows.sort(key=lambda row: float(row["total_r"]), reverse=True)
    write_rows(args.output, rows)
    print(f"Batch comparison written to {args.output}")


if __name__ == "__main__":
    main()
