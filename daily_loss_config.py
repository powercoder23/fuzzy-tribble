# -*- coding: utf-8 -*-
"""
daily_loss_config.py — book-level daily-loss lockout for the paper book.

Once the day's realized (+ optionally marked-open) P&L across ALL strategies in
the shared paper book falls to -LIMIT_RUPEES or worse, the OrderManager stops
booking NEW entries for the rest of the session. Existing positions keep being
managed and exited normally. This is the one book-level risk control that the
per-trade rupee cap and the trade-count caps do NOT provide
(review 2026-07-09 §3.1).

Mode idiom matches the other gates (settings-DB override -> env -> default):
  off  -> never evaluate, never block (default — safe rollout)
  soft -> evaluate and log the would-be lockout, but keep booking
  hard -> block all new entries once the floor is breached

All values are overridable via environment variables and the Settings UI
(DAILY_LOSS_GATE_MODE / DAILY_LOSS_LIMIT_RUPEES flags in settings_store).
"""

import os

# ── Mode ─────────────────────────────────────────────────────────────────── #
MODE = os.getenv("DAILY_LOSS_GATE_MODE", "off").strip().lower()

# ── Loss floor (rupees, positive magnitude) ──────────────────────────────── #
# New entries stop when the day's book P&L <= -LIMIT_RUPEES. 0 disables the
# guard regardless of MODE.
LIMIT_RUPEES = float(os.getenv("DAILY_LOSS_LIMIT_RUPEES", "5000"))

# ── Marked-open inclusion ────────────────────────────────────────────────── #
# When True, the day total includes the marked (unrealized) P&L of still-open
# positions, so a large open drawdown also locks out new entries — not just
# realized losses. When False, only closed-trade realized P&L counts.
INCLUDE_OPEN = os.getenv("DAILY_LOSS_INCLUDE_OPEN", "true").strip().lower() == "true"
