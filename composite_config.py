# -*- coding: utf-8 -*-
"""
Configuration for the Composite Conviction Scanner (service: composite).

The composite FUSES the five existing zero-API scanners into one ranked,
direction-aware conviction score per F&O stock. It reads ONLY the persisted
*_history tables those scanners already write to iv_history.db — so it makes
ZERO broker calls and adds no API load.

Two cadences (see SCAN_TIMES):
  * EOD (primary)  — after smart-money (16:30/18:00) and delivery-surge
                     (19:45+) have populated their tables. This is the only
                     time ALL factors are fresh → next-session conviction list.
  * Intraday (opt) — IV/OI/gap update live; delivery/deals carry yesterday's
                     value as a static bias overlay. Enable via INTRADAY_TIMES.
"""

import os
from pathlib import Path


# --------------------------------------------------------------------------- #
# Factor weights — how much each scanner contributes to the directional score.
# Only the four DIRECTIONAL factors vote CE/PE. IV rank and VIX are MODIFIERS
# (they scale conviction, they don't pick a side). Weights need not sum to 1;
# the score is normalised by the max achievable weight.
# --------------------------------------------------------------------------- #
W_OI_BUILDUP     = float(os.getenv("CMP_W_OI",       "0.30"))  # fresh longs/shorts = core direction
W_SMART_MONEY    = float(os.getenv("CMP_W_SMART",    "0.25"))  # institutional catalyst/conviction
W_DELIVERY_SURGE = float(os.getenv("CMP_W_DELIV",    "0.20"))  # conviction behind the move
W_GAP            = float(os.getenv("CMP_W_GAP",      "0.15"))  # momentum trigger
# Sum of directional weights is the denominator for the 0-100 base score.

# --------------------------------------------------------------------------- #
# IV-rank modifier (not a vote — a cost gate for option BUYERS).
# CHEAP IV boosts conviction (cheap premium, low crush risk); EXPENSIVE penalises.
# --------------------------------------------------------------------------- #
IV_CHEAP_BOOST   = float(os.getenv("CMP_IV_BOOST",   "0.20"))  # ×1.20 when CHEAP
IV_EXPENSIVE_PEN = float(os.getenv("CMP_IV_PENALTY", "0.25"))  # ×0.75 when EXPENSIVE

# --------------------------------------------------------------------------- #
# VIX regime modifier (market-wide). High VIX = richer premium + whippy =
# scale buyer conviction down a touch; low VIX = friendlier for buyers.
# --------------------------------------------------------------------------- #
VIX_HIGH         = float(os.getenv("CMP_VIX_HIGH",   "20"))    # >= this = elevated
VIX_LOW          = float(os.getenv("CMP_VIX_LOW",    "13"))    # <= this = calm
VIX_HIGH_PENALTY = float(os.getenv("CMP_VIX_PEN",    "0.15"))  # ×0.85 when elevated
VIX_LOW_BOOST    = float(os.getenv("CMP_VIX_BOOST",  "0.10"))  # ×1.10 when calm

# --------------------------------------------------------------------------- #
# Confluence — the real edge is several factors agreeing. Reward agreement.
# --------------------------------------------------------------------------- #
MIN_FACTORS      = int(os.getenv("CMP_MIN_FACTORS",  "2"))     # need >= N directional votes to rank
AGREE_BONUS_3    = float(os.getenv("CMP_AGREE3",     "0.10"))  # +10% if 3 factors align
AGREE_BONUS_4    = float(os.getenv("CMP_AGREE4",     "0.20"))  # +20% if all 4 align

# --------------------------------------------------------------------------- #
# Score classification (applied to the final 0-100 conviction).
# --------------------------------------------------------------------------- #
STRONG_MIN       = float(os.getenv("CMP_STRONG_MIN", "70"))
MODERATE_MIN     = float(os.getenv("CMP_MODERATE_MIN", "45"))
# below MODERATE_MIN -> WEAK (not alerted by default)

# --------------------------------------------------------------------------- #
# Output / cadence
# --------------------------------------------------------------------------- #
TOP_N_ALERT   = int(os.getenv("CMP_TOP_N_ALERT", "12"))
OUTPUT_CSV    = str(Path("data") / "composite_conviction.csv")
PERSIST_TABLE = "composite_history"

# EOD run is authoritative — after deals + delivery are in. Add intraday slots
# to CMP_INTRADAY_TIMES if you also want a live (stale-overlay) read.
SCAN_TIMES     = os.getenv("CMP_SCAN_TIMES", "20:15,22:45").split(",")
INTRADAY_TIMES = [t for t in os.getenv("CMP_INTRADAY_TIMES", "").split(",") if t.strip()]
