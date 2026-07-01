# -*- coding: utf-8 -*-
"""
Configuration for the OI Buildup Scanner (service: oi-buildup).

Universe-wide promotion of the four-quadrant OI model that already lives in
oi_validator.classify(). Reads ONLY iv_history.db (spot + aggregate option OI),
so it makes zero broker calls and is fully isolated from existing services.
"""

import os
from pathlib import Path

# Compare the latest intraday snapshot against the FIRST snapshot of the same
# day ("intraday_open" — mirrors oi_validator's default comparison mode).
COMPARISON_MODE = os.getenv("OIB_COMPARISON_MODE", "intraday_open")  # intraday_open | prev_day

# Dead-band: ignore noise. A name is only classified as a real buildup/unwind
# when BOTH moves clear these thresholds (percent). Below them -> "FLAT".
MIN_PRICE_CHANGE_PCT = float(os.getenv("OIB_MIN_PRICE_CHANGE_PCT", "0.3"))
MIN_OI_CHANGE_PCT    = float(os.getenv("OIB_MIN_OI_CHANGE_PCT", "1.0"))

# Minimum aggregate OI (call+put) for a name to be trustworthy.
MIN_ABS_OI = float(os.getenv("OIB_MIN_ABS_OI", "0"))

# How many names to push to Telegram per side.
TOP_N_ALERT = int(os.getenv("OIB_TOP_N_ALERT", "12"))

OUTPUT_CSV    = str(Path("data") / "oi_buildup_opportunities.csv")
PERSIST_TABLE = "oi_buildup_history"

SCAN_TIMES = os.getenv("OIB_SCAN_TIMES", "09:45,11:30,13:30,15:15").split(",")

# Scanning + persistence to oi_buildup_history always run regardless of this
# flag. This only controls the external Telegram push — set to true to make
# the scanner alert-visible again; default is internal-use-only (false).
ALERTS_ENABLED = os.getenv("OIB_ALERTS_ENABLED", "false").strip().lower() == "true"
