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


def build_variants() -> Dict[str, BacktestConfig]:
    standard = build_best_nifty_strategy_config()
    return {
        "standard": standard,
        "two_bias_layers_fvg_only": replace(standard, bias_timeframes=("1w", "1d"), allowed_zone_kinds=("fvg",)),
        "two_bias_layers_session_1200": replace(standard, bias_timeframes=("1w", "1d"), session_end="12:00"),
        "two_bias_layers_confirmation_2": replace(standard, bias_timeframes=("1w", "1d"), confirmation_lookback_bars=2),
        "two_bias_layers_fvg_only_session_1200": replace(
            standard,
            bias_timeframes=("1w", "1d"),
            allowed_zone_kinds=("fvg",),
            session_end="12:00",
        ),
        "two_bias_layers_fvg_only_confirmation_2": replace(
            standard,
            bias_timeframes=("1w", "1d"),
            allowed_zone_kinds=("fvg",),
            confirmation_lookback_bars=2,
        ),
        "two_bias_layers_session_1200_confirmation_2": replace(
            standard,
            bias_timeframes=("1w", "1d"),
            session_end="12:00",
            confirmation_lookback_bars=2,
        ),
        "two_bias_layers_fvg_only_session_1200_confirmation_2": replace(
            standard,
            bias_timeframes=("1w", "1d"),
            allowed_zone_kinds=("fvg",),
            session_end="12:00",
            confirmation_lookback_bars=2,
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare combined intraday filter ideas for NIFTY.")
    parser.add_argument("--data", type=Path, help="Optional local candle CSV instead of fetching from Dhan.")
    parser.add_argument("--from-date", required=True, help="Start date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--to-date", required=True, help="End date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--symbol-name", default="NIFTY")
    parser.add_argument("--security-id")
    parser.add_argument("--exchange-segment", default="IDX_I")
    parser.add_argument("--instrument-type", default="INDEX")
    parser.add_argument(
        "--variants",
        default="standard,two_bias_layers_fvg_only,two_bias_layers_session_1200,two_bias_layers_confirmation_2,two_bias_layers_fvg_only_session_1200,two_bias_layers_fvg_only_confirmation_2,two_bias_layers_session_1200_confirmation_2,two_bias_layers_fvg_only_session_1200_confirmation_2",
        help="Comma-separated combo variant names to test.",
    )
    parser.add_argument("--output", type=Path, default=Path("nifty_combo_lab.csv"))
    parser.add_argument("--show-diagnostics", action="store_true")
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

    available = build_variants()
    selected_names = [name.strip() for name in args.variants.split(",") if name.strip()]
    invalid = [name for name in selected_names if name not in available]
    if invalid:
        parser.error(f"Unknown variant(s): {', '.join(invalid)}")

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
            "variant": name,
            "bias_timeframes": ">".join(config.bias_timeframes),
            "poi_timeframe": config.poi_timeframe,
            "confirmation_lookback_bars": config.confirmation_lookback_bars,
            "allowed_zone_kinds": "all" if config.allowed_zone_kinds is None else "+".join(config.allowed_zone_kinds),
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
    print(f"Combo lab results written to {args.output}")


if __name__ == "__main__":
    main()
