from __future__ import annotations

import argparse
import csv
import os
from dataclasses import replace
from datetime import timedelta
from itertools import product
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from trading_plan_backtest import (
    BacktestConfig,
    BacktestResult,
    Trade,
    build_nifty_dhan_fetch_config,
    compute_metrics,
    fetch_dhan_intraday_candles,
    load_env_file,
    parse_float_list,
    parse_string_list,
    parse_timestamp,
    print_diagnostics,
    resolve_index_security_id,
    run_backtest,
)


def build_base_config(preset: str) -> BacktestConfig:
    if preset == "15m":
        return BacktestConfig(
            base_timeframe="15m",
            bias_timeframes=("1M", "1w", "1d"),
            poi_timeframe="1h",
            confirmation_timeframes=("1h", "15m"),
            confirmation_mode="bos_or_choch",
            confirmation_lookback_bars=3,
            risk_reward=2.0,
            session_start="09:15",
            session_end="10:45",
            force_intraday_exit=True,
            square_off_time="15:20",
        )
    if preset == "5m":
        return BacktestConfig(
            base_timeframe="5m",
            bias_timeframes=("1d", "4h"),
            poi_timeframe="15m",
            confirmation_timeframes=("15m", "5m"),
            confirmation_mode="bos_or_choch",
            confirmation_lookback_bars=2,
            allowed_zone_kinds=("fvg",),
            risk_reward=2.0,
            session_start="09:15",
            session_end="10:45",
            force_intraday_exit=True,
            square_off_time="15:20",
        )
    if preset == "1m":
        return BacktestConfig(
            base_timeframe="1m",
            bias_timeframes=("1d", "4h"),
            poi_timeframe="15m",
            confirmation_timeframes=("15m", "5m"),
            confirmation_mode="bos_or_choch",
            confirmation_lookback_bars=2,
            allowed_zone_kinds=("fvg",),
            risk_reward=2.0,
            session_start="09:15",
            session_end="10:45",
            force_intraday_exit=True,
            square_off_time="15:20",
        )
    raise ValueError(f"Unsupported preset: {preset}")


def parse_bias_variant(name: str, base_config: BacktestConfig) -> Tuple[str, ...]:
    normalized = name.strip().lower()
    if normalized in {"base", "default"}:
        return base_config.bias_timeframes
    if normalized in {"3layer", "3_layer"}:
        return ("1M", "1w", "1d")
    if normalized in {"2layer", "2_layer"}:
        return ("1w", "1d") if base_config.base_timeframe == "15m" else ("1d", "4h")
    raise ValueError(f"Unsupported bias variant: {name}")


def parse_zone_kind_option(name: str, base_config: BacktestConfig) -> Optional[Tuple[str, ...]]:
    normalized = name.strip().lower()
    if normalized in {"base", "default"}:
        return base_config.allowed_zone_kinds
    if normalized == "all":
        return None
    if normalized == "fvg":
        return ("fvg",)
    if normalized in {"order_block", "ob"}:
        return ("order_block",)
    raise ValueError(f"Unsupported zone kind option: {name}")


def parse_session_option(name: str, base_config: BacktestConfig) -> Tuple[Optional[str], Optional[str]]:
    normalized = name.strip().lower()
    if normalized in {"base", "default"}:
        return base_config.session_start, base_config.session_end
    if normalized == "full":
        return None, None
    if "-" not in name:
        raise ValueError(f"Invalid session option: {name}")
    start, end = [part.strip() for part in name.split("-", 1)]
    return start, end


def parse_square_off_option(name: str, base_config: BacktestConfig) -> Tuple[bool, Optional[str]]:
    normalized = name.strip().lower()
    if normalized in {"base", "default"}:
        return base_config.force_intraday_exit, base_config.square_off_time
    if normalized == "none":
        return False, None
    return True, name.strip()


def build_variant_name(
    bias_variant: str,
    zone_option: str,
    session_option: str,
    confirmation_lookback: int,
    confirmation_mode: str,
    poi_timeframe: str,
    risk_reward: float,
    square_off: str,
) -> str:
    parts = [
        bias_variant,
        zone_option,
        session_option.replace(":", "").replace("-", "_"),
        f"lookback_{confirmation_lookback}",
        confirmation_mode,
        f"poi_{poi_timeframe}",
        f"rr_{risk_reward:.2f}".replace(".", "_"),
        f"sq_{square_off.replace(':', '')}" if square_off.lower() != "none" else "sq_none",
    ]
    return "__".join(parts)


