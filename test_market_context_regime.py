# -*- coding: utf-8 -*-
"""
test_market_context_regime.py — Phase 2: estimators, axis classification,
hysteresis, and mc_regime persistence.

The estimators are checked against hand-computed values wherever a closed form
exists, because a subtly wrong volatility formula produces plausible numbers
and would silently poison every downstream classification.

The classifier tests concentrate on the properties that make a regime series
usable at all: hysteresis prevents boundary chatter, missing inputs lower
confidence instead of inventing a state, and no trading decision leaks in.
"""

import json
import math
from datetime import datetime, timedelta

import pytest

from market_context import config as cfg
from market_context import store
from market_context.contracts import (
    ALL_AXES, BREADTH_NEUTRAL, HIGH_PARTICIPATION, HIGH_VOL, LONG_BUILDUP,
    LOW_VOL, NORMAL_VOL, PANIC, POSITIVE, RANGE, SHORT_BUILDUP, STABLE,
    STRENGTHENING, STRONG_POSITIVE, TRANSITIONING, TRENDING_DOWN, TRENDING_UP,
    UNKNOWN, WEAKENING,
)
from market_context.features import estimators as est
from market_context.features.builder import FeatureVector
from market_context.regime import axes
from market_context.regime.engine import RegimeEngine
from market_context.regime.hysteresis import (
    AxisTracker, confidence_score, transition_probability,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "market_context.db")
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(cfg, "DB_PATH", path)
    store.init_db(path)
    return path


def _fv(**kw):
    fv = FeatureVector(ts="2026-08-04 10:00:00", data_quality=1.0)
    for k, v in kw.items():
        setattr(fv, k, v)
    return fv


# =========================================================================== #
# Estimators
# =========================================================================== #
def test_efficiency_ratio_perfect_trend_is_one():
    assert est.efficiency_ratio([1, 2, 3, 4, 5]) == pytest.approx(1.0)


def test_efficiency_ratio_perfect_chop_is_zero():
    assert est.efficiency_ratio([1, 2, 1, 2, 1]) == pytest.approx(0.0)


def test_efficiency_ratio_hand_computed():
    # net |10-13| = 3; path = 2 + 3 + 4 = 9  -> 1/3
    assert est.efficiency_ratio([10, 12, 9, 13]) == pytest.approx(3 / 9)


def test_efficiency_ratio_needs_three_points():
    assert est.efficiency_ratio([1, 2]) is None
    assert est.efficiency_ratio([]) is None


def test_efficiency_ratio_flat_series_is_none_not_zero():
    """A flat line has no path; 'undefined' and 'perfectly inefficient' are
    different claims and must not be conflated."""
    assert est.efficiency_ratio([5, 5, 5, 5]) is None


def test_super_smoother_tracks_and_damps():
    noisy = [10, 12, 8, 14, 6, 13, 9, 11, 10, 12, 8, 11]
    smoothed = est.super_smoother(noisy, 10)
    assert len(smoothed) == len(noisy)
    # The filter must reduce bar-to-bar variation.
    raw_var = sum(abs(noisy[i] - noisy[i - 1]) for i in range(2, len(noisy)))
    sm_var = sum(abs(smoothed[i] - smoothed[i - 1]) for i in range(2, len(smoothed)))
    assert sm_var < raw_var


def test_yang_zhang_rises_with_volatility():
    calm = [(100, 100.2, 99.8, 100.0)] * 30
    wild = [(100, 105.0, 95.0, 101.0), (101, 108.0, 96.0, 98.0),
            (98, 104.0, 92.0, 103.0)] * 10
    calm_v = est.yang_zhang_vol(calm, cfg.VOL_ANNUALISATION_MINUTES)
    wild_v = est.yang_zhang_vol(wild, cfg.VOL_ANNUALISATION_MINUTES)
    assert calm_v is not None and wild_v is not None
    assert wild_v > calm_v * 5


