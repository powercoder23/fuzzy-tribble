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

# ---- index slope (NIFTY SuperSmoother, fed into regime lean) ----------------
# E2-1: regime lean was breadth-only because index_slope_pct was never computed.
# NIFTY 5-min closes live in candles_5m under this security_id (symbol column is
# mislabeled there, so we key on the id). Params mirror the sonar scanner so the
# index trend is read the same way stock trends are.
INDEX_SECURITY_ID = os.getenv("ENGINE_INDEX_SID", "13")   # NIFTY
INDEX_SLOPE_PERIOD = _i("ENGINE_INDEX_SLOPE_PERIOD", 10)   # SuperSmoother period
INDEX_SLOPE_LOOKBACK = _i("ENGINE_INDEX_SLOPE_LOOKBACK", 3)  # slope lookback (bars)
INDEX_SLOPE_MIN_BARS = _i("ENGINE_INDEX_SLOPE_MIN_BARS", 6)  # need this many to trust it

# ---- conviction weights (formula_ver bumps when these change shape) --------
# v2.1 (P0.4, 2026-07-24): replay over 38k labeled decisions (train Jul 3-16 /
# valid Jul 17-23) showed three anti-predictive score inputs that stacked into
# the inverted ladder (A+ = worst grade, CE side):
#   inst_flow      — EOD bulk/block (BTST horizon) used for 60-min bets; edge
#                    -0.52 when present vs -0.10 absent. Weight -> 0.
#   gap            — continuation vote but intraday gaps fade. Weight -> 0.
#                    (gap-as-FADE showed top-grade alpha in replay; research
#                    candidate for v2.2, not shipped — one change at a time.)
#   premium_value  — direction-neutral cheap-IV bonus inflating a directional
#                    score. Score weight -> 0; the EXPENSIVE hard gate stays.
# Replay result for this combo: ladder monotone on train, top-grade positive on
# validation. Factors still journal their votes (weight 0) so evidence keeps
# accruing for re-inclusion. P0.5 = two-week live re-observation gate.
FORMULA_VER = "v2.1"
W_TRIGGER = _f("ENGINE_W_TRIGGER", 30.0)
W_OI_FLOW = _f("ENGINE_W_OI_FLOW", 20.0)
W_TREND = _f("ENGINE_W_TREND", 15.0)
W_SECTOR_RS = _f("ENGINE_W_SECTOR_RS", 10.0)
W_INST_FLOW = _f("ENGINE_W_INST_FLOW", 0.0)      # v2.1: was 10.0
W_PREMIUM_VALUE = _f("ENGINE_W_PREMIUM_VALUE", 0.0)  # v2.1: was 10.0 (gate remains)
W_GAP = _f("ENGINE_W_GAP", 0.0)                  # v2.1: was 5.0

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

# ---- paper booking (E1-1: measure grade->edge on realized option P&L) -------
# The engine paper-trades its own EMITTED decisions so the journal can show
# hit-rate/expectancy per grade, not just forward spot moves. Zero broker calls:
# entry premium is ESTIMATED from ATM IV (expected_move), the ATM strike comes
# from the iv_history snapshot, and expiry/lot/opt-id from the local scrip
# master. Rows land in the shared paper_trades.db tagged PAPER_STRATEGY_TAG so
# the existing monitor marks/exits them with real quotes.
PAPER_MODE = os.getenv("ENGINE_PAPER_MODE", "off").lower()   # off | paper
PAPER_MAX_TRADES = _i("ENGINE_PAPER_MAX_TRADES", 5)          # per day, across cycles
PAPER_GRADES = [g for g in os.getenv("ENGINE_PAPER_GRADES", "A+,A")
                .replace(" ", "").split(",") if g]
PAPER_SL_PCT = _f("ENGINE_PAPER_SL_PCT", 0.30)              # SL = entry x (1 - SL_PCT)
PAPER_TARGET_R = _f("ENGINE_PAPER_TARGET_R", 2.0)           # target = entry x (1 + SL_PCT x R)
PAPER_MIN_DTE = _i("ENGINE_PAPER_MIN_DTE", 1)               # skip contracts nearer than this
PAPER_STRATEGY_TAG = os.getenv("ENGINE_PAPER_STRATEGY_TAG", "Convex")
SCRIP_MASTER_DB = os.getenv("ENGINE_SCRIP_MASTER_DB",
                            os.path.join("data", "api-scrip-master.db"))
