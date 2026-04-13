from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Dict, List

from trading_plan_backtest import (
    Candle,
    Trade,
    build_nifty_dhan_fetch_config,
    fetch_dhan_intraday_candles,
    load_candles,
    load_env_file,
    parse_timestamp,
    resolve_index_security_id,
    write_candles_csv,
)


RANGE_START = time(9, 30)
RANGE_END = time(9, 45)
SQUARE_OFF_TIME = time(15, 15)
DEFAULT_TRAIL_STEP_POINTS = 100.0
DEFAULT_STOP_POINTS = 100.0
DEFAULT_LOT_SIZE = 30
DEFAULT_BREAKOUT_BUFFER = 5.0
DEFAULT_SLIPPAGE_POINTS = 5.0
DEFAULT_INITIAL_CAPITAL = 2_000_000.0


@dataclass
class DayRange:
    trade_date: date
    high: float
    low: float


@dataclass
class TradeReportRow:
    trade_date: date
    range_high: float
    range_low: float
    breakout_level: float
    trade: Trade
    initial_stop_price: float
    risk_points: float
    risk_per_trade: float
    steps_reached: int
    max_favorable_points: float
    locked_points_at_exit: float
    pnl_points: float
    pnl_amount: float
    pnl_r: float
    slippage_cost: float
    equity_before: float = 0.0
    equity: float = 0.0
    peak_equity: float = 0.0
    drawdown: float = 0.0
    drawdown_pct: float = 0.0
    trade_return: float = 0.0

    @property
    def holding_minutes(self) -> float:
        if self.trade.exit_time is None:
            return 0.0
        return (self.trade.exit_time - self.trade.entry_time).total_seconds() / 60.0

    @property
    def entry_clock(self) -> str:
        return self.trade.entry_time.strftime("%H:%M")

    @property
    def exit_clock(self) -> str:
        if self.trade.exit_time is None:
            return ""
        return self.trade.exit_time.strftime("%H:%M")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backtest a BANKNIFTY opening-range breakout with realistic fills, trailing stop, and equity tracking."
    )
    parser.add_argument("--data", type=Path, help="Optional local candle CSV instead of fetching from Dhan.")
    parser.add_argument("--from-date", required=True, help="Start date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--to-date", required=True, help="End date in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--interval", type=int, default=1, help="Dhan candle interval in minutes. Default is 1.")
    parser.add_argument("--symbol-name", default="BANKNIFTY", help="Display name for logs and Dhan symbol resolution.")
    parser.add_argument("--security-id", help="Optional Dhan security id. If omitted, the script will try to resolve it.")
    parser.add_argument("--exchange-segment", default="IDX_I")
    parser.add_argument("--instrument-type", default="INDEX")
    parser.add_argument("--trail-step-points", type=float, default=DEFAULT_TRAIL_STEP_POINTS)
    parser.add_argument("--stop-points", type=float, default=DEFAULT_STOP_POINTS)
    parser.add_argument("--breakout-buffer", type=float, default=DEFAULT_BREAKOUT_BUFFER)
    parser.add_argument("--slippage-points", type=float, default=DEFAULT_SLIPPAGE_POINTS)
    parser.add_argument("--lot-size", type=int, default=DEFAULT_LOT_SIZE, help="Quantity per trade for money calculations.")
    parser.add_argument("--initial-capital", type=float, default=DEFAULT_INITIAL_CAPITAL)
    parser.add_argument("--save-fetched-data", type=Path, help="Optional CSV path to save fetched candles.")
    parser.add_argument("--output", type=Path, default=Path("banknifty_opening_range_breakout_trades.csv"))
    return parser


def fetch_candles(args: argparse.Namespace) -> List[Candle]:
    client_id = os.getenv("DHAN_CLIENT_ID")
    access_token = os.getenv("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        raise ValueError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for Dhan fetches.")

    security_id = args.security_id or resolve_index_security_id(
        args.symbol_name,
        exchange_segment=args.exchange_segment,
        instrument_type=args.instrument_type,
    )
    if not security_id:
        raise ValueError(f"Could not resolve a Dhan security id for {args.symbol_name}. Pass --security-id explicitly.")

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
    if args.save_fetched_data:
        write_candles_csv(args.save_fetched_data, candles)
        print(f"Fetched candles written to {args.save_fetched_data}")
    return candles


