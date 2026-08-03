# -*- coding: utf-8 -*-
"""
engine.py — the backtest orchestrator. run_backtest() is called on a
background thread by backtest_routes.py; it fetches candles, finds
historical signals, prices them via Black-Scholes, walks each trade forward
to its exit, and persists everything through results_store.py.

Strategy registry pattern: STRATEGIES maps a strategy key to a small config
of (candle interval, signal-finder, risk config) so adding Break & Bounce /
Vol-Expansion / Discount later is "add one entry + one signals/*.py module",
not a rewrite of this file.
"""

from __future__ import annotations

import logging
import sqlite3
import traceback
from datetime import datetime, time as dt_time, timedelta

import pandas as pd

from backtest import bs_pricer, candle_fetcher, contracts, iv_lookup, results_store
from backtest.signals import momentum as momentum_signals
from momentum_config import ORB, RISK_CONFIG, SCRIP_MASTER_DB, STRIKE

logger = logging.getLogger(__name__)

INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
T1_BOOK_FRACTION = 0.7  # matches this repo's standard two-target convention (test_paper_exits.py)


# --------------------------------------------------------------------------- #
# Strategy registry (Phase 1: momentum only — see plan for the rest)
# --------------------------------------------------------------------------- #
def _momentum_config(params: dict) -> dict:
    return {
        "interval_min": 15,
        "rule": params.get("rule", "both"),  # 'orb' | 'vwap' | 'both'
        "otm_offset": STRIKE["intraday_otm_offset"],
        "sl_pct": RISK_CONFIG["sl_pct"],
        "t1_mult": RISK_CONFIG["target1_mult"],
        "t2_mult": RISK_CONFIG["target2_mult"],
        "force_exit": dt_time(ORB["force_exit_hour"], ORB["force_exit_min"]),
        "find_signals": lambda candles, symbol: momentum_signals.find_signals(
            candles, symbol, params.get("rule", "both")),
    }


STRATEGIES = {
    "momentum": {"label": "Momentum: ORB + VWAP", "build": _momentum_config},
}


def _exchange_segment(symbol: str) -> str:
    return "IDX_I" if symbol in INDEX_SYMBOLS else "NSE_EQ"


def _resolve_security_ids(symbols: list[str]) -> dict[str, str]:
    """{symbol: security_id} via the scrip master, skipping unresolvable names.

    Deliberately a standalone query (not load_scrip_master_sqlite.
    get_security_id_symbol_map) — that module imports `dhanhq` at the top
    level purely for an unrelated marketfeed class, which would drag a heavy,
    unused SDK into the dashboard's Docker image just for this one lookup.
    """
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with sqlite3.connect(SCRIP_MASTER_DB) as conn:
        rows = conn.execute(
            f"SELECT SEM_TRADING_SYMBOL, MIN(CAST(SEM_SMST_SECURITY_ID AS INTEGER)) "
            f"FROM scrip_master WHERE SEM_TRADING_SYMBOL IN ({placeholders}) "
            f"AND SEM_EXM_EXCH_ID='NSE' AND SEM_SEGMENT='E' GROUP BY SEM_TRADING_SYMBOL",
            symbols,
        ).fetchall()
    return {sym: str(sid) for sym, sid in rows if sid is not None}


