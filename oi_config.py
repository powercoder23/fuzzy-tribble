# -*- coding: utf-8 -*-
"""
Configuration for the OI Validation Layer (Break & Bounce add-on).

This module is intentionally self-contained: pure constants + environment
toggles, no imports from any strategy module and no business logic. It exists so
the OI validator stays completely isolated from the core Break & Bounce code.

CRITICAL CONTRACT
-----------------
* When ``OI_VALIDATION_ENABLED`` is False (the default), the validator is a
  no-op and the strategy behaves EXACTLY as it does today.
* When OI data is unavailable for any reason, the validator returns an
  ``ALLOW`` decision (see ``FALLBACK_DECISION``). The layer must never block a
  trade because of missing / bad OI data.

All values can be overridden via environment variables so behaviour can be
tuned without touching production strategy code.
"""

import os
from pathlib import Path


# ---------------------------------------------------------------------------
# Small typed env helpers
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Master switches
# ---------------------------------------------------------------------------

# OFF by default → existing behaviour is byte-for-byte unchanged until opted in.
OI_VALIDATION_ENABLED = _env_bool("OI_VALIDATION_ENABLED", False)

# Strict vs normal allow-lists (see ALLOWED_ROLES below).
OI_STRICT_MODE = _env_bool("OI_STRICT_MODE", False)

# When True, a rejected classification VOIDS the setup (the 5-min retest never
# runs). When False, the validator only annotates + scores; the setup proceeds
# and the 5-min retest runs exactly as today (annotate / alert-only mode).
OI_BLOCK_ON_REJECT = _env_bool("OI_BLOCK_ON_REJECT", True)


# ---------------------------------------------------------------------------
# Classifications
# ---------------------------------------------------------------------------

LONG_BUILDUP   = "LONG_BUILDUP"      # price up   + OI up
SHORT_BUILDUP  = "SHORT_BUILDUP"     # price down + OI up
SHORT_COVERING = "SHORT_COVERING"    # price up   + OI down
LONG_UNWINDING = "LONG_UNWINDING"    # price down + OI down
NO_DATA        = "NO_DATA"           # OI / price unavailable → fallback path

# Role of each classification per breakout direction.
#   role in {"preferred", "acceptable", "weak", "reject"}
ROLE_BY_DIRECTION = {
    "BULLISH": {
        LONG_BUILDUP:   "preferred",
        SHORT_COVERING: "acceptable",
        LONG_UNWINDING: "weak",
        SHORT_BUILDUP:  "reject",
    },
    "BEARISH": {
        SHORT_BUILDUP:  "preferred",
        LONG_UNWINDING: "acceptable",
        SHORT_COVERING: "weak",
        LONG_BUILDUP:   "reject",
    },
}

# Which roles are allowed to PROCEED to the 5-min retest in each mode.
#   strict (True)  → only the preferred classification
#   normal (False) → preferred + acceptable  (matches the spec's NORMAL MODE)
#
# NOTE on "weak": the written spec lists only {preferred, acceptable} as the
# NORMAL-MODE allow-list, so "weak" is treated as blocked here. If you would
# rather let weak setups proceed (annotated as low-confidence), simply add
# "weak" to the normal set below — that is the single intended tuning point.
ALLOWED_ROLES = {
    True:  {"preferred"},                 # strict
    False: {"preferred", "acceptable"},   # normal
}

# Contextual setup score by role.
#   buildups (preferred when aligned) = 100
#   covering / unwinding              = 60
#   rejected setup                    = 0
# This reproduces the spec's SCORING table exactly.
SCORE_BY_ROLE = {
    "preferred":  100,
    "acceptable": 60,
    "weak":       60,
    "reject":     0,
}

# Human-readable confidence label per role (Telegram / logs).
CONFIDENCE_BY_ROLE = {
    "preferred":  "Strong",
    "acceptable": "Moderate",
    "weak":       "Weak",
    "reject":     "Rejected",
}

# Pretty labels for classifications (Telegram / logs).
CLASSIFICATION_LABEL = {
    LONG_BUILDUP:   "Long Build-up",
    SHORT_BUILDUP:  "Short Build-up",
    SHORT_COVERING: "Short Covering",
    LONG_UNWINDING: "Long Unwinding",
    NO_DATA:        "OI Unavailable",
}


# ---------------------------------------------------------------------------
# Data-fetch tuning
# ---------------------------------------------------------------------------

# Upstox instrument master used to resolve the nearest-expiry futures key.
# (Same file the upstox_adapter reads; FUT rows: exchange='NSE',
#  instrument_type='FUT', underlying_symbol=<SYMBOL>, instrument_key='NSE_FO|..')
COMPLETE_DB = str(Path("data") / "complete.db")

# Intraday candle interval (minutes) used to read futures price + OI.
OI_CANDLE_INTERVAL = _env_int("OI_CANDLE_INTERVAL", 15)

# How the price/OI change is measured:
#   "intraday_open" → latest candle vs the first candle of the day   (1 API call)
#   "prev_candle"   → latest candle vs the candle immediately before (1 API call)
#   "prev_day"      → latest intraday value vs yesterday's daily      (+1 API call)
OI_COMPARISON_MODE = os.getenv("OI_COMPARISON_MODE", "intraday_open").strip().lower()

# Dead-band: if BOTH |price change| and |OI change| are below these (percent),
# the reading is treated as inconclusive → fallback ALLOW (never block on noise).
# Defaults of 0 disable the dead-band entirely.
OI_MIN_OI_CHANGE_PCT    = _env_float("OI_MIN_OI_CHANGE_PCT", 0.0)
OI_MIN_PRICE_CHANGE_PCT = _env_float("OI_MIN_PRICE_CHANGE_PCT", 0.0)

# Sanity floor: futures OI below this is treated as untrustworthy → NO_DATA.
OI_MIN_ABS_OI = _env_int("OI_MIN_ABS_OI", 0)

# In-memory cache TTL (seconds) for a symbol's futures snapshot, so repeated
# look-ups within a scan reuse the same fetch.
OI_CACHE_TTL_SEC = _env_int("OI_CACHE_TTL_SEC", 300)


# ---------------------------------------------------------------------------
# Fallback (must stay ALLOW — see module docstring)
# ---------------------------------------------------------------------------

FALLBACK_DECISION = "ALLOW"
FALLBACK_SCORE    = _env_int("OI_FALLBACK_SCORE", 50)
