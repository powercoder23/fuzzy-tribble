"""All numeric constants for the Break and Bounce strategy. No business logic."""

import os
from pathlib import Path

CAPITAL = 200_000  # INR. Override via env BB_CAPITAL.

BB_RISK = {
    "max_risk_pct":         0.02,   # 2% of capital per trade = ₹4,000
    "sl_pct":               0.30,   # 30% SL on option premium
    "target_ratio":         2.5,    # Target = 2.5x SL distance (between 2x and 3x)
    "daily_loss_limit_pct": 0.03,   # Hard stop if down 3% on day
    "max_trades_per_day":   3,
    "max_open_positions":   2,
}

BB_BREAKOUT = {
    # Breakout window: 9:15 AM – 11:45 AM (first 2.5 hours of market)
    "window_end_hour":    11,
    "window_end_min":     45,
    # Retest tolerance: how close price must come to the breakout level (0.3%)
    "retest_tol_pct":     0.003,
    # Hammer pattern: lower/upper wick must be >= 2x the candle body
    "hammer_wick_ratio":  2.0,
    # Counter wick must be <= 50% of body (relaxed — avoids filtering too aggressively)
    "max_counter_wick":   0.5,
    # Force exit all positions by this time
    "force_exit_hour":    15,
    "force_exit_min":     15,
}

BB_LIQUIDITY = {
    "min_oi":         500,
    "min_volume":     200,
    "max_spread_pct": 0.05,
}

BB_STRIKE = {
    "otm_offset": 0,  # ATM strike (tighter to the level for break & bounce)
}

SCRIP_MASTER_DB = str(Path("data") / "api-scrip-master.db")
IV_HISTORY_DB   = "iv_history.db"
TRADE_LOG_PATH  = str(Path("data") / "break_bounce_trades.csv")

LOT_SIZE_FALLBACK = {
    "PPLPHARMA":  1800,
    "TORNTPOWER": 750,
    "TATATECH":   475,
    "HUDCO":      2000,
    "NIFTY":      75,
    "BANKNIFTY":  30,
    "FINNIFTY":   65,
    "MIDCPNIFTY": 75,
}

_env_capital = os.getenv("BB_CAPITAL")
if _env_capital:
    try:
        CAPITAL = float(_env_capital)
    except ValueError:
        pass
