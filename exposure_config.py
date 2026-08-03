# -*- coding: utf-8 -*-
"""exposure_config.py — portfolio-wide open-exposure cap (review-style gap).

The concentration gate (order_manager.py PORTFOLIO_MAX_SAME_DIRECTION /
PORTFOLIO_MAX_PER_SECTOR) caps positions per direction and per sector, and
the per-trade max_risk_rupees cap sizes one trade at a time — but nothing
caps the BOOK as a whole: how many positions are open concurrently, or how
much total premium is deployed across every strategy combined. A day where
every strategy independently stays under its own caps can still stack into
an outsized book-wide bet.

Counts OPEN positions (+ already-accepted candidates within the same batch,
same running-total style as the concentration gate) against two independent
caps; either one, if set, can block a candidate.

Mode idiom matches PORTFOLIO_GATE_MODE / BREADTH_GATE_MODE:
  off  → never evaluate, never block (default — safe rollout)
  soft → evaluate and log the would-be block, but keep booking
  hard → drop candidates that would breach either cap
"""

import os

# ── Mode ─────────────────────────────────────────────────────────────────── #
MODE = os.getenv("EXPOSURE_GATE_MODE", "off").strip().lower()

# ── Caps ─────────────────────────────────────────────────────────────────── #
# Max concurrent open positions (legs) across the whole book. 0 = unlimited.
MAX_OPEN_POSITIONS = int(os.getenv("EXPOSURE_MAX_OPEN_POSITIONS", "0"))

# Max total premium deployed (Rs) across every open leg — sum(entry *
# lot_size). Short legs still count (they carry real margin/assignment
# risk even though they're a credit, not a debit) — this is a raw capital-
# at-risk proxy, not a net-debit calculation. 0 = unlimited.
MAX_OPEN_PREMIUM_RUPEES = float(os.getenv("EXPOSURE_MAX_OPEN_PREMIUM_RUPEES", "0"))
