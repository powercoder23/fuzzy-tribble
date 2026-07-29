# -*- coding: utf-8 -*-
"""
Configuration for the IV Seller strategy (service: iv-seller).

Mirrors the config-module convention already used by directional-IV
(directional_iv_config.py) and IV Rank (iv_rank_config.py) — but this one is
option-SELLER oriented: it looks for RICH IV (the opposite zone from
iv_rank_config's buy-zone thresholds) and sells premium (short strangle or
short straddle) expecting IV / price mean-reversion.

Structure choice is dynamic, driven by how extreme the IV setup is (2026-07-29
decision): moderate-rich IV -> OTM strangle (smaller premium, smaller risk,
higher probability of profit); very-rich IV -> ATM straddle (bigger premium,
bigger risk, lower POP) — see STRADDLE_MIN_PCT below.
"""

import os
from pathlib import Path

CAPITAL = float(os.getenv("IV_SELLER_CAPITAL", "200000"))

# --------------------------------------------------------------------------- #
# IV sell-zone thresholds (iv_percentile from iv_rank_scanner.iv_percentile)
# --------------------------------------------------------------------------- #
# Below SELL_ZONE_MIN, premium isn't rich enough to justify selling risk.
SELL_ZONE_MIN     = float(os.getenv("IV_SELLER_SELL_ZONE_MIN", "65"))
# At/above STRADDLE_MIN_PCT the setup is extreme enough to sell the ATM
# straddle instead of an OTM strangle (bigger credit, bigger risk).
STRADDLE_MIN_PCT  = float(os.getenv("IV_SELLER_STRADDLE_MIN_PCT", "85"))

# Same fail-open history requirement as iv_rank_config.MIN_HISTORY_DAYS.
MIN_HISTORY_DAYS = int(os.getenv("IV_SELLER_MIN_HISTORY_DAYS", "15"))
LOOKBACK_DAYS    = int(os.getenv("IV_SELLER_LOOKBACK_DAYS", "252"))

# --------------------------------------------------------------------------- #
# Strike selection
# --------------------------------------------------------------------------- #
# Delta target for the OTM strangle leg (each side, abs value).
STRANGLE_DELTA_TARGET = float(os.getenv("IV_SELLER_STRANGLE_DELTA", "0.175"))
# Straddle legs are simply the ATM strike (delta ~0.5); no separate knob needed.

DTE_MIN = int(os.getenv("IV_SELLER_DTE_MIN", "7"))
DTE_MAX = int(os.getenv("IV_SELLER_DTE_MAX", "15"))

LIQUIDITY = {
    "min_oi":         2500,
    "min_volume":     500,
    "min_atm_oi":     500,
    "max_spread_pct": 0.20,
}

# --------------------------------------------------------------------------- #
# Credit-based exit rule (multiples of the entry credit per leg — mirrors
# discount_config.TRADE_PLAN's "multiples of entry" style, mirrored in
# direction: SL is a LOSS multiple, target is a PROFIT-capture fraction)
# --------------------------------------------------------------------------- #
SL_CREDIT_MULT     = float(os.getenv("IV_SELLER_SL_CREDIT_MULT", "2.0"))   # stop at -100% of credit
TARGET_CREDIT_MULT = float(os.getenv("IV_SELLER_TARGET_CREDIT_MULT", "0.35"))  # book at +65% of credit

# --------------------------------------------------------------------------- #
# Risk / position caps
# --------------------------------------------------------------------------- #
# Per-leg 1-lot rupee risk ceiling, same enforcement point as discount's
# max_risk_rupees (paper_trader.book_signal / _risk_rupees, direction-aware).
MAX_RISK_RUPEES_PER_LEG = float(os.getenv("IV_SELLER_MAX_RISK_RUPEES", "3000"))
# 0 = unlimited (testing mode).
MAX_COMBOS_PER_DAY             = int(os.getenv("IV_SELLER_MAX_COMBOS_PER_DAY", "0"))
MAX_COMBOS_PER_SYMBOL_PER_DAY  = int(os.getenv("IV_SELLER_MAX_COMBOS_PER_SYMBOL_PER_DAY", "0"))

DEFAULT_UNIVERSE_SIZE = int(os.getenv("IV_SELLER_UNIVERSE_SIZE", "30"))
OUTPUT_CSV = str(Path("data") / "iv_seller_opportunities.csv")
TELEGRAM_ALERT_THRESHOLD = float(os.getenv("IV_SELLER_TELEGRAM_ALERT_THRESHOLD", "70"))

STRATEGY_STRANGLE = "IV Short Strangle"
STRATEGY_STRADDLE = "IV Short Straddle"