def test_yang_zhang_needs_minimum_bars_and_rejects_bad_ohlc():
    assert est.yang_zhang_variance([(100, 101, 99, 100)]) is None
    assert est.yang_zhang_variance([(0, 0, 0, 0)] * 10) is None
    assert est.yang_zhang_variance(None) is None


def test_yang_zhang_captures_gap_risk_close_to_close_would_miss():
    """The overnight/gap term is the reason Yang-Zhang was chosen: two series
    with identical closes but different gap behaviour must NOT score the
    same."""
    smooth = [(100 + i, 100.5 + i, 99.5 + i, 100 + i) for i in range(30)]
    gappy = [(100 + i + (3 if i % 2 else -3), 100.5 + i + 3,
              99.5 + i - 3, 100 + i) for i in range(30)]
    assert est.yang_zhang_variance(gappy) > est.yang_zhang_variance(smooth)


def test_percentile_of_hand_computed():
    assert est.percentile_of(15, [10, 12, 14, 20, 25]) == pytest.approx(60.0)
    assert est.percentile_of(5, [10, 20]) == pytest.approx(0.0)
    assert est.percentile_of(None, [1, 2, 3]) is None
    assert est.percentile_of(5, [10]) is None       # too little history


def test_variance_risk_premium_sign():
    assert est.variance_risk_premium(20.0, 15.0) == pytest.approx(400 - 225)
    assert est.variance_risk_premium(10.0, 15.0) < 0
    assert est.variance_risk_premium(None, 15.0) is None


def test_weighted_score_ignores_missing_and_reports_coverage():
    parts = {"a": 1.0, "b": None, "c": -1.0}
    weights = {"a": 0.5, "b": 0.3, "c": 0.2}
    score, coverage = est.weighted_score(parts, weights)
    # (1.0*0.5 + -1.0*0.2) / 0.7
    assert score == pytest.approx((0.5 - 0.2) / 0.7)
    assert coverage == pytest.approx(0.7)


def test_weighted_score_missing_input_is_not_treated_as_zero():
    """Treating a missing input as 0 would drag every score toward the middle
    exactly when data is thinnest."""
    full, _ = est.weighted_score({"a": 1.0, "b": 1.0}, {"a": 0.5, "b": 0.5})
    partial, coverage = est.weighted_score({"a": 1.0, "b": None},
                                           {"a": 0.5, "b": 0.5})
    assert full == pytest.approx(1.0)
    assert partial == pytest.approx(1.0)        # not 0.5
    assert coverage == pytest.approx(0.5)


def test_weighted_score_all_missing():
    assert est.weighted_score({"a": None}, {"a": 1.0}) == (None, 0.0)


def test_vwap_position_is_range_scaled_and_clipped():
    assert est.vwap_position(105, 100, 110, 90) == pytest.approx(0.25)
    assert est.vwap_position(200, 100, 110, 90) == pytest.approx(1.0)
    assert est.vwap_position(105, 100, 100, 100) is None


# =========================================================================== #
# Axis classification
# =========================================================================== #
def test_trend_up_requires_efficiency_and_score():
    fv = _fv(ef_ratio=0.8, ss_slope_pct=0.6, mom_z=2.0, vwap_position=0.8)
    res = axes.classify_trend(fv)
    assert res.state == TRENDING_UP
    assert res.score > 0


def test_trend_down_is_mirrored():
    fv = _fv(ef_ratio=0.8, ss_slope_pct=-0.6, mom_z=-2.0, vwap_position=-0.8)
    assert axes.classify_trend(fv).state == TRENDING_DOWN


def test_inefficient_path_is_range_even_with_a_big_move():
    """The whole point of the efficiency ratio: a large move achieved by
    thrashing is not a trend."""
    fv = _fv(ef_ratio=0.05, ss_slope_pct=0.9, mom_z=3.0, vwap_position=0.9)
    res = axes.classify_trend(fv)
    assert res.state == RANGE
    assert "inefficient" in " ".join(res.reasons)


