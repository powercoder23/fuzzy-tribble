# -*- coding: utf-8 -*-
"""
Configuration for the Extreme Opening (Gap + Range) Scanner (service: gap-scan).

Finds names that OPEN with both (a) a significant gap vs yesterday's close AND
(b) the open printing beyond yesterday's high/low — the strongest open-drive
setups for buying CE (gap-up breakout) or PE (gap-down breakdown).

Reads ONLY iv_history.db. Prefers true daily OHLC from `delivery_daily` (written
by the bhav collector) when present; otherwise derives a prior-day OHLC proxy
from the intraday IV snapshots. Zero broker calls either way.
"""

import os
from pathlib import Path

# Minimum absolute gap (open vs prev close, percent) to qualify as "extreme".
GAP_PCT = float(os.getenv("GAP_MIN_PCT", "1.5"))

# Require the open to also break beyond yesterday's high (gap-up) / low (gap-down).
# Default True = the "gap + range combined" definition. Set False for gap-only.
REQUIRE_RANGE_BREAK = os.getenv("GAP_REQUIRE_RANGE_BREAK", "true").lower() == "true"

# Treat the first intraday snapshot at/after this time as the session "open".
OPEN_CUTOFF = os.getenv("GAP_OPEN_CUTOFF", "09:30")  # HH:MM

TOP_N_ALERT = int(os.getenv("GAP_TOP_N_ALERT", "15"))

OUTPUT_CSV    = str(Path("data") / "gap_opportunities.csv")
PERSIST_TABLE = "gap_history"

# Runs early — gap setups are an opening play. A couple of checkpoints is enough.
SCAN_TIMES = os.getenv("GAP_SCAN_TIMES", "09:25,09:45").split(",")
