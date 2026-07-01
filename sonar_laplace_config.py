# -*- coding: utf-8 -*-
"""
Configuration for the Sonar-Laplace Scanner (service: sonar).

"Sonar Laplace" style indicators apply mathematical smoothing / signal-processing
filters to price to derive a smoothed midline + dynamic bands that act as moving
support/resistance, and from those: trend, reversals, and breakouts. This scanner
implements an Ehlers SuperSmoother (a 2-pole Butterworth / Laplace-family low-pass
filter) + adaptive residual bands.

Reads 5-min CLOSE candles from the shared DataProvider (cache-first, with a
direct-fetch fallback) — no longer the iv_history intraday spot snapshots.

NOTE (matches the trader caveat): these levels are *dynamic* (they move with price)
and are best used as a TREND/CONFIRMATION filter, not as standalone S/R. Anchor real
entries to PDH/PDL, VWAP, opening range, and swing levels.
"""

import os
from pathlib import Path

# Smoother period — larger = smoother/slower midline. ~10-20 suits a 15-min series.
SMOOTH_PERIOD = int(os.getenv("SONAR_PERIOD", "10"))  # was 12

# Band width = SMOOTH_PERIOD residual std × this multiplier (dynamic S/R distance).
BAND_MULT = float(os.getenv("SONAR_BAND_MULT", "1.6"))

# Slope lookback (points) used to read trend direction off the smoothed line.
SLOPE_LOOKBACK = int(os.getenv("SONAR_SLOPE_LOOKBACK", "3"))

# Minimum slope (% of price) to call a trend vs FLAT — kills noise.
MIN_SLOPE_PCT = float(os.getenv("SONAR_MIN_SLOPE_PCT", "0.05"))

# Minimum intraday price points before a symbol is analysable.
# 10 × 5-min candles = 50 min minimum history.
MIN_POINTS = int(os.getenv("SONAR_MIN_POINTS", "10"))  # was 12

TOP_N_ALERT   = int(os.getenv("SONAR_TOP_N_ALERT", "12"))
OUTPUT_CSV    = str(Path("data") / "sonar_laplace_opportunities.csv")
PERSIST_TABLE = "sonar_history"

# Intraday scanner — align with the other intraday screeners.
SCAN_TIMES = os.getenv("SONAR_SCAN_TIMES",
                       "09:50,10:15,10:45,11:30,13:30,15:00").split(",")

# Scanning + persistence to sonar_history always run regardless of this flag,
# and paper_trader's Sonar side-override gate (get_latest_sonar) is unaffected
# either way — it reads sonar_history directly, not Telegram. This only
# controls the external Telegram push; default is internal-use-only (false).
ALERTS_ENABLED = os.getenv("SONAR_ALERTS_ENABLED", "false").strip().lower() == "true"
