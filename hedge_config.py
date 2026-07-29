"""hedge_config.py — Global config for the spread-hedge layer.

Every long option buy (CE or PE) is automatically paired with a short OTM
leg of the same direction and expiry (bull-call spread / bear-put spread).
This caps max loss to the net debit and eliminates unlimited-premium-risk on
the long side. Toggle ENABLED=false to revert to naked-long behaviour.
"""

import os

# Master on/off switch. Set HEDGE_ENABLED=false env var or Settings-page
# toggle to revert to single-leg buying for testing.
ENABLED = os.getenv("HEDGE_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")

# How many strike intervals above/below the primary strike to place the
# short hedge. 3 strikes gives a reasonable credit while keeping the spread
# wide enough to capture most of the move.
HEDGE_STRIKES_OTM = int(os.getenv("HEDGE_STRIKES_OTM", "3"))

# Maximum hedge premium as a fraction of the primary entry premium.
# If the OTM option costs more than this fraction of the primary, skip the
# hedge (it would be too close to the primary — the spread is too narrow).
HEDGE_MAX_CREDIT_RATIO = float(os.getenv("HEDGE_MAX_CREDIT_RATIO", "0.60"))

# Minimum credit received for the short leg (₹). Below this the hedge is
# too thin to justify the extra leg.
HEDGE_MIN_CREDIT = float(os.getenv("HEDGE_MIN_CREDIT", "0.50"))

# SL for the short hedge leg: buy back if premium rises to this multiple
# of the entry credit (e.g. 2.5× means the leg moves 150% against us).
HEDGE_SL_MULT = float(os.getenv("HEDGE_SL_MULT", "2.5"))

# Target for the short hedge leg: buy back when premium has decayed to
# this fraction of the entry credit (e.g. 0.15 = 85% time-decay captured).
HEDGE_T1_MULT = float(os.getenv("HEDGE_T1_MULT", "0.15"))
