# -*- coding: utf-8 -*-
"""trailing_config.py — trailing stop-loss for open (non-combo) paper legs.

apply_tick() uses one fixed SL + one fixed target (see paper_trader.py
docstring) — the first level touched closes the whole position. That's
correct for the common case but gives back a big favorable move that
reverses before the fixed target is reached. This adds an opt-in ratchet:
once a trade is up ACTIVATION_PCT on premium, its SL is walked up (long) /
down (short) toward the best price seen so far, only ever tightening,
never loosening — a classic trailing stop layered on top of the existing
fixed-SL/fixed-target state machine, not a replacement for it.

Scope: only trades that go through apply_tick() (naked longs from
strategies that don't hedge, and iv-seller's short strangle/straddle
legs). 2-leg debit-spread combos are evaluated via apply_combo_tick's
combined spread-level SL/target instead and are untouched by this.

Mode idiom matches hedge_config.py: a plain ENABLED switch (no soft/hard —
there's nothing to "log only" here, a trailing SL either moves or it
doesn't), off by default for safe rollout.
"""

import os

# Master on/off switch.
ENABLED = os.getenv("TRAILING_SL_ENABLED", "false").strip().lower() not in (
    "false", "0", "no", "off",
)

# Profit-on-premium (fraction) required before the trailing ratchet starts.
# Below this the fixed SL from the originating strategy's own risk model
# still applies unchanged.
ACTIVATION_PCT = float(os.getenv("TRAILING_SL_ACTIVATION_PCT", "0.20"))

# Fraction of the gain from entry to the peak that's allowed to give back
# before the trailing stop fires. 0.15 means the ratcheted SL sits at
# peak - 15% of (peak - entry) for a long (mirrored for a short leg).
GIVEBACK_PCT = float(os.getenv("TRAILING_SL_GIVEBACK_PCT", "0.15"))
