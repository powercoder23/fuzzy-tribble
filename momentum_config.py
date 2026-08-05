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

# ---------------------------------------------------------------------------
# Shared paper book (2026-08-05). Momentum historically journalled to
# data/momentum_trades.csv and alerted, but never called
# OrderManager.submit_external_signal — so it was invisible on the dashboard
# and absent from every shared analytic. It now books into paper_trades.db too;
# the CSV journal is unchanged and still written, so nothing that reads it
# breaks.
#
# `mode`: off -> journal + alert only, exactly the pre-2026-08-05 behaviour.
#         paper -> also book into the shared book (never a real order; live
#         orders remain gated solely by AUTO_EXECUTE, which is untouched).
# ---------------------------------------------------------------------------
PAPER = {
    "mode":                 os.getenv("MOMENTUM_PAPER_MODE", "paper").strip().lower(),
    "strategy_tag":         os.getenv("MOMENTUM_STRATEGY_TAG", "Momentum"),
    "monitor_interval_min": int(os.getenv("MOMENTUM_MONITOR_INTERVAL_MIN", "5")),
    "monitor_from":         os.getenv("MOMENTUM_MONITOR_FROM", "09:35"),
    "monitor_until":        os.getenv("MOMENTUM_MONITOR_UNTIL", "15:10"),
    "square_off":           os.getenv("MOMENTUM_SQUARE_OFF", "15:15"),
    "eod_summary_at":       os.getenv("MOMENTUM_EOD_SUMMARY_AT", "15:20"),
    # Premium floor for the shared book. Momentum runs its own liquidity and
    # affordability gates, so this only filters sub-rupee junk — the discount
    # path's ₹5 floor would wrongly drop cheap large-lot names.
    "min_premium":          float(os.getenv("MOMENTUM_MIN_PREMIUM", "1.0")),
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

# ---------------------------------------------------------------------------
# Alpha engine (signal-generation layer only — risk/sizing/journal untouched).
# Every factor below is derived from data already on disk: delivery_daily and
# candles_5m in iv_history.db, sector_mapping.db, and the OI buildup scanner.
# ---------------------------------------------------------------------------

RS = {
    "lookback_days":  20,    # N-day return used for the cross-sectional rank
    "min_percentile": 70,    # CE needs rs_pct >= this, PE needs <= (100 - this)
    "min_universe":   40,    # fewer ranked names than this -> RS gate disabled
    "shortlist":      25,    # names carried from premarket into intraday scans
}

ATR = {
    "period":         14,
    "min_atr_pct":    1.2,   # reject dead names — option buyers need range
    "expansion_fast": 5,     # mean TR% over last 5 days ...
    "expansion_slow": 20,    # ... divided by mean TR% over last 20
    "min_expansion":  0.95,  # below this the name is contracting — reject
}

RVOL = {
    "sessions":     10,   # baseline sessions (raise to 20 as candles_5m fills)
    "min_sessions":  5,   # below this RVOL is unavailable -> neutral, no veto
    "min_rvol":      1.2, # participation floor at signal time
    "saturate_at":   2.5, # rvol scoring 1.0 at this multiple
}

BREAKOUT = {
    "coil_bars":         4,    # 15-min bars examined for the pre-breakout coil
    "nr_contraction":    0.85, # range(coil) / range(prior coil) must be <= this
    "min_vol_expansion": 1.5,
    "min_close_loc":     0.6,  # close in top 60% of the breakout bar's range
}

VWAP_Q = {
    "slope_bars":      3,
    "min_slope_pct":   0.03,  # VWAP slope over slope_bars, as % of price
    "acceptance_bars": 3,     # of the last N completed closes ...
    "min_acceptance":  2,     # ... this many must sit on the signal side
}

BREADTH_GATE = {
    "enabled":          True,
    "min_for_ce":       45.0,
    "max_for_pe":       55.0,
    "sector_enabled":   True,
    "sector_min_names": 3,
    "sector_min_ce":    40.0,
    "sector_max_pe":    60.0,
}

# Weighted scoring engine. Weights need not sum to 100 — the scorer normalises.
# Set a weight to 0 to keep journalling a factor while denying it any influence
# (the observe-then-enable protocol that produced engine formula v2.1).
WEIGHTS = {
    "relative_strength": 20.0,
    "breakout_quality":  20.0,
    "rvol":              15.0,
    "atr_expansion":     10.0,
    "sector_breadth":    10.0,
    "market_breadth":    10.0,
    "regime":            10.0,
    "oi_buildup":         5.0,
}

CONFIDENCE = {
    "min_score":    60.0,   # only signals scoring above this are traded
    "observe_only": False,  # True = score and journal, never block a trade
}

SCRIP_MASTER_DB = str(Path("data") / "api-scrip-master.db")
# Shared IV store lives in the /app/data Docker volume (see collectors/iv_store.py).
IV_HISTORY_DB   = str(Path("data") / "iv_history.db")
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


def _env_override(env_name, container, key, cast=float):
    """Apply an env override onto one config key. Silently ignores bad values."""
    raw = os.getenv(env_name)
    if raw is None:
        return
    try:
        container[key] = cast(raw)
    except (TypeError, ValueError):
        pass


# Operational knobs worth turning without a rebuild.
_env_override("MOMENTUM_MIN_CONFIDENCE", CONFIDENCE, "min_score")
_env_override("MOMENTUM_OBSERVE_ONLY", CONFIDENCE, "observe_only",
              lambda v: str(v).strip().lower() in ("1", "true", "yes"))
_env_override("MOMENTUM_RS_MIN_PCT", RS, "min_percentile")
_env_override("MOMENTUM_RS_SHORTLIST", RS, "shortlist", int)
_env_override("MOMENTUM_MIN_ATR_PCT", ATR, "min_atr_pct")
_env_override("MOMENTUM_RVOL_SESSIONS", RVOL, "sessions", int)
_env_override("MOMENTUM_MIN_RVOL", RVOL, "min_rvol")
_env_override("MOMENTUM_BREADTH_ENABLED", BREADTH_GATE, "enabled",
              lambda v: str(v).strip().lower() in ("1", "true", "yes"))