def test_trend_hysteresis_band_is_asymmetric():
    """Entering a trend needs ER >= 0.35; leaving needs ER < 0.20. An ER of
    0.25 therefore means RANGE from cold but HOLDS a trend."""
    fv = _fv(ef_ratio=0.25, ss_slope_pct=0.6, mom_z=2.0, vwap_position=0.8)
    assert axes.classify_trend(fv, current=UNKNOWN).state == RANGE
    assert axes.classify_trend(fv, current=TRENDING_UP).state == TRENDING_UP


def test_trend_without_inputs_is_unknown():
    res = axes.classify_trend(_fv())
    assert res.state == UNKNOWN and res.score is None


def test_volatility_states_across_the_percentile_range():
    assert axes.classify_volatility(_fv(vix_percentile=97.0)).state == PANIC
    assert axes.classify_volatility(_fv(vix_percentile=80.0)).state == HIGH_VOL
    assert axes.classify_volatility(_fv(vix_percentile=50.0)).state == NORMAL_VOL
    assert axes.classify_volatility(_fv(vix_percentile=10.0)).state == LOW_VOL


def test_volatility_hysteresis_holds_high_between_the_bands():
    """65th percentile: not high enough to ENTER HIGH_VOL (75), not low enough
    to LEAVE it (60)."""
    fv = _fv(vix_percentile=65.0)
    assert axes.classify_volatility(fv, current=UNKNOWN).state == NORMAL_VOL
    assert axes.classify_volatility(fv, current=HIGH_VOL).state == HIGH_VOL


def test_panic_can_fire_on_realized_vol_alone():
    """A realized-vol explosion is panic even if VIX has not caught up."""
    res = axes.classify_volatility(_fv(rv_ratio=2.5))
    assert res.state == PANIC


def test_volatility_falls_back_to_rv_ratio_without_a_vix_baseline():
    """Early in the platform's life vix_daily may be too short to rank."""
    res = axes.classify_volatility(_fv(rv_ratio=1.4))
    assert res.state == HIGH_VOL
    assert "no VIX baseline" in " ".join(res.reasons)


def test_positioning_uses_futures_quadrants():
    res = axes.classify_positioning(
        _fv(nifty_quadrant=LONG_BUILDUP, banknifty_quadrant=LONG_BUILDUP))
    assert res.state == LONG_BUILDUP
    assert res.score == pytest.approx(1.0)


def test_positioning_disagreement_lowers_magnitude_not_the_label():
    res = axes.classify_positioning(
        _fv(nifty_quadrant=LONG_BUILDUP, banknifty_quadrant=SHORT_BUILDUP))
    assert res.state == LONG_BUILDUP          # NIFTY is the dominant weight
    assert abs(res.score) < 0.5               # but conviction is diluted


def test_short_covering_scores_below_long_buildup():
    """A rally on short covering is exhausted buying, not fresh conviction."""
    buildup = axes.classify_positioning(_fv(nifty_quadrant=LONG_BUILDUP)).score
    covering = axes.classify_positioning(_fv(nifty_quadrant="SHORT_COVERING")).score
    assert 0 < covering < buildup


def test_breadth_states():
    assert axes.classify_breadth(_fv(adv_dec_pct=80.0)).state == STRONG_POSITIVE
    assert axes.classify_breadth(_fv(adv_dec_pct=60.0)).state == POSITIVE
    assert axes.classify_breadth(_fv(adv_dec_pct=50.0)).state == BREADTH_NEUTRAL
    assert axes.classify_breadth(_fv(adv_dec_pct=20.0)).state == "STRONG_NEGATIVE"


def test_breadth_flags_subsample_in_reasons():
    res = axes.classify_breadth(_fv(adv_dec_pct=60.0, breadth_is_subsample=True))
    assert "subsample" in " ".join(res.reasons)


def test_participation_needs_a_baseline():
    res = axes.classify_participation(_fv())
    assert res.state == UNKNOWN
    assert "baseline" in " ".join(res.reasons)
    assert axes.classify_participation(_fv(volume_ratio=1.8)).state == HIGH_PARTICIPATION
    assert axes.classify_participation(_fv(volume_ratio=0.4)).state == "LOW_PARTICIPATION"