def build_rows(
    base_config: BacktestConfig,
    bias_variants: Iterable[str],
    zone_options: Iterable[str],
    session_options: Iterable[str],
    confirmation_lookbacks: Iterable[int],
    confirmation_modes: Iterable[str],
    poi_timeframes: Iterable[str],
    risk_rewards: Iterable[float],
    square_off_options: Iterable[str],
    candles,
    report_start,
    report_end,
    show_diagnostics: bool,
) -> List[Dict[str, str | float]]:
    rows: List[Dict[str, str | float]] = []
    for (
        bias_variant,
        zone_option,
        session_option,
        confirmation_lookback,
        confirmation_mode,
        poi_timeframe,
        risk_reward,
        square_off_option,
    ) in product(
        bias_variants,
        zone_options,
        session_options,
        confirmation_lookbacks,
        confirmation_modes,
        poi_timeframes,
        risk_rewards,
        square_off_options,
    ):
        config = replace(
            base_config,
            bias_timeframes=parse_bias_variant(bias_variant, base_config),
            allowed_zone_kinds=parse_zone_kind_option(zone_option, base_config),
            session_start=parse_session_option(session_option, base_config)[0],
            session_end=parse_session_option(session_option, base_config)[1],
            confirmation_lookback_bars=confirmation_lookback,
            confirmation_mode=confirmation_mode,
            poi_timeframe=poi_timeframe,
            risk_reward=risk_reward,
            force_intraday_exit=parse_square_off_option(square_off_option, base_config)[0],
            square_off_time=parse_square_off_option(square_off_option, base_config)[1],
        )
        full_result = run_backtest(candles, config)
        filtered_trades = [
            trade for trade in full_result.trades if report_start <= trade.entry_time <= report_end
        ]
        result = BacktestResult(
            trades=filtered_trades,
            metrics=compute_metrics(filtered_trades, config.starting_equity, config.risk_per_trade),
            diagnostics=full_result.diagnostics,
        )
        session_label = "full_session" if not config.session_start else f"{config.session_start}-{config.session_end}"
        zone_label = "all" if config.allowed_zone_kinds is None else "+".join(config.allowed_zone_kinds)
        square_off_label = config.square_off_time or "none"
        row = {
            "variant": build_variant_name(
                bias_variant,
                zone_option,
                session_option,
                confirmation_lookback,
                confirmation_mode,
                poi_timeframe,
                risk_reward,
                square_off_label,
            ),
            "base_timeframe": config.base_timeframe,
            "bias_timeframes": ">".join(config.bias_timeframes),
            "poi_timeframe": config.poi_timeframe,
            "confirmation_timeframes": ">".join(config.confirmation_timeframes),
            "confirmation_mode": config.confirmation_mode,
            "confirmation_lookback_bars": config.confirmation_lookback_bars,
            "allowed_zone_kinds": zone_label,
            "session": session_label,
            "square_off": square_off_label,
            "trades": int(result.metrics["trades"]),
            "win_rate": round(result.metrics["win_rate"], 2),
            "avg_r": round(result.metrics["avg_r"], 2),
            "total_r": round(result.metrics["total_r"], 2),
            "ending_equity": round(result.metrics["ending_equity"], 2),
            "return_pct": round(result.metrics["return_pct"], 2),
        }
        rows.append(row)
        print(
            f"{row['variant']}: trades={row['trades']}, win_rate={row['win_rate']:.2f}%, "
            f"avg_r={row['avg_r']:.2f}, total_r={row['total_r']:.2f}, return_pct={row['return_pct']:.2f}%"
        )
        if show_diagnostics:
            print(f"Diagnostics for {row['variant']}")
            print_diagnostics(result.diagnostics)
    rows.sort(key=lambda item: (float(item["total_r"]), float(item["win_rate"])), reverse=True)
    return rows


