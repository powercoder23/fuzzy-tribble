# -*- coding: utf-8 -*-
"""
market_context/config.py — every tunable for the Market Context subsystem.

Nothing in this subsystem hardcodes a threshold. Resolution order matches the
rest of the platform (see paper_trader._flag_override / order_manager._resolve_mode):

    settings-DB flag  ->  environment variable  ->  default here

Two deliberate properties:

1. Every threshold is a PERCENTILE or a NORMALISED RATIO, never a raw price or
   point value. NIFTY moving from 24,000 to 30,000 must not require
   recalibration, and the same config must work for BANKNIFTY.

2. CONFIG_VERSION + config_hash() are stamped onto every persisted row. If a
   threshold changes, historical rows keep the old hash so research can
   partition rather than silently mixing two definitions of "HIGH_VOL".

PHASE 1 OPERATING RULE (operator decision 2026-08-03)
-----------------------------------------------------
Market Context is OBSERVATIONAL ONLY. It must not veto entries, change exits,
change stop-losses, change targets, or change sizing. MODE defaults to
"observe" and the only modes implemented today are "off" and "observe".
"soft"/"hard" are reserved and intentionally NOT wired to anything.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Resolution helpers — settings-DB override wins, then env, then default.
# All fail-open: a broken settings DB must never break config resolution.
# --------------------------------------------------------------------------- #
def _flag_override(key: str):
    try:
        import settings_store
        return settings_store.get_flag_raw(key)
    except Exception:
        return None


def _f(env_key: str, default: float, flag_key: str | None = None) -> float:
    raw = _flag_override(flag_key) if flag_key else None
    if raw is None:
        raw = os.getenv(env_key)
    try:
        return float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _i(env_key: str, default: int, flag_key: str | None = None) -> int:
    raw = _flag_override(flag_key) if flag_key else None
    if raw is None:
        raw = os.getenv(env_key)
    try:
        return int(float(raw)) if raw is not None else int(default)
    except (TypeError, ValueError):
        return int(default)


def _s(env_key: str, default: str, flag_key: str | None = None) -> str:
    raw = _flag_override(flag_key) if flag_key else None
    if raw is None:
        raw = os.getenv(env_key)
    return str(raw).strip() if raw not in (None, "") else str(default)


def _b(env_key: str, default: bool, flag_key: str | None = None) -> bool:
    raw = _s(env_key, "true" if default else "false", flag_key)
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


# --------------------------------------------------------------------------- #
# Master mode
# --------------------------------------------------------------------------- #
#   off      -> subsystem does nothing; get() returns NEUTRAL_CONTEXT
#   observe  -> collect, classify, persist. Influences NOTHING. (Phase 1)
#   soft     -> RESERVED. Not implemented. Treated as "observe".
#   hard     -> RESERVED. Not implemented. Treated as "observe".
MODE = _s("MC_MODE", "observe", "MC_MODE")

#: Modes that are actually wired. Anything else degrades to "observe" so a
#: mis-set env var can never silently start influencing trades.
IMPLEMENTED_MODES = ("off", "observe")


def effective_mode() -> str:
    m = (MODE or "observe").strip().lower()
    return m if m in IMPLEMENTED_MODES else "observe"


def is_enabled() -> bool:
    return effective_mode() != "off"


def influences_trading() -> bool:
    """Phase 1 rule: ALWAYS False. Kept as an explicit, greppable predicate so
    that when the validation gate is eventually passed there is exactly one
    place to change, and every call site can be found."""
    return False


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))

#: Separate file from iv_history.db on purpose: that DB is 315 MB and written
#: continuously by iv-collector. Tick-derived bars would add write contention
#: to the platform's most important table for no benefit.
DB_PATH = str(DATA_DIR / "market_context.db")

BUSY_TIMEOUT_MS = _i("MC_BUSY_TIMEOUT_MS", 30000)

# --------------------------------------------------------------------------- #
# get() behaviour
# --------------------------------------------------------------------------- #
#: Process-local cache TTL. Makes get() free for batch callers — e.g.
#: paper_trader.collect_factor_snapshot() booking 40 signals does ONE read.
GET_CACHE_TTL_SEC = _f("MC_GET_CACHE_TTL_SEC", 10.0)

#: Past this age the snapshot is reported unavailable rather than returned
#: stale. A stale regime is worse than no regime: it looks like information.
MAX_CONTEXT_AGE_SEC = _f("MC_MAX_CONTEXT_AGE_SEC", 300.0)

# --------------------------------------------------------------------------- #
# Service cadence
# --------------------------------------------------------------------------- #
SNAPSHOT_INTERVAL_SEC = _i("MC_SNAPSHOT_INTERVAL_SEC", 60)
BAR_FLUSH_INTERVAL_SEC = _i("MC_BAR_FLUSH_INTERVAL_SEC", 60)
HEARTBEAT_LOG_MINUTES = _i("MC_HEARTBEAT_LOG_MINUTES", 15)

#: How often dynamically-subscribed instruments' latest tick is persisted to
#: mc_live_quotes. Deliberately much faster than BAR_FLUSH_INTERVAL_SEC — the
#: whole point is beating the old 5-minute REST poll for open-position
#: monitoring, not researching bar-level data.
LIVE_QUOTE_FLUSH_INTERVAL_SEC = _f("MC_LIVE_QUOTE_FLUSH_INTERVAL_SEC", 3.0)
#: get_quote() reports a quote as unavailable (not stale) past this age, so a
#: caller falls back to REST rather than trading on a dead number.
QUOTE_STALE_SEC = _f("MC_QUOTE_STALE_SEC", 15.0)

MARKET_OPEN = _s("MC_MARKET_OPEN", "09:00")
MARKET_CLOSE = _s("MC_MARKET_CLOSE", "15:45")

# --------------------------------------------------------------------------- #
# WebSocket / feed  (Phase 1 — config defined now so schema + budget are stable)
# --------------------------------------------------------------------------- #
#: auto | standard | plus.  "auto" probes the live connection allowance at
#: startup and scales the universe accordingly (operator decision: plan tier
#: is a deployment concern, not an architectural one).
PLAN_TIER = _s("MC_PLAN_TIER", "auto", "MC_PLAN_TIER")

WS_MAX_KEYS_PER_CONNECTION = _i("MC_WS_MAX_KEYS", 100)
WS_CONNECTIONS_STANDARD = _i("MC_WS_CONNECTIONS_STANDARD", 1)
WS_CONNECTIONS_PLUS = _i("MC_WS_CONNECTIONS_PLUS", 5)

# ── Plan-tier probe ──────────────────────────────────────────────────────── #
# With PLAN_TIER="auto" the service measures the account's concurrent-
# connection allowance instead of guessing: it opens throwaway sockets and
# counts how many coexist. Opening 2 is enough to separate Standard (1) from
# Plus (5), so the probe is deliberately cheap.
#
# Skipped entirely when PLAN_TIER is an explicit "standard" or "plus".
PLAN_PROBE_ENABLED = _b("MC_PLAN_PROBE_ENABLED", True)
PLAN_PROBE_MAX = _i("MC_PLAN_PROBE_MAX", 2)
PLAN_PROBE_TIMEOUT_SEC = _f("MC_PLAN_PROBE_TIMEOUT_SEC", 10.0)
# The result is cached in mc_meta and reused for this many days — a plan
# change is a billing event, not an intraday one, and re-probing every restart
# would burn connections during a reconnect storm.
PLAN_PROBE_TTL_DAYS = _i("MC_PLAN_PROBE_TTL_DAYS", 7)

#: Reconnect: we own the loop rather than using the SDK's auto_reconnect(),
#: because the SDK cannot re-run REST re-authorisation (the WSS URL is
#: short-lived and the access token rotates daily), cannot record a feed gap,
#: and cannot trigger a REST resync.
RECONNECT_BASE_SEC = _f("MC_RECONNECT_BASE_SEC", 1.0)
RECONNECT_MAX_SEC = _f("MC_RECONNECT_MAX_SEC", 30.0)
RECONNECT_MULTIPLIER = _f("MC_RECONNECT_MULTIPLIER", 2.0)
RECONNECT_JITTER_PCT = _f("MC_RECONNECT_JITTER_PCT", 0.25)

#: MarketDataStreamerV3.connect() only starts the feeder — the WS handshake
#: finishes on another thread. How long to wait for the `open` event before
#: calling the attempt failed.
WS_CONNECT_TIMEOUT_SEC = _f("MC_WS_CONNECT_TIMEOUT_SEC", 15.0)

#: A socket that opens and dies immediately is a FAILED connect, not a
#: successful one that happened to end. Crediting it resets the backoff, so a
#: sustained rejection (expired token, plan connection limit) turns into a
#: ~1/sec reconnect storm that never escalates. Observed 2026-08-04 11:17.
WS_MIN_UPTIME_SEC = _f("MC_WS_MIN_UPTIME_SEC", 30.0)

#: Staleness beats ping/pong: a dead feed usually presents as a live TCP
#: connection that stopped delivering. Judged on tier-1 instruments only —
#: they always tick during market hours; a quiet mid-cap is data, not an outage.
WS_WATCHDOG_INTERVAL_SEC = _f("MC_WS_WATCHDOG_INTERVAL_SEC", 5.0)
WS_STALE_TIMEOUT_SEC = _f("MC_WS_STALE_TIMEOUT_SEC", 20.0)
WS_TIER3_STALE_WARN_SEC = _f("MC_WS_TIER3_STALE_WARN_SEC", 300.0)

#: Gaps longer than this trigger a REST resync (batched quotes + tier-1 candle
#: backfill). Shorter gaps self-heal from the next frames.
RESYNC_THRESHOLD_SEC = _f("MC_RESYNC_THRESHOLD_SEC", 120.0)

# --------------------------------------------------------------------------- #
# Universe sizing per tier — scales automatically with the detected plan.
# --------------------------------------------------------------------------- #
#: Tier 1 (always, mode=full): NIFTY spot, BANKNIFTY spot, India VIX,
#: NIFTY fut near+next, BANKNIFTY fut near+next.
TIER1_KEYS = 7
#: Tier 2 (mode=ltpc): sector indices for RS + dispersion.
TIER2_KEYS = _i("MC_TIER2_KEYS", 14)

#: Tier 3/4 scale with the plan. Standard keeps the operator's stated
#: 50-60 liquid stock target; Plus expands to the full monitored universe.
TIER3_STOCKS_STANDARD = _i("MC_TIER3_STOCKS_STANDARD", 59)
TIER4_FUTURES_STANDARD = _i("MC_TIER4_FUTURES_STANDARD", 20)
TIER3_STOCKS_PLUS = _i("MC_TIER3_STOCKS_PLUS", 220)
TIER4_FUTURES_PLUS = _i("MC_TIER4_FUTURES_PLUS", 60)

#: Below this many breadth constituents the reading is a PARTIAL-UNIVERSE
#: subsample, not market breadth. Surfaced via mc_breadth.is_subsample and
#: penalised in confidence.
#:
#: Note the sample is NOT simply "the biggest names": it is whatever the
#: liquidity ranking selects (see instruments.LIQUIDITY_METRIC), intersected
#: with whatever iv_history actually covers — about 119 of ~208 F&O names.
#: Both truncations are silent, which is why universe_size and sample_quality
#: are persisted on every row.
BREADTH_FULL_UNIVERSE_MIN = _i("MC_BREADTH_FULL_UNIVERSE_MIN", 150)


class SubscriptionBudget:
    """Resolved per-tier instrument counts for a given plan."""

    __slots__ = ("tier", "connections", "capacity", "tier1", "tier2", "tier3",
                 "tier4", "breadth_is_subsample")

    def __init__(self, tier, connections, capacity, tier1, tier2, tier3, tier4):
        self.tier = tier
        self.connections = connections
        self.capacity = capacity
        self.tier1 = tier1
        self.tier2 = tier2
        self.tier3 = tier3
        self.tier4 = tier4
        self.breadth_is_subsample = tier3 < BREADTH_FULL_UNIVERSE_MIN

    @property
    def total(self) -> int:
        return self.tier1 + self.tier2 + self.tier3 + self.tier4

    def as_dict(self) -> dict:
        return {
            "tier": self.tier, "connections": self.connections,
            "capacity": self.capacity, "total": self.total,
            "tier1": self.tier1, "tier2": self.tier2,
            "tier3": self.tier3, "tier4": self.tier4,
            "breadth_is_subsample": self.breadth_is_subsample,
        }

    def __repr__(self) -> str:
        return f"<SubscriptionBudget {self.as_dict()}>"


def subscription_budget(detected_connections: int | None = None) -> SubscriptionBudget:
    """Resolve the per-tier instrument budget for the live plan.

    `detected_connections` is what the feed client actually managed to open at
    startup. Passing None resolves from PLAN_TIER config alone (used by tests
    and by anything that needs the budget before the socket is up).

    Tiers 1 and 2 are MANDATORY and are always satisfied first — the regime
    engine cannot run without them. Tier 3/4 absorb whatever capacity is left,
    so a tighter-than-expected cap degrades breadth coverage rather than
    breaking the subsystem.
    """
    tier = (PLAN_TIER or "auto").strip().lower()
    if tier == "auto":
        if detected_connections is None:
            tier = "standard"           # safe assumption until probed
        else:
            tier = "plus" if detected_connections > WS_CONNECTIONS_STANDARD else "standard"

    if tier == "plus":
        connections = detected_connections or WS_CONNECTIONS_PLUS
        want3, want4 = TIER3_STOCKS_PLUS, TIER4_FUTURES_PLUS
    else:
        tier = "standard"
        connections = detected_connections or WS_CONNECTIONS_STANDARD
        want3, want4 = TIER3_STOCKS_STANDARD, TIER4_FUTURES_STANDARD

    capacity = max(connections, 1) * WS_MAX_KEYS_PER_CONNECTION
    remaining = max(capacity - TIER1_KEYS - TIER2_KEYS, 0)

    # Tier 4 (futures positioning) is funded before tier 3 (breadth): the
    # positioning axis is mechanical and high-confidence, breadth degrades
    # gracefully to a subsample.
    tier4 = min(want4, remaining)
    tier3 = min(want3, remaining - tier4)

    return SubscriptionBudget(
        tier=tier, connections=connections, capacity=capacity,
        tier1=TIER1_KEYS, tier2=TIER2_KEYS, tier3=tier3, tier4=tier4,
    )


# --------------------------------------------------------------------------- #
# AXIS: trend
# --------------------------------------------------------------------------- #
#: Kaufman Efficiency Ratio = |net move| / sum(|bar moves|). Measures
#: DIRECTIONAL EFFICIENCY — how much net travel per unit of path. ER->1 is a
#: clean trend, ER->0 is chop with the same gross movement. Scale-free, so one
#: threshold works for NIFTY and BANKNIFTY alike.
TREND_ER_LOOKBACK = _i("MC_TREND_ER_LOOKBACK", 20)
#: Asymmetric bands = hysteresis. Enter "trending" at 0.35, but do not fall
#: back to "range" until below 0.20. Prevents boundary chatter.
TREND_ER_TRENDING_MIN = _f("MC_TREND_ER_TRENDING_MIN", 0.35)
TREND_ER_RANGE_MAX = _f("MC_TREND_ER_RANGE_MAX", 0.20)

TREND_SS_PERIOD = _i("MC_TREND_SS_PERIOD", 10)
TREND_SS_LOOKBACK = _i("MC_TREND_SS_LOOKBACK", 3)
TREND_SS_MIN_BARS = _i("MC_TREND_SS_MIN_BARS", 6)

TREND_SCORE_UP_MIN = _f("MC_TREND_SCORE_UP_MIN", 0.30)
TREND_SCORE_DOWN_MAX = _f("MC_TREND_SCORE_DOWN_MAX", -0.30)

TREND_WEIGHTS = {
    "ef_ratio": _f("MC_TREND_W_ER", 0.35),
    "ss_slope": _f("MC_TREND_W_SLOPE", 0.30),
    "mom_z": _f("MC_TREND_W_MOM", 0.20),
    "vwap_position": _f("MC_TREND_W_VWAP", 0.15),
}

#: Structural events on the trend axis.
STRUCT_ORB_MINUTES = _i("MC_STRUCT_ORB_MINUTES", 15)
STRUCT_BREAKOUT_BUFFER_PCT = _f("MC_STRUCT_BREAKOUT_BUFFER_PCT", 0.05)
#: A REVERSAL call needs THREE independent confirmations (breadth divergence,
#: trend-score sign change, positioning flip). Reversal has the worst
#: false-positive rate of any state, so it gets the strictest evidence bar.
STRUCT_REVERSAL_MIN_CONFIRMATIONS = _i("MC_STRUCT_REVERSAL_MIN_CONF", 3)

# --------------------------------------------------------------------------- #
# AXIS: volatility
# --------------------------------------------------------------------------- #
#: Yang-Zhang realized volatility. Chosen over close-to-close/Parkinson/
#: Garman-Klass because it is the only common estimator handling BOTH the
#: overnight opening jump (routine in Indian index futures) and intraday
#: drift, at ~8x the efficiency of close-to-close.
VOL_RV_SHORT_BARS = _i("MC_VOL_RV_SHORT_BARS", 30)
VOL_RV_LONG_BARS = _i("MC_VOL_RV_LONG_BARS", 120)
VOL_RV_MIN_BARS = _i("MC_VOL_RV_MIN_BARS", 10)
#: Trading minutes/year used to annualise 1-min RV: 375 min x 250 sessions.
VOL_ANNUALISATION_MINUTES = _f("MC_VOL_ANNUALISATION_MINUTES", 93750.0)

VOL_VIX_PERCENTILE_LOOKBACK_DAYS = _i("MC_VOL_VIX_PCTILE_LOOKBACK", 252)
VOL_PANIC_VIX_PCTILE = _f("MC_VOL_PANIC_VIX_PCTILE", 95.0)
VOL_HIGH_VIX_PCTILE = _f("MC_VOL_HIGH_VIX_PCTILE", 75.0)
VOL_HIGH_EXIT_PCTILE = _f("MC_VOL_HIGH_EXIT_PCTILE", 60.0)   # hysteresis
VOL_LOW_VIX_PCTILE = _f("MC_VOL_LOW_VIX_PCTILE", 25.0)
VOL_LOW_EXIT_PCTILE = _f("MC_VOL_LOW_EXIT_PCTILE", 35.0)     # hysteresis
VOL_PANIC_RV_RATIO = _f("MC_VOL_PANIC_RV_RATIO", 2.0)
VOL_EXPANSION_RV_RATIO = _f("MC_VOL_EXPANSION_RV_RATIO", 1.15)

VOL_WEIGHTS = {
    "vix_percentile": _f("MC_VOL_W_VIX", 0.40),
    "rv_ratio": _f("MC_VOL_W_RV_RATIO", 0.30),
    "rv_level": _f("MC_VOL_W_RV_LEVEL", 0.20),
    "vol_of_vol": _f("MC_VOL_W_VOV", 0.10),
}

#: Variance Risk Premium = IV^2 - E[RV^2]. Persisted as a descriptive feature
#: on the volatility axis. NOTE: whether a given VRP means "sell premium" is a
#: TRADING decision and deliberately lives in the strategy, not here.
VRP_ENABLED = _b("MC_VRP_ENABLED", True)

# --------------------------------------------------------------------------- #
# AXIS: liquidity
# --------------------------------------------------------------------------- #
#: Capacity of the market to absorb size. Derived from D5 depth + quoted
#: spread on tier-1 instruments, normalised against each instrument's own
#: trailing session distribution (so it is comparable across instruments).
LIQ_SPREAD_LOOKBACK_BARS = _i("MC_LIQ_SPREAD_LOOKBACK_BARS", 120)
LIQ_THIN_SPREAD_PCTILE = _f("MC_LIQ_THIN_SPREAD_PCTILE", 75.0)
LIQ_ILLIQUID_SPREAD_PCTILE = _f("MC_LIQ_ILLIQUID_SPREAD_PCTILE", 90.0)
LIQ_LIQUID_SPREAD_PCTILE = _f("MC_LIQ_LIQUID_SPREAD_PCTILE", 25.0)
LIQ_DEPTH_IMBALANCE_MAX = _f("MC_LIQ_DEPTH_IMBALANCE_MAX", 3.0)

LIQ_WEIGHTS = {
    "spread_pctile": _f("MC_LIQ_W_SPREAD", 0.50),
    "depth_total": _f("MC_LIQ_W_DEPTH", 0.30),
    "depth_imbalance": _f("MC_LIQ_W_IMBALANCE", 0.20),
}

# --------------------------------------------------------------------------- #
# AXIS: participation
# --------------------------------------------------------------------------- #
#: How much activity there is, versus this time-of-day's own normal.
#: Intraday volume is strongly U-shaped, so a raw volume comparison between
#: 09:20 and 13:00 is meaningless — every reading is normalised against the
#: same 15-minute bucket over the trailing N sessions.
PART_SEASONALITY_LOOKBACK_DAYS = _i("MC_PART_SEASONALITY_DAYS", 20)
PART_BUCKET_MINUTES = _i("MC_PART_BUCKET_MINUTES", 15)
PART_HIGH_RATIO = _f("MC_PART_HIGH_RATIO", 1.30)
PART_LOW_RATIO = _f("MC_PART_LOW_RATIO", 0.70)
PART_MIN_SESSIONS = _i("MC_PART_MIN_SESSIONS", 5)

PART_WEIGHTS = {
    "volume_ratio": _f("MC_PART_W_VOLUME", 0.50),
    "trade_count_ratio": _f("MC_PART_W_TRADES", 0.30),
    "active_names_pct": _f("MC_PART_W_ACTIVE", 0.20),
}

# --------------------------------------------------------------------------- #
# AXIS: positioning
# --------------------------------------------------------------------------- #
#: Futures price x OI quadrant. Deadbands are MANDATORY: without them a 0.01%
#: drift produces a confident LONG_BUILDUP.
POS_MIN_PRICE_CHG_PCT = _f("MC_POS_MIN_PRICE_CHG_PCT", 0.15)
POS_MIN_OI_CHG_PCT = _f("MC_POS_MIN_OI_CHG_PCT", 0.50)
#: Index state = OI-weighted blend of NIFTY + BANKNIFTY futures.
POS_INDEX_WEIGHTS = {
    "NIFTY": _f("MC_POS_W_NIFTY", 0.60),
    "BANKNIFTY": _f("MC_POS_W_BANKNIFTY", 0.40),
}
POS_BASIS_ANNUALISATION_DAYS = _f("MC_POS_BASIS_ANN_DAYS", 365.0)

# --------------------------------------------------------------------------- #
# AXIS: breadth
# --------------------------------------------------------------------------- #
#: A name counts as advancing/declining only past this deadband.
BREADTH_MIN_MOVE_PCT = _f("MC_BREADTH_MIN_MOVE_PCT", 0.30)
BREADTH_MIN_NAMES = _i("MC_BREADTH_MIN_NAMES", 20)
BREADTH_STRONG_POSITIVE_PCT = _f("MC_BREADTH_STRONG_POSITIVE_PCT", 70.0)
BREADTH_POSITIVE_PCT = _f("MC_BREADTH_POSITIVE_PCT", 57.0)
BREADTH_NEGATIVE_PCT = _f("MC_BREADTH_NEGATIVE_PCT", 43.0)
BREADTH_STRONG_NEGATIVE_PCT = _f("MC_BREADTH_STRONG_NEGATIVE_PCT", 30.0)
BREADTH_THRUST_LOOKBACK_MIN = _i("MC_BREADTH_THRUST_LOOKBACK_MIN", 15)

#: Volume breadth (up-volume / total volume) separates a thin 55% tape from a
#: heavy one. Name-count breadth alone cannot.
BREADTH_WEIGHTS = {
    "adv_dec_pct": _f("MC_BREADTH_W_ADVDEC", 0.50),
    "volume_breadth_pct": _f("MC_BREADTH_W_VOLUME", 0.35),
    "thrust": _f("MC_BREADTH_W_THRUST", 0.15),
}

#: Cross-sectional sector dispersion -> implied-correlation proxy. High
#: correlation is why N "diversified" positions can be one bet.
DISPERSION_MIN_SECTORS = _i("MC_DISPERSION_MIN_SECTORS", 6)

# --------------------------------------------------------------------------- #
# Hysteresis / dwell — applied uniformly to EVERY axis.
# A classifier that flips state every bar is worse than no classifier.
# --------------------------------------------------------------------------- #
MIN_DWELL_MINUTES = _i("MC_MIN_DWELL_MINUTES", 5)
CONFIRMATION_COUNT = _i("MC_CONFIRMATION_COUNT", 2)
CONFIRMATION_WINDOW = _i("MC_CONFIRMATION_WINDOW", 3)
EMIT_TRANSITIONING = _b("MC_EMIT_TRANSITIONING", True)

MOMENTUM_LOOKBACK_MIN = _i("MC_MOMENTUM_LOOKBACK_MIN", 10)
MOMENTUM_EPS = _f("MC_MOMENTUM_EPS", 0.05)

TRANSITION_HORIZON_MIN = _i("MC_TRANSITION_HORIZON_MIN", 30)
TRANSITION_MIN_HISTORY = _i("MC_TRANSITION_MIN_HISTORY", 500)
TRANSITION_BASE_PROB = _f("MC_TRANSITION_BASE_PROB", 0.15)
TRANSITION_K_MOMENTUM = _f("MC_TRANSITION_K_MOMENTUM", 0.30)
TRANSITION_K_MARGIN = _f("MC_TRANSITION_K_MARGIN", 0.25)
TRANSITION_K_CONFIDENCE = _f("MC_TRANSITION_K_CONF", 0.20)

# --------------------------------------------------------------------------- #
# Confidence — per axis. NOT a probability of profit; a measure of how well
# IDENTIFIED the current state is.
# --------------------------------------------------------------------------- #
CONF_WEIGHTS = {
    "agreement": _f("MC_CONF_W_AGREE", 0.35),
    "data_quality": _f("MC_CONF_W_DATA", 0.25),
    "dwell": _f("MC_CONF_W_DWELL", 0.20),
    "margin": _f("MC_CONF_W_MARGIN", 0.20),
}

#: Confidence ceiling applied when breadth is a large-cap subsample rather
#: than the full universe. Surfaces the plan-tier limitation instead of
#: hiding it.
CONF_SUBSAMPLE_CEILING = _f("MC_CONF_SUBSAMPLE_CEILING", 0.70)

# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
RETAIN_BARS_DAYS = _i("MC_RETAIN_BARS_DAYS", 90)
RETAIN_FUTURES_DAYS = _i("MC_RETAIN_FUTURES_DAYS", 90)
#: mc_features / mc_regime are kept forever — ~375 rows/day each, and they
#: ARE the research asset.

# --------------------------------------------------------------------------- #
# Version stamping — what makes historical research valid
# --------------------------------------------------------------------------- #
CONFIG_VERSION = "mc-v1.0"

#: Only values that change the MEANING of a persisted classification belong
#: here. Cadence/retention/IO knobs are excluded: changing the cache TTL must
#: not invalidate history.
_HASHED_KEYS = (
    "TREND_ER_LOOKBACK", "TREND_ER_TRENDING_MIN", "TREND_ER_RANGE_MAX",
    "TREND_SS_PERIOD", "TREND_SS_LOOKBACK", "TREND_SCORE_UP_MIN",
    "TREND_SCORE_DOWN_MAX", "TREND_WEIGHTS",
    "STRUCT_ORB_MINUTES", "STRUCT_BREAKOUT_BUFFER_PCT",
    "STRUCT_REVERSAL_MIN_CONFIRMATIONS",
    "VOL_RV_SHORT_BARS", "VOL_RV_LONG_BARS", "VOL_ANNUALISATION_MINUTES",
    "VOL_VIX_PERCENTILE_LOOKBACK_DAYS", "VOL_PANIC_VIX_PCTILE",
    "VOL_HIGH_VIX_PCTILE", "VOL_HIGH_EXIT_PCTILE", "VOL_LOW_VIX_PCTILE",
    "VOL_LOW_EXIT_PCTILE", "VOL_PANIC_RV_RATIO", "VOL_EXPANSION_RV_RATIO",
    "VOL_WEIGHTS",
    "LIQ_SPREAD_LOOKBACK_BARS", "LIQ_THIN_SPREAD_PCTILE",
    "LIQ_ILLIQUID_SPREAD_PCTILE", "LIQ_LIQUID_SPREAD_PCTILE",
    "LIQ_DEPTH_IMBALANCE_MAX", "LIQ_WEIGHTS",
    "PART_SEASONALITY_LOOKBACK_DAYS", "PART_BUCKET_MINUTES",
    "PART_HIGH_RATIO", "PART_LOW_RATIO", "PART_WEIGHTS",
    "POS_MIN_PRICE_CHG_PCT", "POS_MIN_OI_CHG_PCT", "POS_INDEX_WEIGHTS",
    "BREADTH_MIN_MOVE_PCT", "BREADTH_MIN_NAMES",
    "BREADTH_STRONG_POSITIVE_PCT", "BREADTH_POSITIVE_PCT",
    "BREADTH_NEGATIVE_PCT", "BREADTH_STRONG_NEGATIVE_PCT",
    "BREADTH_THRUST_LOOKBACK_MIN", "BREADTH_WEIGHTS",
    "MIN_DWELL_MINUTES", "CONFIRMATION_COUNT", "CONFIRMATION_WINDOW",
    "MOMENTUM_LOOKBACK_MIN", "MOMENTUM_EPS",
    "TRANSITION_HORIZON_MIN", "TRANSITION_BASE_PROB",
    "CONF_WEIGHTS", "CONF_SUBSAMPLE_CEILING",
    "CONFIG_VERSION",
)


def config_snapshot() -> dict:
    """The subset of config that defines what a persisted classification MEANS."""
    g = globals()
    return {k: g.get(k) for k in _HASHED_KEYS}


def config_hash() -> str:
    """Stable 12-char hash of the meaning-bearing config.

    Stamped onto every mc_features / mc_regime row. If a threshold changes,
    older rows keep the old hash, so research can partition instead of
    silently mixing two different definitions of the same state label.
    """
    blob = json.dumps(config_snapshot(), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