def test_liquidity_states_from_spread_percentile():
    assert axes.classify_liquidity(_fv(spread_pctile=95.0)).state == "ILLIQUID"
    assert axes.classify_liquidity(_fv(spread_pctile=80.0)).state == "THIN"
    assert axes.classify_liquidity(_fv(spread_pctile=10.0)).state == "LIQUID"
    assert axes.classify_liquidity(_fv(spread_pctile=50.0)).state == "NORMAL_LIQUIDITY"


def test_liquidity_without_history_is_unknown():
    assert axes.classify_liquidity(_fv()).state == UNKNOWN


def test_breakout_event_needs_trend_and_location():
    fv = _fv(ef_ratio=0.8, ss_slope_pct=0.6, mom_z=2.0, vwap_position=0.8,
             orb_state="ABOVE", prior_day_state="ABOVE")
    assert axes.classify_trend(fv).event == "BREAKOUT"


def test_reversal_needs_three_confirmations():
    two = _fv(ef_ratio=0.8, ss_slope_pct=0.6, breadth_divergence=1.0,
              range_position=0.95)
    assert axes.classify_trend(two).event != "REVERSAL"
    three = _fv(ef_ratio=0.8, ss_slope_pct=0.6, breadth_divergence=1.0,
                range_position=0.95, thrust=-25.0)
    assert axes.classify_trend(three).event == "REVERSAL"


# =========================================================================== #
# Hysteresis / dwell / direction
# =========================================================================== #
def test_first_observation_is_adopted_immediately():
    t = AxisTracker(name="trend")
    obs = t.update(TRENDING_UP, 0.5, datetime(2026, 8, 4, 10, 0))
    assert obs.state == TRENDING_UP and obs.transitioned is True


def test_single_contrary_reading_does_not_flip_the_state():
    """One noisy snapshot must not whipsaw every consumer downstream."""
    t = AxisTracker(name="trend")
    base = datetime(2026, 8, 4, 10, 0)
    t.update(TRENDING_UP, 0.5, base)
    obs = t.update(TRENDING_DOWN, -0.5, base + timedelta(minutes=1))
    assert obs.state == TRANSITIONING
    assert t.state == TRENDING_UP


def test_flip_requires_confirmation_and_dwell(monkeypatch):
    monkeypatch.setattr(cfg, "CONFIRMATION_COUNT", 2)
    monkeypatch.setattr(cfg, "MIN_DWELL_MINUTES", 5)
    t = AxisTracker(name="trend")
    base = datetime(2026, 8, 4, 10, 0)
    t.update(TRENDING_UP, 0.5, base)
    # Confirmed twice, but only 2 minutes of dwell in the old state.
    t.update(TRENDING_DOWN, -0.5, base + timedelta(minutes=1))
    obs = t.update(TRENDING_DOWN, -0.5, base + timedelta(minutes=2))
    assert obs.state == TRENDING_UP          # dwell not satisfied
    # Past the dwell floor it flips.
    obs = t.update(TRENDING_DOWN, -0.5, base + timedelta(minutes=6))
    assert obs.state == TRENDING_DOWN and obs.transitioned is True


def test_unknown_reading_holds_the_last_state():
    t = AxisTracker(name="volatility")
    base = datetime(2026, 8, 4, 10, 0)
    t.update(HIGH_VOL, 0.8, base)
    obs = t.update(UNKNOWN, None, base + timedelta(minutes=1))
    assert obs.state == HIGH_VOL


def test_direction_is_relative_to_the_state_sign():
    """A falling score in a DOWN state is the trend strengthening, not
    weakening."""
    t = AxisTracker(name="trend")
    base = datetime(2026, 8, 4, 10, 0)
    for i, score in enumerate([-0.4, -0.6, -0.9]):
        obs = t.update(TRENDING_DOWN, score, base + timedelta(minutes=i * 2))
    assert obs.direction == STRENGTHENING

    t2 = AxisTracker(name="trend")
    for i, score in enumerate([0.9, 0.6, 0.35]):
        obs2 = t2.update(TRENDING_UP, score, base + timedelta(minutes=i * 2))
    assert obs2.direction == WEAKENING