def group_candles_by_day(candles: List[Candle]) -> Dict[date, List[Candle]]:
    grouped: Dict[date, List[Candle]] = {}
    for candle in candles:
        grouped.setdefault(candle.timestamp.date(), []).append(candle)
    for trade_date in grouped:
        grouped[trade_date].sort(key=lambda candle: candle.timestamp)
    return grouped


def build_day_range(day_candles: List[Candle]) -> DayRange | None:
    range_candles = [candle for candle in day_candles if RANGE_START <= candle.timestamp.time() < RANGE_END]
    if not range_candles:
        return None
    return DayRange(
        trade_date=range_candles[0].timestamp.date(),
        high=max(candle.high for candle in range_candles),
        low=min(candle.low for candle in range_candles),
    )


def filter_tradeable_candles(day_candles: List[Candle]) -> List[Candle]:
    return [candle for candle in day_candles if RANGE_END <= candle.timestamp.time() < SQUARE_OFF_TIME]


def breakout_hit(candle: Candle, day_range: DayRange, breakout_buffer: float) -> tuple[bool, bool]:
    bullish_break = candle.high >= day_range.high + breakout_buffer
    bearish_break = candle.low <= day_range.low - breakout_buffer
    return bullish_break, bearish_break


def create_trade(
    direction: str,
    candle: Candle,
    breakout_level: float,
    breakout_buffer: float,
    slippage_points: float,
    stop_points: float,
    trail_step_points: float,
) -> tuple[Trade, float]:
    if direction == "bullish":
        entry_price = breakout_level + breakout_buffer + slippage_points
        initial_stop_price = entry_price - stop_points
        next_trigger_price = entry_price + trail_step_points
    else:
        entry_price = breakout_level - breakout_buffer - slippage_points
        initial_stop_price = entry_price + stop_points
        next_trigger_price = entry_price - trail_step_points
    trade = Trade(
        direction=direction,
        entry_time=candle.timestamp,
        entry_price=entry_price,
        stop_price=initial_stop_price,
        target_price=next_trigger_price,
    )
    return trade, initial_stop_price


def favorable_points_since_entry(trade: Trade, candle: Candle) -> float:
    if trade.direction == "bullish":
        return max(0.0, candle.high - trade.entry_price)
    return max(0.0, trade.entry_price - candle.low)


def stop_touched(trade: Trade, candle: Candle) -> bool:
    if trade.direction == "bullish":
        return candle.low <= trade.stop_price
    return candle.high >= trade.stop_price


def slipped_stop_exit_price(trade: Trade, slippage_points: float) -> float:
    if trade.direction == "bullish":
        return trade.stop_price - slippage_points
    return trade.stop_price + slippage_points


def realized_points(direction: str, entry_price: float, exit_price: float) -> float:
    if direction == "bullish":
        return exit_price - entry_price
    return entry_price - exit_price


