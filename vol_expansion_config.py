# -*- coding: utf-8 -*-
"""
vol_expansion_config.py — config for the Volatility-Expansion paper strategy.

Trades the dashboard's "II · Volatility Expansion — 4-day IV slope" signal:
long premium on names whose daily ATM IV is CLIMBING while still CHEAP on
52-wk history (the buy zone). Direction-agnostic vega signal booked as a
DIRECTIONAL single leg (CE/PE picked from the underlying's recent trend).

All values env-overridable. Idiom matches the other gates.
"""
import os

# ── Master mode ──────────────────────────────────────────────────────────── #
#   off   -> never scan / never book
#   alert -> evaluate and Telegram-alert candidates, but DON'T book
#   paper -> book paper trades into the shared book (paper only; no real orders)
MODE = os.getenv("VOL_EXP_MODE", "paper").strip().lower()

# ── Candidate selection ──────────────────────────────────────────────────── #
LOOKBACK_DAYS   = int(os.getenv("VOL_EXP_LOOKBACK_DAYS", "4"))
MIN_SLOPE       = float(os.getenv("VOL_EXP_MIN_SLOPE", "0.5"))   # IV pts/day
# Most-suitable candidates = EXPANDING *and* still cheap (buy zone). When False,
# trade the top expanding names by slope regardless of IVP (includes rich chases).
BUY_ZONE_ONLY   = os.getenv("VOL_EXP_BUY_ZONE_ONLY", "true").strip().lower() == "true"
MAX_SCAN        = int(os.getenv("VOL_EXP_MAX_SCAN", "40"))       # names to consider
MAX_TRADES_PER_DAY = int(os.getenv("VOL_EXP_MAX_TRADES", "3"))

# ── Direction ────────────────────────────────────────────────────────────── #
# CE/PE from the underlying's recent daily spot trend (iv_history). If the move
# is inside +/-MIN_MOVE_PCT it reads as no-trend. REQUIRE_TREND skips those
# (don't force a directional bet on a pure-vega signal with no lean).
MIN_MOVE_PCT    = float(os.getenv("VOL_EXP_MIN_MOVE_PCT", "1.0"))
TREND_LOOKBACK  = int(os.getenv("VOL_EXP_TREND_LOOKBACK", "6"))  # daily samples
REQUIRE_TREND   = os.getenv("VOL_EXP_REQUIRE_TREND", "true").strip().lower() == "true"

# ── Strike / expiry ──────────────────────────────────────────────────────── #
STRIKE_OTM_OFFSET = int(os.getenv("VOL_EXP_OTM_OFFSET", "0"))    # 0 = ATM
MIN_DTE           = int(os.getenv("VOL_EXP_MIN_DTE", "4"))       # trading days

# ── Risk / trade plan (single leg, long premium) ─────────────────────────── #
SL_PCT          = float(os.getenv("VOL_EXP_SL_PCT", "0.30"))     # 30% premium stop
T1_MULT         = float(os.getenv("VOL_EXP_T1_MULT", "1.5"))     # +50% book partial
T2_MULT         = float(os.getenv("VOL_EXP_T2_MULT", "2.0"))     # +100% runner
T1_BOOK_FRACTION = float(os.getenv("VOL_EXP_T1_BOOK_FRACTION", "0.5"))
MIN_PREMIUM     = float(os.getenv("VOL_EXP_MIN_PREMIUM", "5.0"))

# ── Liquidity floor ──────────────────────────────────────────────────────── #
LIQ_MIN_OI      = int(os.getenv("VOL_EXP_MIN_OI", "50000"))
LIQ_MIN_VOLUME  = int(os.getenv("VOL_EXP_MIN_VOLUME", "1000"))
LIQ_MAX_SPREAD  = float(os.getenv("VOL_EXP_MAX_SPREAD", "0.20")) # 20% of mid

# ── Schedule (IST) ───────────────────────────────────────────────────────── #
# Daily-IV signal changes slowly; a few scans catch freshly-qualifying names
# once the morning IV snapshots have accrued.
SCAN_TIMES      = os.getenv("VOL_EXP_SCAN_TIMES", "09:45,11:00,13:00").split(",")
ENTRY_CUTOFF    = os.getenv("VOL_EXP_ENTRY_CUTOFF", "13:30")
MONITOR_INTERVAL_MIN = int(os.getenv("VOL_EXP_MONITOR_INTERVAL_MIN", "5"))
MONITOR_UNTIL   = os.getenv("VOL_EXP_MONITOR_UNTIL", "15:20")
SQUARE_OFF      = os.getenv("VOL_EXP_SQUARE_OFF", "15:20")
EOD_SUMMARY_AT  = os.getenv("VOL_EXP_EOD_AT", "15:25")

STRATEGY_TAG = "Vol Expansion (IV slope)"