def test_flat_score_reads_stable():
    t = AxisTracker(name="trend")
    base = datetime(2026, 8, 4, 10, 0)
    for i in range(3):
        obs = t.update(TRENDING_UP, 0.5, base + timedelta(minutes=i * 2))
    assert obs.direction == STABLE


def test_dwell_accumulates_while_the_state_holds():
    t = AxisTracker(name="volatility")
    base = datetime(2026, 8, 4, 10, 0)
    t.update(HIGH_VOL, 0.8, base)
    obs = t.update(HIGH_VOL, 0.8, base + timedelta(minutes=17))
    assert obs.dwell_minutes == 17


# =========================================================================== #
# Confidence
# =========================================================================== #
def test_confidence_rises_with_agreement_and_dwell():
    low = confidence_score(agreement=0.3, data_quality=0.5, dwell_minutes=0,
                           boundary_margin=0.1)
    high = confidence_score(agreement=1.0, data_quality=1.0, dwell_minutes=60,
                            boundary_margin=1.0)
    assert 0.0 <= low < high <= 1.0


def test_thin_data_caps_confidence():
    thin = confidence_score(agreement=1.0, data_quality=0.1, dwell_minutes=60,
                            boundary_margin=1.0)
    rich = confidence_score(agreement=1.0, data_quality=1.0, dwell_minutes=60,
                            boundary_margin=1.0)
    assert thin < rich


def test_boundary_margin_matters():
    """A score of 0.301 against a 0.300 threshold is a coin flip and must not
    read as confident."""
    edge = confidence_score(agreement=1.0, data_quality=1.0, dwell_minutes=60,
                            boundary_margin=0.0)
    clear = confidence_score(agreement=1.0, data_quality=1.0, dwell_minutes=60,
                             boundary_margin=1.0)
    assert edge < clear


def test_subsample_ceiling_applies():
    capped = confidence_score(agreement=1.0, data_quality=1.0, dwell_minutes=60,
                              boundary_margin=1.0, subsample=True)
    assert capped == pytest.approx(cfg.CONF_SUBSAMPLE_CEILING)


def test_transition_probability_bounds_and_direction():
    calm = transition_probability(momentum=0.0, boundary_margin=1.0, confidence=1.0)
    edgy = transition_probability(momentum=0.9, boundary_margin=0.0, confidence=0.1)
    assert 0.0 <= calm < edgy <= 1.0


# =========================================================================== #
# Engine
# =========================================================================== #
def test_engine_classifies_and_persists(db):
    engine = RegimeEngine(db_path=db)
    fv = _fv(ef_ratio=0.8, ss_slope_pct=0.6, mom_z=2.0, vwap_position=0.7,
             vix_percentile=80.0, rv_ratio=1.3, rv_yz_short=18.0,
             adv_dec_pct=72.0, volume_breadth_pct=68.0,
             nifty_quadrant=LONG_BUILDUP, spread_pctile=20.0,
             volume_ratio=1.5)
    result = engine.classify(fv, datetime(2026, 8, 4, 10, 0))
    assert engine.persist(result) is True

    assert result["axes"]["trend"]["state"] == TRENDING_UP
    assert result["axes"]["volatility"]["state"] == HIGH_VOL
    assert result["axes"]["breadth"]["state"] == STRONG_POSITIVE
    assert result["axes"]["positioning"]["state"] == LONG_BUILDUP
    assert set(result["axes_available"]) == set(ALL_AXES)

    with store.connect(db) as conn:
        row = dict(conn.execute("SELECT * FROM mc_regime").fetchone())
    assert row["trend_state"] == TRENDING_UP
    assert row["volatility_state"] == HIGH_VOL
    assert json.loads(row["axes_available"])
    assert json.loads(row["axis_inputs"])["trend"]


