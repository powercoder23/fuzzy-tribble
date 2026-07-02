# -*- coding: utf-8 -*-
"""Engine configuration — all knobs in one place, env-overridable."""

import os


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


# ---- regime ----------------------------------------------------------------
VIX_RED = _f("ENGINE_VIX_RED", 22.0)          # no-trade above
VIX_ELEVATED = _f("ENGINE_VIX_ELEVATED", 18.0)  # amber above
VIX_CALM = _f("ENGINE_VIX_CALM", 13.0)
BREADTH_BULL = _f("ENGINE_BREADTH_BULL", 55.0)  # % advancers for CE lean
BREADTH_BEAR = _f("ENGINE_BREADTH_BEAR", 45.0)
INDEX_SLOPE_MIN = _f("ENGINE_INDEX_SLOPE_MIN", 0.05)  # % per lookback, trend floor
SIZE_MULT = {"GREEN": 1.0, "AMBER": 0.5, "RED": 0.0}

# ---- conviction weights (formula_ver bumps when these change shape) --------
FORMULA_VER = "v2.0-p0"
W_TRIGGER = _f("ENGINE_W_TRIGGER", 30.0)
W_OI_FLOW = _f("ENGINE_W_OI_FLOW", 20.0)
W_TREND = _f("ENGINE_W_TREND", 15.0)
W_SECTOR_RS = _f("ENGINE_W_SECTOR_RS", 10.0)
W_INST_FLOW = _f("ENGINE_W_INST_FLOW", 10.0)
W_PREMIUM_VALUE = _f("ENGINE_W_PREMIUM_VALUE", 10.0)
W_GAP = _f("ENGINE_W_GAP", 5.0)

CONFLUENCE_BONUS = _f("ENGINE_CONFLUENCE_BONUS", 0.10)   # >=3 factors agree
CONFLUENCE_MIN_AGREE = _i("ENGINE_CONFLUENCE_MIN_AGREE", 3)
VIX_ELEVATED_PENALTY = _f("ENGINE_VIX_ELEVATED_PENALTY", 0.15)

# ---- grades ----------------------------------------------------------------
GRADE_A_PLUS = _f("ENGINE_GRADE_A_PLUS", 75.0)
GRADE_A = _f("ENGINE_GRADE_A", 60.0)
GRADE_B = _f("ENGINE_GRADE_B", 45.0)
GRADE_SIZE_MULT = {"A+": 1.0, "A": 1.0, "B": 0.5}

# ---- expected move (buyer viability) ----------------------------------------
# 1-day 1-sigma move (from ATM IV) below this % = dead-vol name, reject.
# 0.8% is conservative; raise toward 1.2 when the journal shows theta losses.
MIN_EXPECTED_MOVE_PCT = _f("ENGINE_MIN_EXPECTED_MOVE_PCT", 0.8)

# ---- watchlist -------------------------------------------------------------
WATCH_MIN_CONTEXT = _f("ENGINE_WATCH_MIN_CONTEXT", 55.0)  # context score to WATCH without trigger

# ---- risk state gates (engine-side; executor re-checks) ---------------------
MAX_CONCURRENT = _i("ENGINE_MAX_CONCURRENT", 3)
DAILY_LOSS_LIMIT_PCT = _f("ENGINE_DAILY_LOSS_LIMIT_PCT", 3.0)
MAX_SL_HITS_PER_DAY = _i("ENGINE_MAX_SL_HITS_PER_DAY", 2)
ENTRY_CUTOFF = os.getenv("ENGINE_ENTRY_CUTOFF", "14:30")  # HH:MM IST

# ---- persistence -----------------------------------------------------------
DECISIONS_TABLE = "engine_decisions"
REGIME_TABLE = "engine_regime"

# ---- alerting --------------------------------------------------------------
ALERT = os.getenv("ENGINE_ALERT", "true").lower() in ("1", "true", "yes")
TOP_N_ALERT = _i("ENGINE_TOP_N_ALERT", 5)