def step_count_from_points(points: float, step_size: float) -> int:
    if step_size <= 0:
        return 0
    return int(points // step_size)


def apply_trailing_step(trade: Trade, initial_stop_price: float, steps_reached: int, trail_step_points: float) -> None:
    if trade.direction == "bullish":
        trade.stop_price = initial_stop_price + (steps_reached * trail_step_points)
        trade.target_price = trade.entry_price + ((steps_reached + 1) * trail_step_points)
    else:
        trade.stop_price = initial_stop_price - (steps_reached * trail_step_points)
        trade.target_price = trade.entry_price - ((steps_reached + 1) * trail_step_points)


def current_locked_points(trade: Trade) -> float:
    return realized_points(trade.direction, trade.entry_price, trade.stop_price)


def finalize_trade_row(
    day_range: DayRange,
    breakout_level: float,
    trade: Trade,
    initial_stop_price: float,
    steps_reached: int,
    max_favorable_points: float,
    lot_size: int,
    slippage_points: float,
) -> TradeReportRow:
    pnl_points = realized_points(trade.direction, trade.entry_price, trade.exit_price or trade.entry_price)
    risk_points = abs(trade.entry_price - initial_stop_price)
    pnl_amount = pnl_points * lot_size
    pnl_r = (pnl_points / risk_points) if risk_points else 0.0
    stop_exit_slippage = slippage_points if trade.exit_reason == "trail_stop" else 0.0
    slippage_cost = (slippage_points + stop_exit_slippage) * lot_size
    return TradeReportRow(
        trade_date=day_range.trade_date,
        range_high=day_range.high,
        range_low=day_range.low,
        breakout_level=breakout_level,
        trade=trade,
        initial_stop_price=initial_stop_price,
        risk_points=risk_points,
        risk_per_trade=risk_points * lot_size,
        steps_reached=steps_reached,
        max_favorable_points=max_favorable_points,
        locked_points_at_exit=current_locked_points(trade),
        pnl_points=pnl_points,
        pnl_amount=pnl_amount,
        pnl_r=pnl_r,
        slippage_cost=slippage_cost,
    )


def trail_trade(
    day_range: DayRange,
    breakout_level: float,
    trade: Trade,
    initial_stop_price: float,
    candles: List[Candle],
    trail_step_points: float,
    slippage_points: float,
    lot_size: int,
) -> TradeReportRow:
    steps_reached = 0
    max_favorable_points = 0.0

    for candle in candles:
        # Conservative intrabar path assumption:
        # bullish trade -> low before high, bearish trade -> high before low.
        if stop_touched(trade, candle):
            trade.exit_time = candle.timestamp
            trade.exit_price = slipped_stop_exit_price(trade, slippage_points)
            trade.exit_reason = "trail_stop"
            return finalize_trade_row(
                day_range=day_range,
                breakout_level=breakout_level,
                trade=trade,
                initial_stop_price=initial_stop_price,
                steps_reached=steps_reached,
                max_favorable_points=max_favorable_points,
                lot_size=lot_size,
                slippage_points=slippage_points,
            )

        candle_favorable_points = favorable_points_since_entry(trade, candle)
        max_favorable_points = max(max_favorable_points, candle_favorable_points)
        new_steps = step_count_from_points(max_favorable_points, trail_step_points)
        if new_steps > steps_reached:
            steps_reached = new_steps
            apply_trailing_step(trade, initial_stop_price, steps_reached, trail_step_points)

    last_candle = candles[-1]
    trade.exit_time = last_candle.timestamp
    trade.exit_price = last_candle.close
    trade.exit_reason = "eod_square_off"
    return finalize_trade_row(
        day_range=day_range,
        breakout_level=breakout_level,
        trade=trade,
        initial_stop_price=initial_stop_price,
        steps_reached=steps_reached,
        max_favorable_points=max_favorable_points,
        lot_size=lot_size,
        slippage_points=slippage_points,
    )


def find_trade_for_day(
    day_candles: List[Candle],
    day_range: DayRange,
    breakout_buffer: float,
    stop_points: float,
    trail_step_points: float,
    slippage_points: float,
    lot_size: int,
) -> tuple[TradeReportRow | None, bool]:
    tradeable_candles = filter_tradeable_candles(day_candles)
    if not tradeable_candles:
        return None, False

    for index, candle in enumerate(tradeable_candles):
        bullish_break, bearish_break = breakout_hit(candle, day_range, breakout_buffer)
        if bullish_break and bearish_break:
            return None, True
        if not bullish_break and not bearish_break:
            continue

        direction = "bullish" if bullish_break else "bearish"
        breakout_level = day_range.high if bullish_break else day_range.low
        trade, initial_stop_price = create_trade(
            direction=direction,
            candle=candle,
            breakout_level=breakout_level,
            breakout_buffer=breakout_buffer,
            slippage_points=slippage_points,
            stop_points=stop_points,
            trail_step_points=trail_step_points,
        )

        remaining_candles = tradeable_candles[index + 1 :]
        if not remaining_candles:
            trade.exit_time = candle.timestamp
            trade.exit_price = candle.close
            trade.exit_reason = "eod_square_off"
            return (
                finalize_trade_row(
                    day_range=day_range,
                    breakout_level=breakout_level,
                    trade=trade,
                    initial_stop_price=initial_stop_price,
                    steps_reached=0,
                    max_favorable_points=0.0,
                    lot_size=lot_size,
                    slippage_points=slippage_points,
                ),
                False,
            )

        return (
            trail_trade(
                day_range=day_range,
                breakout_level=breakout_level,
                trade=trade,
                initial_stop_price=initial_stop_price,
                candles=remaining_candles,
                trail_step_points=trail_step_points,
                slippage_points=slippage_points,
                lot_size=lot_size,
            ),
            False,
        )

    return None, False


def apply_equity_curve(rows: List[TradeReportRow], initial_capital: float) -> None:
    equity = initial_capital
    peak_equity = initial_capital

    for row in rows:
        row.equity_before = equity
        equity += row.pnl_amount
        peak_equity = max(peak_equity, equity)
        row.equity = equity
        row.peak_equity = peak_equity
        row.drawdown = peak_equity - equity
        row.drawdown_pct = (row.drawdown / peak_equity * 100.0) if peak_equity else 0.0
        row.trade_return = (row.pnl_amount / row.equity_before) if row.equity_before else 0.0


def compute_sharpe_ratio(rows: List[TradeReportRow]) -> float:
    returns = [row.trade_return for row in rows]
    if len(returns) < 2:
        return 0.0
    stdev = statistics.stdev(returns)
    if stdev == 0:
        return 0.0
    return (statistics.mean(returns) / stdev) * math.sqrt(len(returns))


def compute_expectancy(rows: List[TradeReportRow]) -> float:
    if not rows:
        return 0.0
    wins = [row.pnl_r for row in rows if row.pnl_r > 0]
    losses = [row.pnl_r for row in rows if row.pnl_r <= 0]
    win_rate = len(wins) / len(rows)
    loss_rate = len(losses) / len(rows)
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    return (win_rate * avg_win) + (loss_rate * avg_loss)


def average(values: List[float]) -> float:
    return statistics.mean(values) if values else 0.0


def median(values: List[float]) -> float:
    return statistics.median(values) if values else 0.0


def build_profitability_breakdown(rows: List[TradeReportRow]) -> dict[str, float]:
    winners = [row for row in rows if row.pnl_amount > 0]
    losers = [row for row in rows if row.pnl_amount <= 0]
    bullish_winners = [row for row in winners if row.trade.direction == "bullish"]
    bearish_winners = [row for row in winners if row.trade.direction == "bearish"]
    eod_winners = [row for row in winners if row.trade.exit_reason == "eod_square_off"]
    trail_winners = [row for row in winners if row.trade.exit_reason == "trail_stop"]
    early_winners = [row for row in winners if row.trade.entry_time.time() <= time(10, 0)]
    late_winners = [row for row in winners if row.trade.entry_time.time() > time(10, 0)]
    multi_step_winners = [row for row in winners if row.steps_reached >= 2]
    runners = [row for row in rows if row.steps_reached >= 3]
    best_trade = max(rows, key=lambda row: row.pnl_amount, default=None)
    worst_trade = min(rows, key=lambda row: row.pnl_amount, default=None)

    return {
        "winner_count": float(len(winners)),
        "loser_count": float(len(losers)),
        "winner_share_long": (len(bullish_winners) / len(winners) * 100.0) if winners else 0.0,
        "winner_share_short": (len(bearish_winners) / len(winners) * 100.0) if winners else 0.0,
        "winner_share_eod": (len(eod_winners) / len(winners) * 100.0) if winners else 0.0,
        "winner_share_trail": (len(trail_winners) / len(winners) * 100.0) if winners else 0.0,
        "winner_share_early_entry": (len(early_winners) / len(winners) * 100.0) if winners else 0.0,
        "winner_share_late_entry": (len(late_winners) / len(winners) * 100.0) if winners else 0.0,
        "winner_share_multi_step": (len(multi_step_winners) / len(winners) * 100.0) if winners else 0.0,
        "avg_winner_pnl": average([row.pnl_amount for row in winners]),
        "avg_loser_pnl": average([row.pnl_amount for row in losers]),
        "avg_winner_points": average([row.pnl_points for row in winners]),
        "avg_loser_points": average([row.pnl_points for row in losers]),
        "avg_winner_hold": average([row.holding_minutes for row in winners]),
        "avg_loser_hold": average([row.holding_minutes for row in losers]),
        "avg_winner_steps": average([float(row.steps_reached) for row in winners]),
        "avg_loser_steps": average([float(row.steps_reached) for row in losers]),
        "avg_winner_mfe": average([row.max_favorable_points for row in winners]),
        "avg_loser_mfe": average([row.max_favorable_points for row in losers]),
        "median_winner_pnl": median([row.pnl_amount for row in winners]),
        "median_loser_pnl": median([row.pnl_amount for row in losers]),
        "runner_count": float(len(runners)),
        "runner_share": (len(runners) / len(rows) * 100.0) if rows else 0.0,
        "best_trade_pnl": best_trade.pnl_amount if best_trade else 0.0,
        "best_trade_steps": float(best_trade.steps_reached) if best_trade else 0.0,
        "best_trade_mfe": best_trade.max_favorable_points if best_trade else 0.0,
        "worst_trade_pnl": worst_trade.pnl_amount if worst_trade else 0.0,
        "worst_trade_steps": float(worst_trade.steps_reached) if worst_trade else 0.0,
        "worst_trade_mfe": worst_trade.max_favorable_points if worst_trade else 0.0,
    }


def print_profitability_breakdown(rows: List[TradeReportRow]) -> None:
    breakdown = build_profitability_breakdown(rows)
    if not rows:
        print("No trades available for profitability diagnostics.")
        return
    print("Profitability diagnostics:")
    print(f"Winners: {int(breakdown['winner_count'])}")
    print(f"Losers: {int(breakdown['loser_count'])}")
    print(f"Winning trades that were long: {breakdown['winner_share_long']:.2f}%")
    print(f"Winning trades that were short: {breakdown['winner_share_short']:.2f}%")
    print(f"Winning trades exited by trail stop: {breakdown['winner_share_trail']:.2f}%")
    print(f"Winning trades exited at EOD: {breakdown['winner_share_eod']:.2f}%")
    print(f"Winning trades entered by 10:00: {breakdown['winner_share_early_entry']:.2f}%")
    print(f"Winning trades entered after 10:00: {breakdown['winner_share_late_entry']:.2f}%")
    print(f"Winning trades reaching at least 2 steps: {breakdown['winner_share_multi_step']:.2f}%")
    print(f"Average winner P&L: {breakdown['avg_winner_pnl']:.2f}")
    print(f"Average loser P&L: {breakdown['avg_loser_pnl']:.2f}")
    print(f"Average winner points: {breakdown['avg_winner_points']:.2f}")
    print(f"Average loser points: {breakdown['avg_loser_points']:.2f}")
    print(f"Average winner hold time: {breakdown['avg_winner_hold']:.2f} minutes")
    print(f"Average loser hold time: {breakdown['avg_loser_hold']:.2f} minutes")
    print(f"Average winner steps: {breakdown['avg_winner_steps']:.2f}")
    print(f"Average loser steps: {breakdown['avg_loser_steps']:.2f}")
    print(f"Average winner max favorable excursion: {breakdown['avg_winner_mfe']:.2f} points")
    print(f"Average loser max favorable excursion: {breakdown['avg_loser_mfe']:.2f} points")
    print(f"Median winner P&L: {breakdown['median_winner_pnl']:.2f}")
    print(f"Median loser P&L: {breakdown['median_loser_pnl']:.2f}")
    print(f"Trades reaching at least 3 steps: {int(breakdown['runner_count'])} ({breakdown['runner_share']:.2f}%)")
    print(f"Best trade P&L: {breakdown['best_trade_pnl']:.2f}")
    print(f"Best trade steps reached: {breakdown['best_trade_steps']:.0f}")
    print(f"Best trade max favorable excursion: {breakdown['best_trade_mfe']:.2f}")
    print(f"Worst trade P&L: {breakdown['worst_trade_pnl']:.2f}")
    print(f"Worst trade steps reached: {breakdown['worst_trade_steps']:.0f}")
    print(f"Worst trade max favorable excursion: {breakdown['worst_trade_mfe']:.2f}")


def write_reason_report(path: Path, rows: List[TradeReportRow]) -> None:
    summary_path = path.with_name(f"{path.stem}_profitability_summary.csv")
    by_direction_path = path.with_name(f"{path.stem}_profitability_by_direction.csv")
    by_exit_path = path.with_name(f"{path.stem}_profitability_by_exit.csv")
    by_entry_time_path = path.with_name(f"{path.stem}_profitability_by_entry_time.csv")
    by_steps_path = path.with_name(f"{path.stem}_profitability_by_steps.csv")

    winners = [row for row in rows if row.pnl_amount > 0]
    losers = [row for row in rows if row.pnl_amount <= 0]

    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        for key, value in build_profitability_breakdown(rows).items():
            if isinstance(value, float):
                writer.writerow([key, f"{value:.4f}"])
            else:
                writer.writerow([key, value])

    def write_group_report(report_path: Path, group_rows: List[tuple[str, List[TradeReportRow]]], label: str) -> None:
        with report_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    label,
                    "trades",
                    "wins",
                    "losses",
                    "win_rate",
                    "net_pnl",
                    "avg_pnl",
                    "avg_points",
                    "avg_steps",
                    "avg_mfe",
                    "avg_hold_minutes",
                ]
            )
            for group_name, group in group_rows:
                win_count = len([row for row in group if row.pnl_amount > 0])
                loss_count = len(group) - win_count
                writer.writerow(
                    [
                        group_name,
                        str(len(group)),
                        str(win_count),
                        str(loss_count),
                        f"{(win_count / len(group) * 100.0) if group else 0.0:.2f}",
                        f"{sum(row.pnl_amount for row in group):.2f}",
                        f"{average([row.pnl_amount for row in group]):.2f}",
                        f"{average([row.pnl_points for row in group]):.2f}",
                        f"{average([float(row.steps_reached) for row in group]):.2f}",
                        f"{average([row.max_favorable_points for row in group]):.2f}",
                        f"{average([row.holding_minutes for row in group]):.2f}",
                    ]
                )

    write_group_report(
        by_direction_path,
        [
            ("bullish", [row for row in rows if row.trade.direction == "bullish"]),
            ("bearish", [row for row in rows if row.trade.direction == "bearish"]),
        ],
        "direction",
    )
    write_group_report(
        by_exit_path,
        [
            ("trail_stop", [row for row in rows if row.trade.exit_reason == "trail_stop"]),
            ("eod_square_off", [row for row in rows if row.trade.exit_reason == "eod_square_off"]),
        ],
        "exit_reason",
    )
    write_group_report(
        by_entry_time_path,
        [
            ("09:45-10:00", [row for row in rows if row.trade.entry_time.time() <= time(10, 0)]),
            ("10:01-11:00", [row for row in rows if time(10, 0) < row.trade.entry_time.time() <= time(11, 0)]),
            ("11:01-15:14", [row for row in rows if row.trade.entry_time.time() > time(11, 0)]),
        ],
        "entry_bucket",
    )
    step_groups: List[tuple[str, List[TradeReportRow]]] = [
        ("0", [row for row in rows if row.steps_reached == 0]),
        ("1", [row for row in rows if row.steps_reached == 1]),
        ("2", [row for row in rows if row.steps_reached == 2]),
        ("3+", [row for row in rows if row.steps_reached >= 3]),
    ]
    write_group_report(by_steps_path, step_groups, "steps_bucket")


