"""All numeric constants for the momentum strategy. No business logic."""

import os
from pathlib import Path

CAPITAL = 200_000   # Total trading capital INR. Override via env MOMENTUM_CAPITAL.

RISK_CONFIG = {
    "max_risk_pct":         0.02,   # 2% of capital per trade = ₹4,000
    "sl_pct":               0.30,   # SL at 30% of premium paid
    "target1_mult":         1.8,    # T1: exit 50% of position here
    "target2_mult":         3.0,    # T2: exit remaining here
    "daily_loss_limit_pct": 0.03,   # Hard stop if down 3% on day
    "max_trades_per_day":   3,
    "max_open_positions":   2,
}

REGIME = {
    "ema_fast":   20,
    "ema_slow":   50,
    "adx_min":    25,   # Minimum ADX to confirm trend
    "adx_strong": 30,   # ADX above this = STRONG
    "vix_max":    22,   # Skip all trades if India VIX above this
}

ORB = {
    "range_candles":     2,     # First 2 × 15-min candles = 9:15–9:30 opening range
    "volume_mult":       1.5,   # Breakout candle must have 1.5× prior 5-candle avg vol
    "entry_cutoff_hour": 11,
    "entry_cutoff_min":  30,    # No new entries after 11:30 AM
    "force_exit_hour":   15,
    "force_exit_min":    15,    # Exit all positions by 15:15
}

LIQUIDITY = {
    "min_oi":         500,    # Minimum open interest at strike
    "min_volume":     200,    # Minimum volume at strike
    "max_spread_pct": 0.05,   # Max (ask-bid)/mid — 5%
}

STRIKE = {
    "intraday_otm_offset": 1,   # 1 strike OTM from ATM for intraday
    "swing_otm_offset":    0,   # ATM for BTST/swing
}

SCRIP_MASTER_DB = str(Path("data") / "api-scrip-master.db")
IV_HISTORY_DB   = "iv_history.db"
TRADE_LOG_PATH  = str(Path("data") / "momentum_trades.csv")

LOT_SIZE_FALLBACK = {
    # 4 symbols that don't match scrip master regex
    "PPLPHARMA":  1800,
    "TORNTPOWER": 750,
    "TATATECH":   475,
    "HUDCO":      2000,
    # Index fallbacks (should always be in scrip master but kept as safety)
    "NIFTY":      75,
    "BANKNIFTY":  30,
    "FINNIFTY":   65,
    "MIDCPNIFTY": 75,
}

# Read capital override from env if set
_env_capital = os.getenv("MOMENTUM_CAPITAL")
if _env_capital:
    try:
        CAPITAL = float(_env_capital)
    except ValueError:
        pass
