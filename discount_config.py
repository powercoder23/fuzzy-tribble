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
MIN_DTE_DAYS = 5

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

# --- Trade plan (single-leg premium levels, INTRADAY) -------------------
# Calibrated for same-day, single-leg Volatility Expansion Plays on ~5-DTE
# options. Levels are multiples of the entry mid premium.
#   SL  = entry * 0.85   (-15%, hard exit)
#   T1  = entry * 1.25   (+25%, book t1_book_fraction of the position)
#   T2  = entry * 1.45   (+45%, exit remaining runner)
# After T1 the runner's stop moves to breakeven (entry) — locks the T1 gain.
TRADE_PLAN = {
    "stop_loss_mult": 0.85,      # -15%
    "t1_mult": 1.25,             # +25%
    "t2_mult": 1.45,             # +45%
    "t1_book_fraction": 0.70,    # book 70% at T1, trail 30%
    "runner_stop_to_breakeven": True,
    # kept for backward-compat with existing CSV/analytics columns:
    "target_mult": 1.25,         # legacy "target" == T1
}

# --- Intraday session rules (no carry-forward) --------------------------
INTRADAY = {
    "scan_interval_min": 15,      # scanner cadence: find + book NEW trades every 15 min
    "monitor_interval_min": 5,    # order-manager cadence: re-price + exit-manage OPEN positions every 5 min
    "session_start": "09:30",     # first scan
    "no_entry_after": "15:00",    # no NEW paper trades after this time (paper: allow later)
    "square_off": "15:20",        # force-close any open paper trade
    "monitor_until": "15:20",     # keep re-pricing open trades until square-off
    "eod_summary_at": "15:25",    # send realized-P&L summary
    "max_signals_per_day": 5,     # max 5 paper trades per day (per-symbol cap)
    "min_premium": 5.0,           # skip options trading below ₹5 (far-OTM junk)
    "max_per_symbol_per_day": 1,  # at most N paper trades per underlying per day
                                  # (stops one symbol — e.g. ABCAPITAL — eating
                                  # every slot via different strikes)
    "max_risk_rupees": 1500.0,    # skip a signal whose 1-lot risk
                                  # (entry-sl)*lot_size exceeds this budget, so a
                                  # big-lot cheap option can't quietly risk 5x a
                                  # small-lot one. 0/None disables the cap. Hard
                                  # ceiling applied to EVERY paper-trade strategy
                                  # (see paper_trader.book_signal), not just discount.
}

# --- Universe (DISCOUNT SCANNER ONLY) -----------------------------------
# Trim the scan universe to the most liquid F&O names. Ranking uses the latest
# OI x volume from the local iv_history.db (zero extra API calls). Does NOT
# affect momentum / break-bounce / directional-IV — they keep their own
# universes. Set LIQUID_UNIVERSE_ONLY = False to scan the full F&O list.
LIQUID_UNIVERSE_ONLY = True
LIQUID_UNIVERSE_SIZE = 120

# --- Upstox API pacing (replaces the Dhan 1-req/3s throttle) ------------
# Upstox "Other Standard APIs" limits: 50/sec, 500/min, 2000/30min (per-user).
# We pace well under these. iv-collector shares the same token, so the
# 30-min option-chain budget is shared — keep total scanner calls modest.
UPSTOX_MAX_REQ_PER_SEC = 7          # ~7/s -> well under 50/s and 500/min
UPSTOX_MIN_REQ_INTERVAL_SEC = 1.0 / UPSTOX_MAX_REQ_PER_SEC
CACHE_DAILY_CANDLES = True          # fetch daily candles once per day per stock
# Soft budget guard: stop issuing new chain calls in a scan if we'd exceed this
# many option-chain requests in the trailing 30 min (leaves room for iv-collector).
CHAIN_CALLS_30MIN_BUDGET = 1500

# --- "Strong liquidity" annotation thresholds ---------------------------
# Cosmetic only: drives the human-readable reason text on an alert.
STRONG_LIQUIDITY = {
    "oi": 10000,
    "volume": 1000,
}