def _simulate_trade(signal: dict, cfg: dict, day_candles: pd.DataFrame) -> tuple[dict | None, str | None]:
    """Price entry via BS, walk forward applying SL/T1(partial)/T2/force-exit.
    Returns (trade_dict, None) on success, or (None, skip_reason) when the
    trade can't be priced — surfaced in the run summary (skip_reasons) so a
    backtest with 0 trades is explainable rather than an opaque null result."""
    symbol, side = signal["symbol"], signal["side"]
    entry_ts, entry_spot = signal["entry_ts"], signal["entry_price"]
    entry_date = entry_ts.date().isoformat()

    contract = contracts.nearest_expiry(symbol, entry_date, min_dte=0, opt_type=side)
    if not contract:
        return None, "no_listed_expiry"
    expiry_iso, lot_size = contract
    strike = contracts.atm_strike(symbol, expiry_iso, entry_spot, side, cfg["otm_offset"])
    if strike is None:
        return None, "no_strike_grid"

    sigma = iv_lookup.daily_atm_iv(symbol, entry_date)
    if sigma is None:
        return None, "no_historical_iv"

    def t_years(ts: pd.Timestamp) -> float:
        dte_days = (datetime.fromisoformat(expiry_iso).date() - ts.date()).days
        return max(dte_days, 0) / 365.0 + (1.0 / 365.0)  # +1 day floor: same-day expiry still has time value intraday

    entry_premium = bs_pricer.price(entry_spot, strike, t_years(entry_ts), sigma, side)
    if not entry_premium or entry_premium < 0.5:
        return None, "unpriceable_or_too_cheap"

    sl_price = round(entry_premium * (1 - cfg["sl_pct"]), 2)
    t1_price = round(entry_premium * cfg["t1_mult"], 2)
    t2_price = round(entry_premium * cfg["t2_mult"], 2)

    forward = day_candles[day_candles["ts"] > entry_ts].sort_values("ts")
    t1_hit = False
    booked_pnl_per_lot = 0.0  # rupees per lot from any partial (T1) exit already booked
    remaining_frac = 1.0
    last_ts, last_premium = entry_ts, entry_premium
    terminal_ts = terminal_premium = terminal_reason = None

    for _, row in forward.iterrows():
        ts = row["ts"]
        if ts.time() >= cfg["force_exit"]:
            terminal_ts, terminal_premium, terminal_reason = ts, last_premium, "FORCE_EXIT"
            break
        premium = bs_pricer.price(float(row["close"]), strike, t_years(ts), sigma, side)
        if premium is None:
            continue
        last_ts, last_premium = ts, premium
        if premium <= sl_price:
            terminal_ts, terminal_premium, terminal_reason = ts, sl_price, ("SL_AFTER_T1" if t1_hit else "SL")
            break
        if not t1_hit and premium >= t1_price:
            t1_hit = True
            booked_pnl_per_lot += (t1_price - entry_premium) * T1_BOOK_FRACTION
            remaining_frac = 1 - T1_BOOK_FRACTION
            continue
        if premium >= t2_price:
            terminal_ts, terminal_premium, terminal_reason = ts, t2_price, "T2"
            break

    if terminal_reason:
        exit_ts, exit_premium, exit_reason = terminal_ts, terminal_premium, terminal_reason
    else:
        exit_ts, exit_premium = last_ts, last_premium
        exit_reason = "EOD_AFTER_T1" if t1_hit else "EOD"

    runner_pnl_per_lot = (exit_premium - entry_premium) * remaining_frac
    pnl_per_lot = booked_pnl_per_lot + runner_pnl_per_lot
    pnl_rupees = round(pnl_per_lot * lot_size, 2)
    pnl_pct = round(pnl_per_lot / entry_premium * 100, 2) if entry_premium else 0.0

    return {
        "symbol": symbol, "side": side, "strike": strike, "expiry": expiry_iso,
        "entry_ts": entry_ts.isoformat(), "entry_premium": entry_premium,
        "exit_ts": exit_ts.isoformat(), "exit_premium": exit_premium,
        "exit_reason": exit_reason, "pnl_rupees": pnl_rupees, "pnl_pct": pnl_pct,
        "lot_size": lot_size, "trigger": signal.get("trigger"),
    }, None


def _summary(trades: list[dict], signals_found: int = 0, skip_reasons: dict | None = None) -> dict:
    base_meta = {"signals_found": signals_found, "signals_priced": len(trades),
                "skip_reasons": skip_reasons or {}}
    if not trades:
        return {**base_meta, "trades": 0, "net_rupees": 0, "win_rate": 0, "profit_factor": None,
                "max_drawdown": 0, "expectancy": 0, "equity_curve": []}
    pnls = [t["pnl_rupees"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    equity, cum = [], 0.0
    for t in sorted(trades, key=lambda x: x["entry_ts"]):
        cum += t["pnl_rupees"]
        equity.append({"ts": t["entry_ts"], "equity": round(cum, 2)})
    peak, max_dd = 0.0, 0.0
    running = 0.0
    for t in sorted(trades, key=lambda x: x["entry_ts"]):
        running += t["pnl_rupees"]
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        **base_meta,
        "trades": len(trades),
        "net_rupees": round(sum(pnls), 2),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_drawdown": round(max_dd, 2),
        "expectancy": round(sum(pnls) / len(trades), 2),
        "equity_curve": equity,
    }


def run_backtest(run_id: int, strategy: str, symbols: list[str],
                 start_date: str, end_date: str, params: dict | None = None) -> None:
    params = params or {}
    try:
        results_store.update_run(run_id, status="running", progress_pct=0)
        strat = STRATEGIES.get(strategy)
        if not strat:
            raise ValueError(f"Unknown strategy: {strategy}")
        cfg = strat["build"](params)

        sec_ids = _resolve_security_ids(symbols)
        resolvable = [s for s in symbols if s in sec_ids]
        all_trades: list[dict] = []
        signals_found = 0
        skip_reasons: dict[str, int] = {}

        for i, symbol in enumerate(resolvable):
            try:
                candles = candle_fetcher.get_candles(
                    sec_ids[symbol], symbol, _exchange_segment(symbol),
                    cfg["interval_min"], start_date, end_date)
                if candles.empty:
                    continue
                signals = cfg["find_signals"](candles, symbol)
                signals_found += len(signals)
                for sig in signals:
                    day = sig["entry_ts"].date()
                    day_candles = candles[candles["ts"].dt.date == day]
                    trade, skip_reason = _simulate_trade(sig, cfg, day_candles)
                    if trade:
                        all_trades.append(trade)
                    else:
                        skip_reasons[skip_reason] = skip_reasons.get(skip_reason, 0) + 1
            except Exception:
                logger.exception("backtest: symbol %s failed, skipping", symbol)
            results_store.update_run(run_id, progress_pct=round((i + 1) / len(resolvable) * 100, 1))

        results_store.insert_trades(run_id, all_trades)
        summary = _summary(all_trades, signals_found, skip_reasons)
        results_store.update_run(
            run_id, status="done", progress_pct=100,
            finished_at=datetime.now().isoformat(), summary=summary)
    except Exception as exc:
        logger.exception("backtest run %s failed", run_id)
        results_store.update_run(
            run_id, status="error", error=f"{exc}\n{traceback.format_exc()[-2000:]}",
            finished_at=datetime.now().isoformat())
