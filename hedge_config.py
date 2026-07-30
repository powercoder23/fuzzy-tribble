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

# --------------------------------------------------------------------------- #
# Combined spread-level exit (2026-07-30)
# --------------------------------------------------------------------------- #
# A debit spread (long + short, same combo_id) should enter AND exit together
# — each leg's own individual SL/T1 (set above/by the originating strategy)
# describes that leg in isolation and is wrong for a hedged position: e.g. the
# short leg's own target fires on pure theta decay regardless of what the long
# leg is doing, closing half the spread while the other half rides alone.
#
# Once both legs of a combo are open, paper_trader.monitor() evaluates the
# COMBINED spread value (long premium - short premium) against these
# combined levels instead of either leg's individual SL/T1, and closes BOTH
# legs together the instant either level is hit. Definitions:
#   entry_debit           = long_entry - short_entry            (net cost paid = max loss)
#   strike_width           = |long_strike - short_strike|         (points)
#   max_profit_potential   = max(strike_width - entry_debit, 0)   (theoretical, at full ITM)
#   spread_value(t)        = long_price(t) - short_price(t)
#   SL     when spread_value <= entry_debit * (1 - SPREAD_SL_PCT)
#   TARGET when spread_value >= entry_debit + max_profit_potential * SPREAD_TARGET_CAPTURE_PCT
SPREAD_SL_PCT = float(os.getenv("HEDGE_SPREAD_SL_PCT", "0.40"))
# Capturing 100% of max profit needs the underlying past the short strike at
# expiry-level certainty — unrealistic intraday, so target a partial capture.
SPREAD_TARGET_CAPTURE_PCT = float(os.getenv("HEDGE_SPREAD_TARGET_CAPTURE_PCT", "0.55"))

# --------------------------------------------------------------------------- #
# Margin estimation (2026-07-30) — ROUGH APPROXIMATIONS, not exchange SPAN.
# --------------------------------------------------------------------------- #
# Real SPAN margin needs the exchange's daily risk-parameter files (per-
# contract VaR scanning ranges), which this system has no access to. This is
# an industry-standard retail rule of thumb for stock F&O volatility — close
# enough to size the "hedge benefit" for comparison, NOT a substitute for the
# broker's live margin calculator before any real money is ever involved. See
# hedge.py estimate_naked_short_margin / estimate_spread_margin.
NAKED_SPAN_PCT_OF_NOTIONAL = float(os.getenv("NAKED_SPAN_PCT_OF_NOTIONAL", "0.15"))
