# -*- coding: utf-8 -*-
"""
breadth_config.py — thresholds for the market & sector breadth filter.

The breadth filter blocks counter-trend option entries: it stops fresh CE buys
when the tape is broadly red, and fresh PE buys when it's broadly green — at
both the whole-market and the candidate's own sector level. Reads only
iv_history.db (intraday spot snapshots from iv-collector) + sector_mapping.db.
Zero broker calls.

Breadth % = advancers / (advancers + decliners) * 100, where a name is an
advancer/decliner only if its spot moved more than MIN_MOVE_PCT from day-open
(small drifters are ignored as noise). 50 = balanced, <50 = net down, >50 = net up.

Mode idiom matches PMG_GATE_MODE / GATE_MODE / AUTO_EXIT_OI_MODE:
  off  → never evaluate, never block (default — safe rollout)
  soft → evaluate and log the would-be block, but pass through
  hard → drop counter-trend candidates
"""

import os
from pathlib import Path

# ── Mode ─────────────────────────────────────────────────────────────────── #
MODE = os.getenv("BREADTH_GATE_MODE", "off").strip().lower()

# ── What counts as a move ────────────────────────────────────────────────── #
# A name is only an advancer/decliner if |spot move from open| clears this.
MIN_MOVE_PCT = float(os.getenv("BREADTH_MIN_MOVE_PCT", "0.3"))

# Need at least this many moving names before the market breadth read is trusted.
MIN_TOTAL_NAMES = int(os.getenv("BREADTH_MIN_TOTAL_NAMES", "20"))

# ── Market-wide gate ─────────────────────────────────────────────────────── #
# Block CE when market breadth is below this (tape broadly red).
MIN_BREADTH_FOR_CE = float(os.getenv("BREADTH_MIN_FOR_CE", "35"))
# Block PE when market breadth is above this (tape broadly green).
MAX_BREADTH_FOR_PE = float(os.getenv("BREADTH_MAX_FOR_PE", "65"))

# ── Sector gate ──────────────────────────────────────────────────────────── #
SECTOR_ENABLED = os.getenv("BREADTH_SECTOR_ENABLED", "true").strip().lower() == "true"
# A sector breadth read needs at least this many names to be meaningful.
SECTOR_MIN_NAMES = int(os.getenv("BREADTH_SECTOR_MIN_NAMES", "4"))
# Block CE when the candidate's OWN sector breadth is below this.
MIN_SECTOR_BREADTH_FOR_CE = float(os.getenv("BREADTH_SECTOR_MIN_FOR_CE", "40"))
# Block PE when the candidate's OWN sector breadth is above this.
MAX_SECTOR_BREADTH_FOR_PE = float(os.getenv("BREADTH_SECTOR_MAX_FOR_PE", "60"))

# ── Data sources ─────────────────────────────────────────────────────────── #
SECTOR_DB_PATH = os.getenv("BREADTH_SECTOR_DB_PATH", str(Path("data") / "sector_mapping.db"))