def write_rows(path: Path, rows: List[Dict[str, str | float]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a real-Dhan intraday strategy combination sweep for NIFTY.")
    parser.add_argument("--from-date", required=True, help="Start date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--to-date", required=True, help="End date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--context-days", type=int, default=400, help="Extra days to fetch before from-date for context.")
    parser.add_argument("--preset", choices=("15m", "5m", "1m"), default="15m", help="Base intraday preset family.")
    parser.add_argument("--symbol-name", default="NIFTY")
    parser.add_argument("--security-id")
    parser.add_argument("--exchange-segment", default="IDX_I")
    parser.add_argument("--instrument-type", default="INDEX")
    parser.add_argument("--bias-variants", default="3layer,2layer", help="Comma-separated: 3layer,2layer,base")
    parser.add_argument("--zone-kinds", default="all,fvg,order_block", help="Comma-separated: all,fvg,order_block,base")
    parser.add_argument("--sessions", default="09:15-10:45,09:15-12:00,full", help="Comma-separated windows or full")
    parser.add_argument("--confirmation-lookbacks", default="2,3,5", help="Comma-separated integers")
    parser.add_argument("--confirmation-modes", default="bos_or_choch,bos_only", help="Comma-separated modes")
    parser.add_argument("--poi-timeframes", default="1h", help="Comma-separated POI timeframes")
    parser.add_argument("--rr-values", default="2.0", help="Comma-separated RR values")
    parser.add_argument("--square-offs", default="15:20,none", help="Comma-separated HH:MM, none, or base")
    parser.add_argument("--output", type=Path, default=Path("nifty_intraday_real_sweep.csv"))
    parser.add_argument("--show-diagnostics", action="store_true")
    parser.add_argument("--top", type=int, default=20, help="How many top rows to print again after sorting.")
    return parser


def main() -> None:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()

    client_id = os.getenv("DHAN_CLIENT_ID")
    access_token = os.getenv("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        parser.error("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for Dhan fetches.")

    base_config = build_base_config(args.preset)
    interval_map = {"15m": 15, "5m": 5, "1m": 1}
    interval = interval_map[args.preset]

    valid_modes = {"bos_or_choch", "bos_only", "choch_only", "rejection_candle", "bos_and_rejection"}
    confirmation_modes = parse_string_list(args.confirmation_modes)
    invalid_modes = [item for item in confirmation_modes if item not in valid_modes]
    if invalid_modes:
        parser.error(f"Unsupported confirmation mode(s): {', '.join(invalid_modes)}")

    valid_poi_by_preset = {
        "15m": {"1h", "4h", "1d"},
        "5m": {"15m", "1h"},
        "1m": {"15m", "1h"},
    }
    poi_timeframes = parse_string_list(args.poi_timeframes)
    invalid_poi = [item for item in poi_timeframes if item not in valid_poi_by_preset[args.preset]]
    if invalid_poi:
        parser.error(f"Unsupported POI timeframe(s) for preset {args.preset}: {', '.join(invalid_poi)}")

    security_id = args.security_id or resolve_index_security_id(
        args.symbol_name,
        exchange_segment=args.exchange_segment,
        instrument_type=args.instrument_type,
    )
    if not security_id:
        parser.error(f"Could not resolve a Dhan security id for {args.symbol_name}. Pass --security-id explicitly.")

    report_start = parse_timestamp(args.from_date)
    report_end = parse_timestamp(args.to_date)
    fetch_start = report_start - timedelta(days=args.context_days)
    fetch_config = build_nifty_dhan_fetch_config(
        client_id=client_id,
        access_token=access_token,
        interval=interval,
        security_id=security_id,
        exchange_segment=args.exchange_segment,
        instrument_type=args.instrument_type,
    )
    candles = fetch_dhan_intraday_candles(fetch_config, fetch_start, report_end)

    rows = build_rows(
        base_config=base_config,
        bias_variants=parse_string_list(args.bias_variants),
        zone_options=parse_string_list(args.zone_kinds),
        session_options=parse_string_list(args.sessions),
        confirmation_lookbacks=[int(item) for item in parse_string_list(args.confirmation_lookbacks)],
        confirmation_modes=confirmation_modes,
        poi_timeframes=poi_timeframes,
        risk_rewards=parse_float_list(args.rr_values),
        square_off_options=parse_string_list(args.square_offs),
        candles=candles,
        report_start=report_start,
        report_end=report_end,
        show_diagnostics=args.show_diagnostics,
    )

    write_rows(args.output, rows)
    print("Top Results")
    for row in rows[: args.top]:
        print(
            f"{row['variant']} | trades={row['trades']} | win_rate={row['win_rate']:.2f}% | "
            f"total_r={row['total_r']:.2f} | return_pct={row['return_pct']:.2f}%"
        )
    print(f"Real intraday sweep results written to {args.output}")


if __name__ == "__main__":
    main()