def compute_summary(rows: List[TradeReportRow], total_days: int, skipped_days: int, initial_capital: float) -> dict[str, float]:
    wins = [row for row in rows if row.pnl_amount > 0]
    losses = [row for row in rows if row.pnl_amount <= 0]
    long_trades = [row for row in rows if row.trade.direction == "bullish"]
    short_trades = [row for row in rows if row.trade.direction == "bearish"]
    trail_stop_exits = [row for row in rows if row.trade.exit_reason == "trail_stop"]
    eod_exits = [row for row in rows if row.trade.exit_reason == "eod_square_off"]
    steps = [row.steps_reached for row in rows]
    max_favorable = [row.max_favorable_points for row in rows]
    net_pnl = sum(row.pnl_amount for row in rows)
    net_points = sum(row.pnl_points for row in rows)
    avg_r = statistics.mean([row.pnl_r for row in rows]) if rows else 0.0
    expectancy = compute_expectancy(rows)
    max_drawdown_pct = max((row.drawdown_pct for row in rows), default=0.0)
    sharpe_ratio = compute_sharpe_ratio(rows)
    no_trade_days = max(0, total_days - len(rows) - skipped_days)
    final_equity = rows[-1].equity if rows else initial_capital

    return {
        "days": float(total_days),
        "traded_days": float(len(rows)),
        "no_trade_days": float(no_trade_days),
        "skipped_days": float(skipped_days),
        "trades": float(len(rows)),
        "long_trades": float(len(long_trades)),
        "short_trades": float(len(short_trades)),
        "wins": float(len(wins)),
        "losses": float(len(losses)),
        "trail_stop_exits": float(len(trail_stop_exits)),
        "eod_exits": float(len(eod_exits)),
        "win_rate": (len(wins) / len(rows) * 100.0) if rows else 0.0,
        "net_points": net_points,
        "net_pnl": net_pnl,
        "avg_points": (net_points / len(rows)) if rows else 0.0,
        "avg_pnl": (net_pnl / len(rows)) if rows else 0.0,
        "avg_r": avg_r,
        "expectancy": expectancy,
        "max_drawdown_pct": max_drawdown_pct,
        "sharpe_ratio": sharpe_ratio,
        "avg_hold_minutes": statistics.mean([row.holding_minutes for row in rows]) if rows else 0.0,
        "min_hold_minutes": min((row.holding_minutes for row in rows), default=0.0),
        "max_hold_minutes": max((row.holding_minutes for row in rows), default=0.0),
        "avg_risk_per_trade": statistics.mean([row.risk_per_trade for row in rows]) if rows else 0.0,
        "avg_slippage_cost": statistics.mean([row.slippage_cost for row in rows]) if rows else 0.0,
        "total_slippage_cost": sum(row.slippage_cost for row in rows),
        "avg_steps_reached": statistics.mean(steps) if steps else 0.0,
        "max_steps_reached": max(steps) if steps else 0.0,
        "avg_max_favorable_points": statistics.mean(max_favorable) if max_favorable else 0.0,
        "best_max_favorable_points": max(max_favorable) if max_favorable else 0.0,
        "starting_equity": initial_capital,
        "final_equity": final_equity,
    }


