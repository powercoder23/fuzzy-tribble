# -*- coding: utf-8 -*-
"""sonar_exit_config.py — thresholds for the Sonar-reversal auto-exit.

OrderManager._check_position_risks already detects a Sonar reversal against
an open position (BREAKDOWN/REVERSAL_DOWN on a CE, BREAKOUT_UP/REVERSAL_UP
on a PE) but only ever sends a Telegram warning. This turns that existing
risk *warning* into a risk *action*, mirroring auto_exit_config.py's
OI-contradiction gate exactly (same off/soft/hard idiom, same
skip-a-clear-winner escape hatch).

Reads only the latest sonar_laplace_scanner read for the symbol (same call
_check_position_risks already makes) — zero extra broker calls.

Mode idiom:
  off  -> never evaluate, never act (default)
  soft -> evaluate and log the would-be exit, but DON'T close
  hard -> close the contradicting position at market (last LTP)

Default is OFF, unlike AUTO_EXIT_OI_MODE's "hard" default — OI-contradiction
was already an operator-reviewed, validated act-on signal (review 2026-07-09);
Sonar reversal has only ever been a warning here, so it starts conservative
until it's been watched in soft/hard mode for a while.
"""

import os

# ── Mode ─────────────────────────────────────────────────────────────────── #
MODE = os.getenv("SONAR_EXIT_MODE", "off").strip().lower()

# Skip a clear winner already up more than this on premium (%) — let it run
# rather than cutting it on a reversal signal alone. Same idea as
# auto_exit_config.MAX_PROFIT_PCT.
MAX_PROFIT_PCT = float(os.getenv("SONAR_EXIT_MAX_PROFIT_PCT", "20"))
