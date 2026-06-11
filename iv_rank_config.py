# -*- coding: utf-8 -*-
"""
Configuration for the IV Rank / IV Percentile Scanner (service: iv-rank).

All thresholds are option-BUYER oriented: low IV rank/percentile = cheap options
= the zone where long premium is least exposed to IV crush.
"""

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Zone thresholds (applied to the PRIMARY_METRIC below)
# --------------------------------------------------------------------------- #
# CHEAP      -> buy zone, options are cheap vs their own history
# FAIR       -> selective, need strong directional conviction
# EXPENSIVE  -> avoid buying, IV crush risk
BUY_ZONE_MAX   = float(os.getenv("IVR_BUY_ZONE_MAX",  "30"))   # <= 30  -> CHEAP
SELECTIVE_MAX  = float(os.getenv("IVR_SELECTIVE_MAX", "55"))   # 30-55  -> FAIR
                                                              # >  55  -> EXPENSIVE

# Which metric drives the zone classification + ranking.
# With a short history window IV *percentile* is more robust than IV *rank*
# (rank is hostage to a single min/max outlier). Switch to "rank" once you have
# a full year of daily snapshots.
PRIMARY_METRIC = os.getenv("IVR_PRIMARY_METRIC", "percentile")  # "percentile" | "rank"

# --------------------------------------------------------------------------- #
# History / data requirements
# --------------------------------------------------------------------------- #
# Minimum distinct daily IV points before a symbol is rankable. Below this the
# percentile is statistically meaningless, so the symbol is skipped (fail-open).
MIN_HISTORY_DAYS = int(os.getenv("IVR_MIN_HISTORY_DAYS", "15"))

# Lookback window (in daily samples) used for the baseline. Adaptive: uses
# whatever exists up to this cap. 252 ~= one trading year (true 52w IV Rank).
LOOKBACK_DAYS = int(os.getenv("IVR_LOOKBACK_DAYS", "252"))

# A daily window this large or larger is treated as a "true" 52-week baseline
# and the alert is labelled "IV Rank" rather than "IV %ile (adaptive)".
FULL_BASELINE_DAYS = int(os.getenv("IVR_FULL_BASELINE_DAYS", "240"))

# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
TOP_N_ALERT   = int(os.getenv("IVR_TOP_N_ALERT", "15"))   # cheapest N in Telegram
OUTPUT_CSV    = str(Path("data") / "iv_rank_opportunities.csv")
PERSIST_TABLE = "iv_rank_history"

# Scan cadence (IST). Daily snapshots refresh through the session, so a few
# checkpoints are plenty. EOD run is the authoritative one.
SCAN_TIMES = os.getenv("IVR_SCAN_TIMES", "09:45,12:30,15:20").split(",")