def write_trade_report(path: Path, rows: List[TradeReportRow]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "trade_date",
                "direction",
                "range_high",
                "range_low",
                "breakout_level",
                "entry_time",
                "entry_price",
                "initial_stop_price",
                "current_stop_price",
                "next_trailing_trigger",
                "exit_time",
                "exit_price",
                "exit_reason",
                "steps_reached",
                "max_favorable_points",
                "locked_points_at_exit",
                "holding_minutes",
                "risk_points",
                "risk_per_trade",
                "pnl_points",
                "pnl_amount",
                "pnl_r",
                "slippage_cost",
                "equity_before",
                "equity",
                "peak_equity",
                "drawdown",
                "drawdown_pct",
                "trade_return",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.trade_date.isoformat(),
                    row.trade.direction,
                    f"{row.range_high:.5f}",
                    f"{row.range_low:.5f}",
                    f"{row.breakout_level:.5f}",
                    row.trade.entry_time.isoformat(sep=" "),
                    f"{row.trade.entry_price:.5f}",
                    f"{row.initial_stop_price:.5f}",
                    f"{row.trade.stop_price:.5f}",
                    f"{row.trade.target_price:.5f}",
                    row.trade.exit_time.isoformat(sep=" ") if row.trade.exit_time else "",
                    f"{row.trade.exit_price:.5f}" if row.trade.exit_price is not None else "",
                    row.trade.exit_reason or "",
                    str(row.steps_reached),
                    f"{row.max_favorable_points:.2f}",
                    f"{row.locked_points_at_exit:.2f}",
                    f"{row.holding_minutes:.2f}",
                    f"{row.risk_points:.2f}",
                    f"{row.risk_per_trade:.2f}",
                    f"{row.pnl_points:.2f}",
                    f"{row.pnl_amount:.2f}",
                    f"{row.pnl_r:.4f}",
                    f"{row.slippage_cost:.2f}",
                    f"{row.equity_before:.2f}",
                    f"{row.equity:.2f}",
                    f"{row.peak_equity:.2f}",
                    f"{row.drawdown:.2f}",
                    f"{row.drawdown_pct:.4f}",
                    f"{row.trade_return:.6f}",
                ]
            )


