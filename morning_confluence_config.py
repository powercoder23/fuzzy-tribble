# -*- coding: utf-8 -*-
"""
Configuration for the Morning Confluence paper-trade flow.

Fuses the THREE intraday screeners (gap + oi_buildup + iv_rank) into A+ morning
option-buy candidates, attaches a reason + caveats, and books a paper trade.
Runs once around 09:50 when all three screeners are fresh together.
"""

import os

# A candidate is A+ when gap and oi_buildup AGREE on direction. IV is a cost gate.
REQUIRE_GAP_OI_AGREE = os.getenv("MC_REQUIRE_AGREE", "true").lower() == "true"

# IV zones (from iv_rank): block EXPENSIVE in strict mode; otherwise just caveat it.
BLOCK_EXPENSIVE_IV = os.getenv("MC_BLOCK_EXPENSIVE", "false").lower() == "true"

# Strike selection fallback when the symbol isn't in the discounted-premium list.
# "atm" or "otm1" (one strike out-of-the-money in the trade direction).
STRIKE_FALLBACK = os.getenv("MC_STRIKE_FALLBACK", "otm1")   # atm | otm1

# Discounted-premium list written by the discount scan each cycle.
DISCOUNT_CSV = os.getenv("MC_DISCOUNT_CSV", "data/discounted_premiums.csv")

# 5-min support lookback (candles) for entry/SL structure.
SUPPORT_LOOKBACK = int(os.getenv("MC_SUPPORT_LOOKBACK", "6"))   # last 6 x 5-min = 30 min

# Liquidity gates (checked on the option if a chain quote is available).
MIN_OI      = int(os.getenv("MC_MIN_OI", "500"))
MIN_VOLUME  = int(os.getenv("MC_MIN_VOLUME", "200"))
MAX_SPREAD_PCT = float(os.getenv("MC_MAX_SPREAD_PCT", "0.05"))

TOP_N        = int(os.getenv("MC_TOP_N", "5"))
OUTPUT_CSV   = "data/morning_confluence.csv"
SCAN_TIMES   = os.getenv("MC_SCAN_TIMES", "09:50").split(",")
