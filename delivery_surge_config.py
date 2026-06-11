# -*- coding: utf-8 -*-
"""
Configuration for the Delivery-% Surge Scanner (service: delivery-surge).

Delivery % = share of traded volume that was actually delivered (not squared off
intraday). A price move on surging delivery % means real money is positioning,
so the move tends to TREND over days — exactly what an option buyer needs to beat
theta. Reads ONLY delivery_daily (bhav collector) in iv_history.db. Zero API.
"""

import os
from pathlib import Path

# Latest deliv% must be at least this multiple of its trailing average.
SURGE_MULT = float(os.getenv("DSURGE_MULT", "1.5"))

# Absolute floor: even a "spike" below this delivery % is not conviction.
MIN_DELIV_PCT = float(os.getenv("DSURGE_MIN_DELIV_PCT", "45"))

# The accompanying price move must be at least this big (percent) to have a
# directional read (surge + up = accumulation -> CE, surge + down = distribution -> PE).
MIN_PRICE_CHANGE_PCT = float(os.getenv("DSURGE_MIN_PRICE_CHANGE_PCT", "1.0"))

# Trailing window (in prior days) used for the average deliv%. Adaptive: uses
# whatever exists up to this cap.
LOOKBACK_DAYS = int(os.getenv("DSURGE_LOOKBACK_DAYS", "20"))

# Minimum prior days needed to form an average. With only ~2 days of bhav today
# the scanner still runs (compares vs the single prior day) but is low-confidence;
# ~10+ days gives a reliable baseline. Labelled in the alert.
MIN_HISTORY_DAYS = int(os.getenv("DSURGE_MIN_HISTORY_DAYS", "2"))
RELIABLE_HISTORY_DAYS = int(os.getenv("DSURGE_RELIABLE_HISTORY_DAYS", "10"))

# Restrict to the F&O universe (symbols present in iv_history) — only those are
# option-buyable. Non-F&O delivery surges are noise for a buyer.
FNO_ONLY = os.getenv("DSURGE_FNO_ONLY", "true").lower() == "true"

TOP_N_ALERT   = int(os.getenv("DSURGE_TOP_N_ALERT", "15"))
OUTPUT_CSV    = str(Path("data") / "delivery_surge_opportunities.csv")
PERSIST_TABLE = "delivery_surge_history"

# Runs in the evening, AFTER the bhav collector lands delivery data (19:00+).
SCAN_TIMES = os.getenv("DSURGE_SCAN_TIMES", "19:45,21:00,22:30").split(",")
