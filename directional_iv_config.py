import os
from pathlib import Path

CAPITAL = float(os.getenv("DIRECTIONAL_IV_CAPITAL", "200000"))

RISK_CONFIG = {
    "max_risk_pct":         0.02,   # 2% of capital per trade
    "sl_pct":               0.30,   # Stop loss at 30% premium decay
    "target_mult":          1.8,    # Target at 1.8x entry
    "daily_loss_limit_pct": 0.03,   # Hard stop if down 3% on day
    "max_trades_per_day":   2,
    "max_open_positions":   2,
}

TREND_FILTER = {
    "ema_fast":  9,
    "ema_mid":  20,
    "ema_slow": 50,
    "ema_long": 200,
    "min_trend_gap_pct": 0.4,  # Minimum gap between price and EMAs for trend conviction
}

LIQUIDITY = {
    "min_oi":        2500,
    "min_volume":    500,
    "min_atm_oi":    500,
    "max_spread_pct": 0.20,
}

IV_FILTER = {
    "max_atm_iv":          45.0,
    "max_iv_rank":         65,
    "max_expected_move_ratio": 1.2,
    "max_moneyness_pct":   2.5,
    "min_delta":           0.18,
    "max_delta":           0.40,
    # --- IV-rank buy-zone gate (fed by the standalone iv-rank scanner) ------ #
    # When enabled, directional_iv only trades names whose IV rank sits in the
    # cheap buy zone. Default OFF so behaviour is unchanged unless you opt in.
    "buy_zone_only":       os.getenv("DIRECTIONAL_IV_BUY_ZONE_ONLY", "false").lower() == "true",
    "buy_zone_max_ivr":    float(os.getenv("DIRECTIONAL_IV_BUY_ZONE_MAX_IVR", "35")),
}

DTE_FILTER = {
    "min_dte": 7,
    "max_dte": 35,
}

DEFAULT_UNIVERSE_SIZE = int(os.getenv("DIRECTIONAL_IV_UNIVERSE_SIZE", "30"))
OUTPUT_CSV = str(Path("data") / "directional_iv_opportunities.csv")
TRADE_LOG_PATH = str(Path("data") / "directional_iv_trades.csv")
TELEGRAM_ALERT_THRESHOLD = int(os.getenv("DIRECTIONAL_IV_TELEGRAM_ALERT_THRESHOLD", "75"))
MIN_SCORE = 65
