# -*- coding: utf-8 -*-
"""
Configuration for the Smart-Money (Bulk/Block) Scanner (service: smart-money).

Turns NSE bulk & block deals into a next-day (BTST) directional bias for option
buyers: net institutional BUY value -> CE bias, net SELL -> PE bias. Reads ONLY
the `deals` table (deals collector) in iv_history.db. Zero API.
"""

import os
from pathlib import Path

# Minimum |net value| (buy minus sell, in crores) for a name to signal.
MIN_NET_VALUE_CR = float(os.getenv("SM_MIN_NET_VALUE_CR", "5.0"))

# Aggregate deals over the last N distinct deal-dates (institutions often
# accumulate across sessions). 1 = latest day only.
LOOKBACK_DAYS = int(os.getenv("SM_LOOKBACK_DAYS", "3"))

# Include BLOCK deals alongside BULK. Block deals are often crosses (matched
# buy+sell) which net to ~0 and self-filter, so including them is safe.
INCLUDE_BLOCK = os.getenv("SM_INCLUDE_BLOCK", "true").lower() == "true"

# Restrict to the F&O universe (symbols in iv_history) — only those are
# option-buyable. A bulk deal on a non-F&O stock is useless to a buyer.
FNO_ONLY = os.getenv("SM_FNO_ONLY", "true").lower() == "true"

TOP_N_ALERT   = int(os.getenv("SM_TOP_N_ALERT", "15"))
OUTPUT_CSV    = str(Path("data") / "smart_money_opportunities.csv")
PERSIST_TABLE = "smart_money_history"

# Bulk + short deals publish ~15:45, block ~10:35. Scan in the evening for a
# clean next-day (BTST) bias.
SCAN_TIMES = os.getenv("SM_SCAN_TIMES", "16:30,18:00").split(",")