def print_summary(summary: dict[str, float]) -> None:
    print(f"Trading days scanned: {int(summary['days'])}")
    print(f"Days with trade: {int(summary['traded_days'])}")
    print(f"Days without trade: {int(summary['no_trade_days'])}")
    print(f"Skipped days without valid setup: {int(summary['skipped_days'])}")
    print(f"Trades taken: {int(summary['trades'])}")
    print(f"Long trades: {int(summary['long_trades'])}")
    print(f"Short trades: {int(summary['short_trades'])}")
    print(f"Wins: {int(summary['wins'])}")
    print(f"Losses: {int(summary['losses'])}")
    print(f"Trail stop exits: {int(summary['trail_stop_exits'])}")
    print(f"EOD exits: {int(summary['eod_exits'])}")
    print(f"Starting equity: {summary['starting_equity']:.2f}")
    print(f"Final equity: {summary['final_equity']:.2f}")
    print(f"Net P&L: {summary['net_pnl']:.2f}")
    print(f"Net points: {summary['net_points']:.2f}")
    print(f"Win rate: {summary['win_rate']:.2f}%")
    print(f"Average P&L per trade: {summary['avg_pnl']:.2f}")
    print(f"Average points per trade: {summary['avg_points']:.2f}")
    print(f"Average R: {summary['avg_r']:.4f}")
    print(f"Expectancy: {summary['expectancy']:.4f}")
    print(f"Max drawdown: {summary['max_drawdown_pct']:.4f}%")
    print(f"Sharpe ratio: {summary['sharpe_ratio']:.4f}")
    print(f"Average hold time: {summary['avg_hold_minutes']:.2f} minutes")
    print(f"Min hold time: {summary['min_hold_minutes']:.2f} minutes")
    print(f"Max hold time: {summary['max_hold_minutes']:.2f} minutes")
    print(f"Average risk per trade: {summary['avg_risk_per_trade']:.2f}")
    print(f"Total slippage cost: {summary['total_slippage_cost']:.2f}")
    print(f"Average slippage cost: {summary['avg_slippage_cost']:.2f}")
    print(f"Average 100-point steps reached: {summary['avg_steps_reached']:.2f}")
    print(f"Best steps reached in one trade: {summary['max_steps_reached']:.0f}")
    print(f"Average max favorable move: {summary['avg_max_favorable_points']:.2f} points")
    print(f"Best max favorable move: {summary['best_max_favorable_points']:.2f} points")