def test_engine_axes_are_independent(db):
    """TRENDING_UP + PANIC + SHORT_COVERING is a real, distinct tape. A single
    composite label could not express it — which is why there isn't one."""
    engine = RegimeEngine(db_path=db)
    fv = _fv(ef_ratio=0.9, ss_slope_pct=0.8, mom_z=2.5, vwap_position=0.9,
             vix_percentile=98.0, nifty_quadrant="SHORT_COVERING")
    result = engine.classify(fv, datetime(2026, 8, 4, 10, 0))
    assert result["axes"]["trend"]["state"] == TRENDING_UP
    assert result["axes"]["volatility"]["state"] == PANIC
    assert result["axes"]["positioning"]["state"] == "SHORT_COVERING"


def test_engine_output_has_no_composite_or_decision_fields(db):
    """DESIGN RULE: the engine describes, it does not decide."""
    engine = RegimeEngine(db_path=db)
    result = engine.classify(_fv(ef_ratio=0.8, ss_slope_pct=0.5), datetime.now())
    blob = json.dumps(result).lower()
    for banned in ("composite", "bias", "size_multiplier", "exit_warning",
                   "should_", "veto", "aggressive", "defensive"):
        assert banned not in blob, f"decision field leaked: {banned}"


def test_engine_survives_an_empty_feature_vector(db):
    engine = RegimeEngine(db_path=db)
    result = engine.classify(FeatureVector(ts="2026-08-04 10:00:00"), datetime.now())
    assert result["axes_available"] == []
    for name in ALL_AXES:
        assert result["axes"][name]["state"] == UNKNOWN
    assert engine.persist(result) is True


def test_engine_warm_start_ignores_a_prior_session(db):
    engine = RegimeEngine(db_path=db)
    engine.classify(_fv(vix_percentile=80.0), datetime(2026, 8, 3, 15, 0))
    engine.persist(engine.classify(_fv(vix_percentile=80.0),
                                   datetime(2026, 8, 3, 15, 0)))
    fresh = RegimeEngine(db_path=db)
    assert fresh.warm_start(datetime(2026, 8, 4, 9, 20)) is False
    assert fresh.trackers["volatility"].state == UNKNOWN


def test_engine_warm_start_adopts_same_session(db):
    now = datetime(2026, 8, 4, 10, 0)
    engine = RegimeEngine(db_path=db)
    engine.persist(engine.classify(_fv(vix_percentile=80.0), now))
    fresh = RegimeEngine(db_path=db)
    assert fresh.warm_start(now + timedelta(minutes=5)) is True
    assert fresh.trackers["volatility"].state == HIGH_VOL


