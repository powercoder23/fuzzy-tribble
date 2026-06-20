# -*- coding: utf-8 -*-
"""
Configuration for the Intraday Trade Suggester (soft / rank-only).

The suggester takes the discount scanner's ORDERABLE setups (strike, entry, SL,
targets) as candidates and RE-RANKS them by how strongly the alert-only scanners
(gap, oi_buildup, iv_rank, smart_money, delivery_surge) agree with the trade's
direction. It is SOFT: nothing is ever filtered out — agreement only re-orders
and scales the score. Zero broker calls (reads the persisted *_history tables).
"""

import os

# Directional confluence weights — how much each scanner's agreement counts.
# Only these four vote on direction. iv_rank and VIX are cost/regime modifiers.
W_OI_BUILDUP     = float(os.getenv("TS_W_OI",     "0.30"))
W_SMART_MONEY    = float(os.getenv("TS_W_SMART",  "0.25"))
W_DELIVERY_SURGE = float(os.getenv("TS_W_DELIV",  "0.20"))
W_GAP            = float(os.getenv("TS_W_GAP",    "0.15"))

# A scanner pointing the OPPOSITE way to the trade subtracts this fraction of its
# weight (soft penalty — never a hard veto).
DISAGREE_FACTOR  = float(os.getenv("TS_DISAGREE", "1.0"))

# How much the confluence reshapes the discount score:
#   suggestion_score = discount_score * (1 + agree_sum * CONFLUENCE_GAIN)
# agree_sum is in [-1, +1]; gain 0.5 => full agreement +50%, full disagreement -50%.
CONFLUENCE_GAIN  = float(os.getenv("TS_GAIN",     "0.5"))

# IV-rank cost modifier (buyer-friendly): cheap IV boosts, expensive penalises.
IV_CHEAP_BOOST   = float(os.getenv("TS_IV_BOOST",   "0.15"))
IV_EXPENSIVE_PEN = float(os.getenv("TS_IV_PENALTY", "0.20"))

# VIX regime modifier (read from vix_daily).
VIX_HIGH         = float(os.getenv("TS_VIX_HIGH",  "20"))
VIX_LOW          = float(os.getenv("TS_VIX_LOW",   "13"))
VIX_HIGH_PENALTY = float(os.getenv("TS_VIX_PEN",   "0.10"))
VIX_LOW_BOOST    = float(os.getenv("TS_VIX_BOOST", "0.05"))

# Output
TOP_N_ALERT   = int(os.getenv("TS_TOP_N", "8"))
OUTPUT_CSV    = "data/trade_suggestions.csv"
# Alerts are entry-only by design (the paper-trade entry alert is the signal).
# The suggester still writes its ranked CSV every cycle; set 