def main() -> None:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args()

    try:
        candles = load_candles(args.data) if args.data else fetch_candles(args)
    except ValueError as exc:
        parser.error(str(exc))

    day_groups = group_candles_by_day(candles)
    rows: List[TradeReportRow] = []
    skipped_days = 0

    for _, day_candles in sorted(day_groups.items()):
        day_range = build_day_range(day_candles)
        if day_range is None:
            skipped_days += 1
            continue
        row, skipped_setup = find_trade_for_day(
            day_candles=day_candles,
            day_range=day_range,
            breakout_buffer=args.breakout_buffer,
            stop_points=args.stop_points,
            trail_step_points=args.trail_step_points,
            slippage_points=args.slippage_points,
            lot_size=args.lot_size,
        )
        if skipped_setup:
            skipped_days += 1
            continue
        if row is not None:
            rows.append(row)

    apply_equity_curve(rows, args.initial_capital)
    write_trade_report(args.output, rows)
    summary = compute_summary(
        rows=rows,
        total_days=len(day_groups),
        skipped_days=skipped_days,
        initial_capital=args.initial_capital,
    )

    print(f"{args.symbol_name} 09:30-09:45 breakout backtest complete")
    print_summary(summary)
    print_profitability_breakdown(rows)
    print(f"Trades written to {args.output}")
    write_reason_report(args.output, rows)


if __name__ == "__main__":
    main()