# =========================================================================== #
# Builder integration — exercises the DB read paths the unit tests bypass
# =========================================================================== #
def _seed_bars(db, key, n=140, start_price=24000.0, drift=1.5, spread=2.0):
    """Write a synthetic trending session into mc_bars_1m."""
    base = datetime(2026, 8, 4, 9, 15)
    rows = []
    price = start_price
    vtt = 0.0
    for i in range(n):
        o = price
        c = price + drift
        h = max(o, c) + 1.0
        l = min(o, c) - 1.0
        vtt += 1000.0
        ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:00")
        rows.append((key, ts, o, h, l, c, 1000.0, None, None, (o + c) / 2,
                     c - spread / 2, c + spread / 2, 50.0, 40.0, 12))
        price = c
    with store.connect(db) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO mc_bars_1m (instrument_key, ts, open, high,"
            " low, close, volume, oi, oi_chg, vwap, bid, ask, bid_qty, ask_qty,"
            " tick_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    return rows[-1]


def test_builder_reads_bars_and_produces_features(db):
    from market_context.features import builder
    from market_context import instruments as inst

    _seed_bars(db, inst.NIFTY_INDEX)
    fv = builder.build(cache=None, now=datetime(2026, 8, 4, 11, 40), db_path=db)

    assert fv.ef_ratio is not None and fv.ef_ratio > 0.9   # clean uptrend
    assert fv.ss_slope_pct is not None and fv.ss_slope_pct > 0
    assert fv.rv_yz_short is not None and fv.rv_yz_short > 0
    assert fv.rv_yz_long is not None
    assert fv.rv_ratio is not None
    assert fv.spread_pctile is not None
    assert fv.range_position is not None
    assert fv.data_quality > 0.0
    assert builder.persist(fv, db) is True


def test_builder_end_to_end_produces_a_readable_context(db, monkeypatch):
    """Bars -> features -> regime -> market_context.get(), the whole chain.

    The seeded bars use a fixed historical timestamp, so the freshness gate
    must be widened here: this test is about the chain, not staleness (which
    test_market_context.py covers separately).
    """
    import market_context
    from market_context import instruments as inst

    monkeypatch.setattr(cfg, "MAX_CONTEXT_AGE_SEC", 10 ** 9)
    _seed_bars(db, inst.NIFTY_INDEX)
    with store.connect(db) as conn:
        conn.execute(
            "INSERT INTO mc_breadth (ts, universe_size, advancing, declining,"
            " unchanged, adv_dec_pct, is_subsample, sample_quality)"
            " VALUES ('2026-08-04 11:39:00', 59, 40, 10, 9, 80.0, 1, 0.95)")
        conn.execute(
            "INSERT INTO mc_futures (ts, instrument_key, symbol, quadrant,"
            " basis_annualised) VALUES ('2026-08-04 11:39:00','NSE_FO|1',"
            " 'NIFTY FUT 25 AUG 26', 'LONG_BUILDUP', 4.2)")
        conn.commit()

    engine = RegimeEngine(db_path=db)
    result = engine.run(cache=None, now=datetime(2026, 8, 4, 11, 40))
    assert result is not None
    market_context.invalidate()

    ctx = market_context.get()
    assert ctx.available is True
    assert ctx.trend.state == TRENDING_UP
    assert ctx.breadth.state == STRONG_POSITIVE
    assert ctx.positioning.state == LONG_BUILDUP
    # A partial-universe breadth reading must not report full confidence.
    assert ctx.breadth.confidence <= cfg.CONF_SUBSAMPLE_CEILING


def test_builder_with_no_data_is_all_none(db):
    from market_context.features import builder
    fv = builder.build(cache=None, now=datetime(2026, 8, 4, 10, 0), db_path=db)
    assert fv.ef_ratio is None and fv.rv_yz_short is None
    assert fv.data_quality == 0.0
    assert len(fv.missing_inputs) > 0


def test_recent_feed_gap_suppresses_data_quality(db):
    from market_context.features import builder
    from market_context import instruments as inst

    _seed_bars(db, inst.NIFTY_INDEX)
    clean = builder.build(cache=None, now=datetime(2026, 8, 4, 11, 40), db_path=db)

    gap_id = store.open_feed_gap(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "STALE", 7, db_path=db)
    store.close_feed_gap(gap_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         600.0, resynced=False, db_path=db)
    degraded = builder.build(cache=None, now=datetime(2026, 8, 4, 11, 40), db_path=db)
    assert degraded.data_quality < clean.data_quality


def test_regime_row_is_readable_by_get(db, monkeypatch):
    """End-to-end: what the engine writes must be what market_context.get()
    reads — the two are separated by a schema and could drift."""
    import market_context

    engine = RegimeEngine(db_path=db)
    result = engine.classify(
        _fv(ef_ratio=0.8, ss_slope_pct=0.6, mom_z=2.0, vwap_position=0.7,
            vix_percentile=80.0, adv_dec_pct=72.0, nifty_quadrant=LONG_BUILDUP),
        datetime.now())
    engine.persist(result)
    market_context.invalidate()

    ctx = market_context.get()
    assert ctx.available is True
    assert ctx.trend.state == TRENDING_UP
    assert ctx.volatility.state == HIGH_VOL
    assert ctx.positioning.state == LONG_BUILDUP
    assert ctx.trend.inputs                      # axis_inputs round-tripped
    assert ctx.trend.reasons
