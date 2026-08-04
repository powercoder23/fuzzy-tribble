# -*- coding: utf-8 -*-
"""
test_market_context.py — Phase 0 tests for the Market Context subsystem.

Phase 0 ships: schema, config, a fail-open get(), and persistence of the
snapshot into paper_trades.factors_json. Nothing influences trading, so the
tests concentrate on the two properties that matter at this stage:

  1. get() is UNCONDITIONALLY SAFE — it cannot raise, cannot block a scan,
     and cannot present stale or half-written data as information.
  2. The persisted shape is stable, because it becomes the historical record
     used later to measure whether context predicts anything.
"""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

import market_context
from market_context import config as cfg
from market_context import store
from market_context.contracts import (
    ALL_AXES,
    AXIS_TREND,
    AXIS_VOLATILITY,
    HIGH_VOL,
    NEUTRAL_CONTEXT,
    SCHEMA_VERSION,
    TRENDING_UP,
    UNKNOWN,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def db(tmp_path, monkeypatch):
    """Isolated market_context.db; get() is pointed at it."""
    path = str(tmp_path / "market_context.db")
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(cfg, "DB_PATH", path)
    store.init_db(path)
    market_context.invalidate()
    yield path
    market_context.invalidate()


def _ts(minutes_ago: float = 0.0) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _write_regime(db_path, ts=None, axes_available=None, **overrides):
    """Insert one mc_regime row with sensible defaults."""
    ts = ts or _ts()
    axes_available = axes_available if axes_available is not None else [AXIS_TREND, AXIS_VOLATILITY]
    row = {
        "ts": ts,
        "trend_state": TRENDING_UP,
        "trend_score": 0.62,
        "trend_confidence": 0.71,
        "trend_direction": "STRENGTHENING",
        "trend_dwell_minutes": 25,
        "trend_transition_prob": 0.12,
        "trend_event": "BREAKOUT",
        "volatility_state": HIGH_VOL,
        "volatility_score": 0.80,
        "volatility_confidence": 0.66,
        "volatility_direction": "STABLE",
        "volatility_dwell_minutes": 40,
        "volatility_transition_prob": 0.20,
        "axes_available": json.dumps(axes_available),
        "axis_inputs": json.dumps({
            AXIS_TREND: {"ef_ratio": 0.44, "ss_slope_pct": 0.09},
            AXIS_VOLATILITY: {"vix_percentile": 81.0, "rv_ratio": 1.35},
        }),
        "reasons": json.dumps({
            AXIS_TREND: ["ER 0.44 >= 0.35", "slope positive"],
            AXIS_VOLATILITY: ["VIX 81st pctile"],
        }),
        "data_quality": 0.92,
        "config_hash": cfg.config_hash(),
        "config_version": cfg.CONFIG_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with store.connect(db_path) as conn:
        conn.execute(f"INSERT OR REPLACE INTO mc_regime ({cols}) VALUES ({marks})",
                     list(row.values()))
        conn.commit()
    market_context.invalidate()
    return ts


# --------------------------------------------------------------------------- #
# Fail-open guarantees — the point of Phase 0
# --------------------------------------------------------------------------- #
def test_get_returns_neutral_when_db_missing(tmp_path, monkeypatch):
    """No DB at all (service not deployed yet) must not raise."""
    missing = tmp_path / "does_not_exist.db"
    monkeypatch.setattr(store, "DB_PATH", str(missing))
    market_context.invalidate()
    ctx = market_context.get()
    assert ctx.available is False
    assert ctx.as_of is None
    # Phase 0 must change NOTHING about a strategy container, including its
    # filesystem: the read path must not create the DB as a side effect.
    assert not missing.exists()


def test_get_returns_neutral_when_table_missing(tmp_path, monkeypatch):
    """DB file exists but has no mc_regime table."""
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    monkeypatch.setattr(store, "DB_PATH", path)
    market_context.invalidate()
    assert market_context.get().available is False


def test_get_returns_neutral_when_table_empty(db):
    assert market_context.get().available is False


def test_get_never_raises_on_corrupt_row(db):
    """A half-written / malformed row must degrade, not explode."""
    with store.connect(db) as conn:
        conn.execute(
            "INSERT INTO mc_regime (ts, axes_available, axis_inputs, reasons) "
            "VALUES (?,?,?,?)",
            (_ts(), "not-json{{", "also-not-json", None),
        )
        conn.commit()
    market_context.invalidate()
    ctx = market_context.get()          # must not raise
    assert ctx.available is False


def test_mode_off_returns_neutral(db, monkeypatch):
    _write_regime(db)
    monkeypatch.setattr(cfg, "MODE", "off")
    market_context.invalidate()
    assert market_context.get().available is False


def test_unimplemented_mode_degrades_to_observe(monkeypatch):
    """A mis-set MC_MODE must never silently start influencing trades."""
    monkeypatch.setattr(cfg, "MODE", "hard")
    assert cfg.effective_mode() == "observe"
    assert cfg.influences_trading() is False


def test_influences_trading_is_false_in_phase_1():
    """The Phase 1 rule, asserted. If this ever flips, it must be deliberate."""
    assert cfg.influences_trading() is False


# --------------------------------------------------------------------------- #
# Neutral context is inert
# --------------------------------------------------------------------------- #
def test_neutral_context_is_inert():
    ctx = NEUTRAL_CONTEXT
    assert ctx.available is False
    for name in ALL_AXES:
        axis = ctx.axis(name)
        assert axis.available is False
        assert axis.state == UNKNOWN
        assert axis.score == 0.0
        assert axis.confidence == 0.0
        # is_() must be False for every state, so a consumer that forgets to
        # check `available` still cannot be pushed into acting.
        assert axis.is_(TRENDING_UP, HIGH_VOL) is False


def test_axis_lookup_with_unknown_name_is_safe():
    """A typo in a strategy must not break a scan."""
    assert NEUTRAL_CONTEXT.axis("nonsense").available is False


# --------------------------------------------------------------------------- #
# Read path
# --------------------------------------------------------------------------- #
def test_get_reads_persisted_regime(db):
    ts = _write_regime(db)
    ctx = market_context.get()

    assert ctx.available is True
    assert ctx.as_of == ts
    assert ctx.source == "db"
    assert ctx.data_quality == pytest.approx(0.92)
    assert ctx.config_version == cfg.CONFIG_VERSION

    trend = ctx.trend
    assert trend.available is True
    assert trend.state == TRENDING_UP
    assert trend.score == pytest.approx(0.62)
    assert trend.direction == "STRENGTHENING"
    assert trend.dwell_minutes == 25
    assert trend.event == "BREAKOUT"
    assert trend.inputs["ef_ratio"] == pytest.approx(0.44)
    assert "ER 0.44 >= 0.35" in trend.reasons

    assert ctx.volatility.is_(HIGH_VOL) is True


def test_axes_not_listed_available_are_neutral(db):
    """Only axes the writer declared available may present as information."""
    _write_regime(db, axes_available=[AXIS_TREND])
    ctx = market_context.get()
    assert ctx.trend.available is True
    assert ctx.volatility.available is False
    assert ctx.volatility.state == UNKNOWN
    assert set(ctx.available_axes()) == {AXIS_TREND}


def test_stale_snapshot_reports_unavailable(db, monkeypatch):
    """Stale context is worse than none — it looks like information."""
    monkeypatch.setattr(cfg, "MAX_CONTEXT_AGE_SEC", 60.0)
    _write_regime(db, ts=_ts(minutes_ago=30))
    assert market_context.get().available is False


def test_fresh_snapshot_within_max_age(db, monkeypatch):
    monkeypatch.setattr(cfg, "MAX_CONTEXT_AGE_SEC", 3600.0)
    _write_regime(db, ts=_ts(minutes_ago=5))
    ctx = market_context.get()
    assert ctx.available is True
    assert 250 < ctx.age_seconds < 400


def test_get_uses_newest_row(db):
    _write_regime(db, ts=_ts(minutes_ago=10), trend_score=0.10)
    newest = _write_regime(db, ts=_ts(minutes_ago=1), trend_score=0.90)
    ctx = market_context.get()
    assert ctx.as_of == newest
    assert ctx.trend.score == pytest.approx(0.90)


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #
def test_cache_avoids_repeated_reads(db, monkeypatch):
    """A batch caller booking N signals must cause ONE read, not N."""
    _write_regime(db)
    monkeypatch.setattr(cfg, "GET_CACHE_TTL_SEC", 60.0)
    market_context.invalidate()

    calls = {"n": 0}
    real = store.latest_regime_row

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(store, "latest_regime_row", counting)
    for _ in range(40):
        market_context.get()
    assert calls["n"] == 1


def test_refresh_bypasses_cache(db, monkeypatch):
    _write_regime(db, trend_score=0.10)
    monkeypatch.setattr(cfg, "GET_CACHE_TTL_SEC", 60.0)
    assert market_context.get().trend.score == pytest.approx(0.10)
    _write_regime(db, ts=_ts(), trend_score=0.80)
    assert market_context.refresh().trend.score == pytest.approx(0.80)


# --------------------------------------------------------------------------- #
# Persisted shape (this becomes the historical research record)
# --------------------------------------------------------------------------- #
def test_as_dict_is_json_serialisable_and_stable(db):
    _write_regime(db)
    d = market_context.get().as_dict()

    json.dumps(d)                                   # must not raise
    assert set(d["axes"]) == set(ALL_AXES)          # all six, always
    assert d["schema_version"] == SCHEMA_VERSION
    assert d["available"] is True

    trend = d["axes"]["trend"]
    for key in ("name", "state", "score", "confidence", "direction",
                "dwell_minutes", "transition_prob", "event", "available",
                "inputs", "reasons"):
        assert key in trend


def test_as_dict_has_no_trading_decision_fields(db):
    """DESIGN RULE: the engine describes, it does not decide.

    If someone adds a bias / size / veto / exit field, this fails loudly.
    """
    _write_regime(db)
    blob = json.dumps(market_context.get().as_dict()).lower()
    for banned in ("bias", "size_multiplier", "exit_warning", "should_",
                   "veto", "allow", "aggressive", "defensive"):
        assert banned not in blob, f"trading-decision field leaked: {banned}"


def test_neutral_context_as_dict_is_serialisable():
    json.dumps(NEUTRAL_CONTEXT.as_dict())


def test_summary_is_readable(db):
    _write_regime(db)
    text = market_context.get().summary()
    assert "trend=TRENDING_UP" in text
    assert "volatility=HIGH_VOL" in text
    assert NEUTRAL_CONTEXT.summary() == "market context: UNAVAILABLE"


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def test_init_db_is_idempotent(tmp_path):
    path = str(tmp_path / "mc.db")
    store.init_db(path)
    store.init_db(path)                             # must not raise
    assert store.schema_version(path) == SCHEMA_VERSION


def test_all_tables_created(db):
    with store.connect(db) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("mc_meta", "mc_instruments", "mc_bars_1m", "mc_futures",
                  "mc_breadth", "mc_sector", "mc_vix", "mc_features",
                  "mc_regime", "mc_feed_gaps"):
        assert table in names


def test_bars_write_is_idempotent(db):
    """A restart mid-minute must not double-write a bar."""
    row = ("NSE_INDEX|Nifty 50", "2026-08-03 09:30:00", 1.0, 2.0, 0.5, 1.5,
           100.0, None, None, 1.2, None, None, None, None, 7)
    with store.connect(db) as conn:
        for _ in range(3):
            conn.execute(
                "INSERT OR IGNORE INTO mc_bars_1m (instrument_key, ts, open, high,"
                " low, close, volume, oi, oi_chg, vwap, bid, ask, bid_qty,"
                " ask_qty, tick_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM mc_bars_1m").fetchone()[0]
    assert n == 1


def test_feed_gap_open_and_close(db):
    gap_id = store.open_feed_gap(_ts(), "STALE", instruments=7, db_path=db)
    assert gap_id is not None
    store.close_feed_gap(gap_id, _ts(), 42.5, resynced=True, db_path=db)
    with store.connect(db) as conn:
        row = conn.execute("SELECT * FROM mc_feed_gaps WHERE id=?", (gap_id,)).fetchone()
    assert row["duration_sec"] == pytest.approx(42.5)
    assert row["resynced"] == 1


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_config_hash_is_stable_and_sensitive(monkeypatch):
    first = cfg.config_hash()
    assert first == cfg.config_hash()                    # stable
    monkeypatch.setattr(cfg, "VOL_HIGH_VIX_PCTILE", 80.0)
    assert cfg.config_hash() != first                    # sensitive to meaning


def test_config_hash_ignores_non_meaning_knobs(monkeypatch):
    """Changing a cache TTL must not invalidate historical classifications."""
    first = cfg.config_hash()
    monkeypatch.setattr(cfg, "GET_CACHE_TTL_SEC", 999.0)
    assert cfg.config_hash() == first


# --------------------------------------------------------------------------- #
# Subscription budget — scales with the plan, no code change (decision 1)
# --------------------------------------------------------------------------- #
def test_budget_standard_matches_operator_target(monkeypatch):
    """Standard: NIFTY + BANKNIFTY + VIX + ~50-60 liquid F&O stocks."""
    monkeypatch.setattr(cfg, "PLAN_TIER", "standard")
    b = cfg.subscription_budget()
    assert b.tier == "standard"
    assert b.total <= b.capacity
    assert 50 <= b.tier3 <= 60
    assert b.breadth_is_subsample is True


def test_budget_plus_expands_universe(monkeypatch):
    monkeypatch.setattr(cfg, "PLAN_TIER", "plus")
    b = cfg.subscription_budget()
    assert b.tier == "plus"
    assert b.tier3 > cfg.TIER3_STOCKS_STANDARD
    assert b.breadth_is_subsample is False


def test_budget_auto_detects_from_connections(monkeypatch):
    monkeypatch.setattr(cfg, "PLAN_TIER", "auto")
    assert cfg.subscription_budget(detected_connections=1).tier == "standard"
    assert cfg.subscription_budget(detected_connections=5).tier == "plus"
    # Un-probed auto must assume the safe (smaller) plan.
    assert cfg.subscription_budget(detected_connections=None).tier == "standard"


def test_budget_never_starves_mandatory_tiers(monkeypatch):
    """A tighter-than-expected cap degrades breadth, never tier 1/2."""
    monkeypatch.setattr(cfg, "PLAN_TIER", "standard")
    monkeypatch.setattr(cfg, "WS_MAX_KEYS_PER_CONNECTION", 25)
    b = cfg.subscription_budget()
    assert b.tier1 == cfg.TIER1_KEYS
    assert b.tier2 == cfg.TIER2_KEYS
    assert b.tier3 >= 0 and b.tier4 >= 0
    assert b.tier3 + b.tier4 <= max(b.capacity - b.tier1 - b.tier2, 0)


# --------------------------------------------------------------------------- #
# Integration: factors_json persistence, and NO behaviour change
# --------------------------------------------------------------------------- #
@pytest.fixture
def fast_snapshot(monkeypatch):
    """Stub the legacy breadth scan out of collect_factor_snapshot().

    Not incidental test hygiene — breadth.compute() does a FULL SCAN of
    today's rows in the 315 MB iv_history.db on every call, and
    collect_factor_snapshot() calls it once per booked trade. Left unstubbed
    these tests take minutes on a network-mapped data volume. That cost is
    exactly the defect the market-context subsystem replaces (see
    MARKET_CONTEXT_PLAN.md C3); until the migration lands, the tests stub it.
    """
    import breadth
    monkeypatch.setattr(breadth, "compute", lambda *a, **kw: None)
    return True


def test_collect_factor_snapshot_includes_market_context(db, fast_snapshot):
    import paper_trader

    blob = paper_trader.collect_factor_snapshot({"symbol": "INFY", "security_id": "1594"})
    snap = json.loads(blob)
    assert "market_context" in snap
    assert set(snap["market_context"]["axes"]) == set(ALL_AXES)


def test_collect_factor_snapshot_survives_broken_context(fast_snapshot, monkeypatch):
    """If market_context blows up, booking must continue unaffected."""
    import paper_trader

    def boom(*a, **kw):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(market_context, "get", boom)
    snap = json.loads(
        paper_trader.collect_factor_snapshot({"symbol": "INFY", "security_id": "1594"})
    )
    assert snap["market_context"] is None
    assert "score" in snap                       # the rest of the snapshot survives
