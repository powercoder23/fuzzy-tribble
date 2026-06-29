# -*- coding: utf-8 -*-
"""
auto_exit_config.py — thresholds for the OI-contradiction auto-exit.

Closes an OPEN paper position when the latest OI-buildup read for that name
strongly contradicts the position's side (a fresh SHORT_BUILDUP against a CE,
or a fresh LONG_BUILDUP against a PE). This turns the existing risk *warning*
in OrderManager._check_position_risks into a risk *action*.

Reads only oi_buildup_history (already in iv_history.db) — zero broker calls.

Mode idiom matches PMG_GATE_MODE / GATE_MODE:
  off  → never evaluate, never act (default — safe rollout)
  soft → evaluate and log the would-be exit, but DON'T close
  hard → close the contradicting position at market (last LTP)

All values are overridable via environment variables so you can tune per
deployment without touching code.
"""

import os

# ── Mode ─────────────────────────────────────────────────────────────────── #
MODE = os.getenv("AUTO_EXIT_OI_MODE", "off").strip().lower()

# ── Trigger thresholds ───────────────────────────────────────────────────── #
# Minimum aggregate-OI change (%) on the contradicting buildup before we act.
# The OI here is aggregate call+put OI vs day-open (see oi_buildup_scanner), so
# this is a conviction filter: small OI drift shouldn't dump a position.
MIN_OI_CHG_PCT = float(os.getenv("AUTO_EXIT_OI_MIN_OI_CHG_PCT", "50"))

# Only act on a *strong* buildup (LONG_BUILDUP / SHORT_BUILDUP — fresh
# positioning), not on weak SHORT_COVERING / LONG_UNWINDING fades.
REQUIRE_STRONG = os.getenv("AUTO_EXIT_OI_REQUIRE_STRONG", "true").strip().lower() == "true"

# Don't dump a clear winner on a noisy OI read: skip auto-exit when the
# position is already up more than this (%). Set very high to disable.
MAX_PROFIT_PCT = float(os.getenv("AUTO_EXIT_OI_MAX_PROFIT_PCT", "10"))
