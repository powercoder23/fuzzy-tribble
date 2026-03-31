from __future__ import annotations

import argparse
import csv
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

from trading_plan_backtest import (
    Candle,
    Trade,
    build_best_nifty_combo_base_config,
    build_best_nifty_combo_strict_config,
    build_fast_nifty_1m_config,
    build_fast_nifty_5m_config,
    build_nifty_dhan_fetch_config,
    fetch_dhan_intraday_candles,
    infer_candle_interval_minutes,
    load_candles,
    load_env_file,
    parse_string_list,
    parse_timestamp,
    resolve_index_security_id,
    run_backtest,
)


def build_strategy_specs() -> Dict[str, tuple[int, object]]:
    return {
        "winning_15m": (15, build_best_nifty_combo_base_config()),
        "strict_15m": (15, build_best_nifty_combo_strict_config()),
        "fast_5m": (5, build_fast_nifty_5m_config()),
        "fast_1m": (1, build_fast_nifty_1m_config()),
    }


def filter_trades_in_range(trades: List[Trade], start: datetime, end: datetime) -> List[Trade]:
    return [trade for trade in trades if start <= trade.entry_time <= end]


def build_daily_rows(
    strategy_name: str,
    trades: List[Trade],
    report_start: datetime,
    report_end: datetime,
) -> List[Dict[str, str | float]]:
    grouped: Dict[str, List[Trade]] = defaultdict(list)
    for trade in trades:
        grouped[trade.entry_time.strftime("%Y-%m-%d")].append(trade)

    rows: List[Dict[str, str | float]] = []
    current = report_start.date()
    last_date = report_end.date()
    while current <= last_date:
        date_key = current.strftime("%Y-%m-%d")
        day_trades = grouped.get(date_key, [])
        wins = [trade for trade in day_trades if (trade.pnl_r or 0.0) > 0]
        total_r = sum(trade.pnl_r or 0.0 for trade in day_trades)
        rows.append(
            {
                "strategy": strategy_name,
                "date": date_key,
                "trades": len(day_trades),
                "win_rate": round((len(wins) / len(day_trades) * 100.0) if day_trades else 0.0, 2),
                "avg_r": round((total_r / len(day_trades)) if day_trades else 0.0, 2),
                "total_r": round(total_r, 2),
            }
        )
        current += timedelta(days=1)
    return rows


def write_rows(path: Path, rows: List[Dict[str, str | float]]) -> None:
    if not rows:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["message"])
            writer.writerow(["No trades in selected date range"])
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def print_rows(rows: List[Dict[str, str | float]]) -> None:
    if not rows:
        print("No trades in selected date range.")
        return
    for row in rows:
        print(
            f"{row['strategy']} | {row['date']} | trades={row['trades']} | "
            f"win_rate={float(row['win_rate']):.2f}% | avg_r={float(row['avg_r']):.2f} | total_r={float(row['total_r']):.2f}"
        )


def fetch_strategy_candles(
    args: argparse.Namespace,
    interval: int,
    report_start: datetime,
    report_end: datetime,
) -> List[Candle]:
    if args.data:
        candles = load_candles(args.data)
        detected_interval = infer_candle_interval_minutes(candles)
        if detected_interval and detected_interval != interval:
            raise RuntimeError(
                f"{args.data} appears to contain {detected_interval}-minute candles, "
                f"but strategy interval {interval}m was requested. Use Dhan fetch for this strategy "
                "or provide a matching CSV."
            )
        return candles

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

    fetch_start = report_start - timedelta(days=args.context_days)
    fetch_config = build_nifty_dhan_fetch_config(
        client_id=client_id,
        access_token=access_token,
        interval=interval,
        security_id=security_id,
        exchange_segment=args.exchange_segment,
        instrument_type=args.instrument_type,
    )
    return fetch_dhan_intraday_candles(fetch_config, fetch_start, report_end)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report day-by-day trades over a selected date range for NIFTY strategies.")
    parser.add_argument("--data", type=Path, help="Optional local candle CSV instead of fetching from Dhan.")
    parser.add_argument("--from-date", required=True, help="Report start in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--to-date", required=True, help="Report end in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--context-days", type=int, default=400, help="Extra historical days to fetch before report start for strategy context.")
    parser.add_argument("--symbol-name", default="NIFTY")
    parser.add_argument("--security-id")
    parser.add_argument("--exchange-segment", default="IDX_I")
    parser.add_argument("--instrument-type", default="INDEX")
    parser.add_argument(
        "--strategies",
        default="winning_15m,fast_5m,fast_1m",
        help="Comma-separated strategies: winning_15m, strict_15m, fast_5m, fast_1m",
    )
    parser.add_argument("--output", type=Path, default=Path("nifty_multi_day_report.csv"))
    parser.add_argument("--trades-output", type=Path, default=Path("nifty_multi_day_trades.csv"))
    return parser


def main() -> None:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()

    specs = build_strategy_specs()
    strategy_names = parse_string_list(args.strategies)
    invalid = [name for name in strategy_names if name not in specs]
    if invalid:
        parser.error(f"Unknown strategy name(s): {', '.join(invalid)}")

    report_start = parse_timestamp(args.from_date)
    report_end = parse_timestamp(args.to_date)
    all_daily_rows: List[Dict[str, str | float]] = []
    all_trade_rows: List[Dict[str, str | float]] = []
    candle_cache: Dict[int, List[Candle]] = {}

    for strategy_name in strategy_names:
        interval, config = specs[strategy_name]
        if interval not in candle_cache:
            candle_cache[interval] = fetch_strategy_candles(args, interval, report_start, report_end)
        result = run_backtest(candle_cache[interval], config)
        filtered_trades = filter_trades_in_range(result.trades, report_start, report_end)
        all_daily_rows.extend(build_daily_rows(strategy_name, filtered_trades, report_start, report_end))
        for trade in filtered_trades:
            all_trade_rows.append(
                {
                    "strategy": strategy_name,
                    "direction": trade.direction,
                    "entry_time": trade.entry_time.isoformat(sep=" "),
                    "entry_price": round(trade.entry_price, 5),
                    "stop_price": round(trade.stop_price, 5),
                    "target_price": round(trade.target_price, 5),
                    "exit_time": trade.exit_time.isoformat(sep=" ") if trade.exit_time else "",
                    "exit_price": round(trade.exit_price, 5) if trade.exit_price is not None else "",
                    "exit_reason": trade.exit_reason or "",
                    "pnl_r": round(trade.pnl_r or 0.0, 3),
                    "zone_kind": trade.setup_zone.kind if trade.setup_zone else "",
                    "zone_timeframe": trade.setup_zone.timeframe if trade.setup_zone else "",
                }
            )

    all_daily_rows.sort(key=lambda row: (str(row["strategy"]), str(row["date"])))
    all_trade_rows.sort(key=lambda row: (str(row["strategy"]), str(row["entry_time"])))
    print_rows(all_daily_rows)
    write_rows(args.output, all_daily_rows)
    write_rows(args.trades_output, all_trade_rows)
    print(f"Daily report written to {args.output}")
    print(f"Trade list written to {args.trades_output}")


if __name__ == "__main__":
    main()
