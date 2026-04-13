from __future__ import annotations

import argparse
import csv
import os
from dataclasses import replace
from pathlib import Path
from typing import Dict, List

from trading_plan_backtest import (
    BacktestConfig,
    build_best_nifty_strategy_config,
    build_nifty_dhan_fetch_config,
    fetch_dhan_intraday_candles,
    load_candles,
    load_env_file,
    parse_timestamp,
    print_diagnostics,
    resolve_index_security_id,
    run_backtest,
)


def build_presets() -> Dict[str, BacktestConfig]:
    standard = build_best_nifty_strategy_config()
    return {
        "standard": standard,
        "reduced_bias": replace(standard, bias_timeframes=("1w", "1d", "4h"), poi_timeframe="1h"),
        "compressed": replace(standard, bias_timeframes=("1w", "1d", "4h"), poi_timeframe="15m", confirmation_timeframes=("15m",)),
        "intraday_fast": replace(standard, bias_timeframes=("1d", "4h", "1h"), poi_timeframe="15m", confirmation_timeframes=("15m",), session_end="11:30"),
        "full_session_fast": replace(
            standard,
            bias_timeframes=("1d", "4h", "1h"),
            poi_timeframe="15m",
            confirmation_timeframes=("15m",),
            session_start=None,
            session_end=None,
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare alternate NIFTY timeframe stacks without changing the main runner.")
    parser.add_argument("--data", type=Path, help="Optional local candle CSV instead of fetching from Dhan.")
    parser.add_argument("--from-date", required=True, help="Start date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--to-date", required=True, help="End date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--symbol-name", default="NIFTY")
    parser.add_argument("--security-id", help="Optional Dhan security id.")
    parser.add_argument("--exchange-segment", default="IDX_I")
    parser.add_argument("--instrument-type", default="INDEX")
    parser.add_argument(
        "--presets",
        default="standard,reduced_bias,compressed,intraday_fast,full_session_fast",
        help="Comma-separated preset names to test.",
    )
    parser.add_argument("--output", type=Path, default=Path("nifty_timeframe_lab.csv"))
    parser.add_argument("--show-diagnostics", action="store_true", help="Print diagnostics for each preset.")
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

    available = build_presets()
    selected_names = [name.strip() for name in args.presets.split(",") if name.strip()]
    invalid = [name for name in selected_names if name not in available]
    if invalid:
        parser.error(f"Unknown preset(s): {', '.join(invalid)}")

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

    rows: List[Dict[str, str | float]] = []
    for name in selected_names:
        config = available[name]
        result = run_backtest(candles, config)
        row = {
            "preset": name,
            "bias_timeframes": ">".join(config.bias_timeframes),
            "poi_timeframe": config.poi_timeframe,
            "confirmation_timeframes": ">".join(config.confirmation_timeframes),
            "session": "full_session" if not config.session_start else f"{config.session_start}-{config.session_end}",
            "trades": int(result.metrics["trades"]),
            "win_rate": round(result.metrics["win_rate"], 2),
            "avg_r": round(result.metrics["avg_r"], 2),
            "total_r": round(result.metrics["total_r"], 2),
            "return_pct": round(result.metrics["return_pct"], 2),
        }
        rows.append(row)
        print(
            f"{name}: trades={row['trades']}, win_rate={row['win_rate']:.2f}%, "
            f"avg_r={row['avg_r']:.2f}, total_r={row['total_r']:.2f}, return_pct={row['return_pct']:.2f}%"
        )
        if args.show_diagnostics:
            print(f"Diagnostics for {name}")
            print_diagnostics(result.diagnostics)

    rows.sort(key=lambda item: float(item["total_r"]), reverse=True)
    write_rows(args.output, rows)
    print(f"Lab results written to {args.output}")


if __name__ == "__main__":
    main()
