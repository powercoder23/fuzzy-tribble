"""
Configuration for the Discounted Premium scanner (discount.py).

All tunable thresholds for the discount strategy live here so the scanning
logic in discount.py stays declarative. This mirrors the config-module
convention already used by momentum (momentum_config.py), break-and-bounce
(break_bounce_config.py), and directional-IV (directional_iv_config.py).
"""

# --- IV history / volatility mode ---------------------------------------
# Minimum number of *daily* ATM IV samples before IV Rank / IV Percentile are
# trusted. Below this the scanner falls back to skew-only ("skew") mode.
MIN_IV_SAMPLES = 30

# --- Option-chain API throttling (Dhan: 1 request / 3 seconds) ----------
CHAIN_API_MIN_INTERVAL_SEC = 3.1
# Base backoff (seconds) before the FIRST retry. Kept as the historical 4s.
CHAIN_API_RETRY_BACKOFF_SEC = 4.0
# FIX 1: configurable retry budget + exponential backoff for the option-chain
# endpoint family. The previous logic retried at most once on a rate-limit.
# Backoff before retry N (1-indexed) is:
#   CHAIN_API_RETRY_BACKOFF_SEC * CHAIN_API_BACKOFF_MULTIPLIER ** (N - 1)
# i.e. 4s, 8s, 16s with these defaults. Retries fire on rate-limit responses,
# empty option chains, empty expiry lists, and temporary network errors.
CHAIN_API_MAX_RETRIES = 3
CHAIN_API_BACKOFF_MULTIPLIER = 2

# --- Liquidity / tradeability gates (applied per strike, per side) ------
# FIX 4: stricter default tiers. The old values (min_oi=1000, min_volume=1)
# admitted near-untradeable contracts; the defaults below demand real depth.
LIQUIDITY = {
    "min_oi": 5000,        # reject strikes with open interest below this
    "min_volume": 100,     # require at least this much traded volume
    # Reject strikes whose quoted bid-ask spread exceeds this fraction of the
    # mid price. A live two-sided quote (bid > 0 AND ask > 0) is required;
    # strikes without one are skipped because they cannot be entered cleanly.
    "max_spread_pct": 0.12,
}

# FIX 4: legacy loose thresholds, used ONLY when ALLOW_LOOSE_LIQUIDITY is True
# (config value below or the ALLOW_LOOSE_LIQUIDITY env var). This preserves the
# exact pre-change behaviour as an opt-in escape hatch.
LOOSE_LIQUIDITY = {
    "min_oi": 1000,
    "min_volume": 1,
    "max_spread_pct": 0.12,
}

# When True, the scanner reverts to LOOSE_LIQUIDITY. Default False = strict.
# discount.py also honours an ALLOW_LOOSE_LIQUIDITY environment variable so the
# threshold can be flipped operationally without editing this file.
ALLOW_LOOSE_LIQUIDITY = False

# --- Strike selection ---------------------------------------------------
STRIKE = {
    "min_abs_delta": 0.10,           # skip near-zero-delta lottery strikes (unless hedging_mode)
    "max_expected_move_ratio": 1.5,  # skip strikes more than 1.5x the expected move away
}

# --- Time gate ----------------------------------------------------------
# Minimum calendar days to expiry. Buying the nearest expiry inside this
# window bleeds theta faster than a cheap-IV edge can pay off, so those
# strikes are skipped.
# NOTE: the scanner only looks at the *nearest* expiry, so within this many
# days of a monthly expiry the stock universe will return few/no ideas until
# the next expiry becomes nearest.
MIN_DTE_DAYS = 3

# FIX 3: NSE trading-holiday calendar used by get_actual_trading_days_to_expiry().
# A simple, hand-maintained list of 'YYYY-MM-DD' strings. When this is empty (or
# an entry is malformed) the holiday-aware DTE helper degrades gracefully to the
# existing weekend-only behaviour, so it can never break current logic.
# Populate with the NSE equity-derivatives holiday list for the current year,
# e.g. ["2026-01-26", "2026-03-06", ...].
NSE_HOLIDAYS = []

# --- Scoring ------------------------------------------------------------
MIN_SCORE = 55  # minimum composite score (0-100) to surface an opportunity

# FIX 8: directional confirmation layer. Cheap IV alone should not generate a
# high-confidence signal, so the base composite from score_option() is blended
# with a 0-100 directional_score that rewards price-structure / trend / flow
# agreement with the option's direction.
#   final_score = base_score * (1 - DIRECTIONAL_WEIGHT)
#               + directional_score * DIRECTIONAL_WEIGHT
# Set ENABLE_DIRECTIONAL_CONFIRMATION = False to get byte-identical legacy
# scores (score == base composite). Existing component weights inside
# score_option() are untouched — this only enhances the final blend.
ENABLE_DIRECTIONAL_CONFIRMATION = True
DIRECTIONAL_WEIGHT = 0.15

# FIX 9: optional futures Open-Interest confirmation. OFF by default. When on,
# a confirming futures buildup (LONG for calls / SHORT for puts) adds a small
# bonus to directional_score. Uses the isolated, fail-open oi_validator module;
# if futures OI is unavailable the scan proceeds unchanged (no penalty, no
# exception). FUTURES_OI_BONUS is added to directional_score (then clipped 0-100).
ENABLE_FUTURES_OI_CONFIRMATION = False
FUTURES_OI_BONUS = 10

# FIX 10: append analytics columns (entry/stop/target/risk_reward/score/
# directional_score/base_score/vol_mode/trend/dte/trading_dte/scan_timestamp)
# to the CSV so future win-rate tracking has the raw data it needs. These are
# additive columns only; existing columns are preserved. Kept as a flag for
# documentation/intent — the columns live in the per-opportunity record either
# way, so toggling this does not remove existing output.
EXPORT_BACKTEST_COLUMNS = True

# --- Trade plan (single-leg premium levels) -----------------------------
TRADE_PLAN = {
    "stop_loss_mult": 0.65,  # stop-loss at 65% of entry mid
    "target_mult": 1.8,      # target at 180% of entry mid
}

# --- "Strong liquidity" annotation thresholds ---------------------------
# Cosmetic only: drives the human-readable reason text on an alert.
STRONG_LIQUIDITY = {
    "oi": 10000,
    "volume": 1000,
}
