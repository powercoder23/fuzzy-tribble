# -*- coding: utf-8 -*-
"""Unit tests for the momentum alpha engine (momentum_alpha.py).

Pure/DB-only layer, so these run without a broker, without Docker and on
Windows (momentum_strategy.py itself can't be imported here — it pulls in
discount -> upstox_token_manager -> fcntl, which is POSIX-only).

    python -m pytest test_momentum_alpha.py -q
"""

import os
import sqlite3
import tempfile
from datetime import date, timedelta

import pytest

import momentum_alpha as ma
from momentum_alpha import (
    DailyUniverseRanker, IntradayRelativeStrength, MomentumConvictionScorer,
    RelativeVolume, breakout_quality, vwap_quality,
)


# --------------------------------------------------------------------------- #
# Fixtures — a throwaway iv_history.db shaped like the real one
# --------------------------------------------------------------------------- #
@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE delivery_daily (
        date TEXT, symbol TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, deliv_qty REAL, deliv_pct REAL)""")
    conn.execute("""CREATE TABLE candles_5m (
        security_id TEXT, symbol TEXT, ts DATETIME,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY (security_id, ts))""")
    conn.commit()
    conn.close()
    yield path
    # Best-effort cleanup: on Windows the WAL sidecars can linger briefly after
    # the last handle closes. The connections themselves are closed explicitly
    # by momentum_alpha._connect; this is a temp-file artifact, not a leak.
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _seed_daily(path, symbol, closes, atr_frac=0.02):
    """Write `closes` as daily bars with a proportional high/low band."""
    rows = []
    start = date(2026, 6, 1)
    for i, c in enumerate(closes):
        h = c * (1 + atr_frac)
        l = c * (1 - atr_frac)
        rows.append(((start + timedelta(days=i)).isoformat(), symbol,
                     c, h, l, c, 1000.0, 500.0, 50.0))
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO delivery_daily VALUES (?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()


def _seed_5m(path, sid, day, bars, symbol="X"):
    """bars = [(hhmm, open, high, low, close, volume)]"""
    rows = [(sid, symbol, f"{day} {hhmm}:00", o, h, l, c, v)
            for hhmm, o, h, l, c, v in bars]
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO candles_5m VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.commit()


# --------------------------------------------------------------------------- #
# Relative strength / ATR
# --------------------------------------------------------------------------- #
def test_rs_percentile_ranks_leader_above_laggard(db_path):
    _seed_daily(db_path, "LEADER", [100 + i * 2 for i in range(30)])    # +~55%
    _seed_daily(db_path, "LAGGARD", [100 - i * 1.5 for i in range(30)])  # falling
    for n in range(10):                       # filler so percentiles are meaningful
        _seed_daily(db_path, f"MID{n}", [100 + (n * 0.1) for _ in range(30)])

    ranked = DailyUniverseRanker(db_path).rank()
    assert ranked["LEADER"]["rs_pct"] > 90
    assert ranked["LAGGARD"]["rs_pct"] < 10
    assert ranked["LEADER"]["ret_pct"] > ranked["LAGGARD"]["ret_pct"]


def test_atr_pct_scales_with_band_width(db_path):
    _seed_daily(db_path, "CALM", [100.0] * 30, atr_frac=0.005)
    _seed_daily(db_path, "WILD", [100.0] * 30, atr_frac=0.05)
    ranked = DailyUniverseRanker(db_path).rank()
    assert ranked["WILD"]["atr_pct"] > ranked["CALM"]["atr_pct"]
    assert ranked["CALM"]["atr_pct"] < 2.0


def test_universe_filter_rejects_weak_rs_for_ce(db_path):
    _seed_daily(db_path, "LEADER", [100 + i * 2 for i in range(30)])
    _seed_daily(db_path, "LAGGARD", [100 - i * 1.5 for i in range(30)])
    for n in range(50):
        _seed_daily(db_path, f"MID{n}", [100 + n * 0.01 for _ in range(30)])

    r = DailyUniverseRanker(db_path)
    ok_ce, _ = r.passes_universe_filter("LAGGARD", "CE")
    ok_pe, _ = r.passes_universe_filter("LAGGARD", "PE")
    assert ok_ce is False          # weakest name is not a call candidate
    assert ok_pe is True           # ...but it is a put candidate


def test_universe_filter_fails_open_on_empty_db(db_path):
    r = DailyUniverseRanker(db_path)
    ok, reason = r.passes_universe_filter("ANYTHING", "CE")
    assert ok is True and "universe_too_small" in reason


# --------------------------------------------------------------------------- #
# RVOL
# --------------------------------------------------------------------------- #
def _session(vol):
    return [("09:15", 10, 11, 9, 10, vol), ("09:20", 10, 11, 9, 10, vol),
            ("09:25", 10, 11, 9, 10, vol)]


def test_rvol_detects_double_normal_participation(db_path):
    for d in range(1, 9):                      # 8 baseline sessions @100/bar
        _seed_5m(db_path, "1", f"2026-07-{d:02d}", _session(100))
    _seed_5m(db_path, "1", "2026-07-20", _session(200))   # today: 2x

    rv = RelativeVolume(db_path).rvol("1", "09:30", day="2026-07-20")
    assert rv == pytest.approx(2.0, rel=0.01)


def test_rvol_is_none_when_baseline_too_thin(db_path):
    _seed_5m(db_path, "1", "2026-07-01", _session(100))
    _seed_5m(db_path, "1", "2026-07-20", _session(200))
    assert RelativeVolume(db_path).rvol("1", "09:30", day="2026-07-20") is None


def test_rvol_respects_time_of_day_cutoff(db_path):
    """Volume after the cutoff must not leak into the comparison."""
    for d in range(1, 9):
        _seed_5m(db_path, "1", f"2026-07-{d:02d}",
                 _session(100) + [("14:00", 10, 11, 9, 10, 9999)])
    _seed_5m(db_path, "1", "2026-07-20", _session(100))
    rv = RelativeVolume(db_path).rvol("1", "09:30", day="2026-07-20")
    assert rv == pytest.approx(1.0, rel=0.01)


# --------------------------------------------------------------------------- #
# Intraday RS vs index
# --------------------------------------------------------------------------- #
def test_intraday_rs_is_stock_minus_index(db_path):
    day = "2026-07-20"
    # index +1%, stock +3%  ->  RS = +2pp
    _seed_5m(db_path, "13", day, [("09:15", 100, 100, 100, 100, 1),
                                  ("09:20", 100, 101, 100, 101, 1)])
    _seed_5m(db_path, "99", day, [("09:15", 100, 100, 100, 100, 1),
                                  ("09:20", 100, 103, 100, 103, 1)])
    rs = IntradayRelativeStrength(db_path).rs("99", day=day)
    assert rs == pytest.approx(2.0, abs=0.01)


def test_intraday_rs_none_without_index_data(db_path):
    _seed_5m(db_path, "99", "2026-07-20", [("09:15", 100, 100, 100, 103, 1)])
    assert IntradayRelativeStrength(db_path).rs("99", day="2026-07-20") is None


# --------------------------------------------------------------------------- #
# Trigger quality
# --------------------------------------------------------------------------- #
def _bars(spec):
    return [{"open": o, "high": h, "low": l, "close": c, "volume": v}
            for o, h, l, c, v in spec]


def test_breakout_quality_prefers_coiled_high_volume_break():
    # wide early range, tight coil, then a strong high-volume break
    good = _bars([(100, 106, 94, 100, 100), (100, 105, 95, 100, 100),
                  (100, 104, 96, 100, 100), (100, 103, 97, 100, 100),
                  (100, 101, 99, 100, 100), (100, 101, 99, 100, 100),
                  (100, 101, 99, 100, 100), (100, 101, 99, 100, 100),
                  (100, 106, 100, 105.8, 400)])
    # no coil, no volume, weak mid-range close
    bad = _bars([(100, 106, 94, 100, 100), (100, 106, 94, 100, 100),
                 (100, 106, 94, 100, 100), (100, 106, 94, 100, 100),
                 (100, 106, 94, 100, 100), (100, 106, 94, 100, 100),
                 (100, 106, 94, 100, 100), (100, 106, 94, 100, 100),
                 (100, 104, 96, 100.2, 90)])
    q_good = breakout_quality(good, 8, 100.0, "CE")
    q_bad = breakout_quality(bad, 8, 100.0, "CE")
    assert q_good > 0.6
    assert q_bad < 0.3
    assert q_good > q_bad


def test_breakout_quality_handles_degenerate_input():
    assert breakout_quality([], 0, 100.0, "CE") == 0.0
    assert breakout_quality(_bars([(1, 1, 1, 1, 1)]), 5, 1.0, "CE") == 0.0


def test_vwap_quality_rewards_slope_and_acceptance():
    rising = _bars([(100, 101, 99, 100, 100)] * 3 +
                   [(100, 103, 100, 102, 200), (102, 105, 102, 104, 200),
                    (104, 107, 104, 106, 200)])
    vwaps_up = [100, 100.2, 100.4, 100.8, 101.4, 102.0]
    flat = _bars([(100, 101, 99, 100.05, 100)] * 6)
    vwaps_flat = [100.0] * 6

    assert vwap_quality(rising, vwaps_up, "CE") > 0.6
    assert vwap_quality(flat, vwaps_flat, "CE") < 0.3


def test_vwap_quality_direction_aware():
    falling = _bars([(100, 101, 99, 100, 100)] * 3 +
                    [(100, 100, 97, 98, 200), (98, 98, 95, 96, 200),
                     (96, 96, 93, 94, 200)])
    vwaps_down = [100, 99.8, 99.6, 99.0, 98.2, 97.4]
    assert vwap_quality(falling, vwaps_down, "PE") > 0.6
    assert vwap_quality(falling, vwaps_down, "CE") < 0.3


# --------------------------------------------------------------------------- #
# Conviction scorer
# --------------------------------------------------------------------------- #
def _strong_ctx():
    return {"rs_pct": 95, "breakout_quality": 0.9, "rvol": 2.5,
            "atr_expansion": 1.4, "sector_pct": 80, "market_pct": 70,
            "regime_strength": "STRONG", "oi_bias": "CE", "oi_strength": "STRONG"}


def test_scorer_separates_strong_from_weak():
    s = MomentumConvictionScorer()
    strong = s.score(_strong_ctx(), "CE")["confidence"]
    weak = s.score({"rs_pct": 50, "breakout_quality": 0.1, "rvol": 1.0,
                    "atr_expansion": 0.9, "sector_pct": 50, "market_pct": 50,
                    "regime_strength": "WEAK"}, "CE")["confidence"]
    assert strong > 80
    assert weak < 25
    assert strong - weak > 50      # real dynamic range, unlike the old ladder


def test_scorer_is_direction_aware():
    s = MomentumConvictionScorer()
    ctx = _strong_ctx()
    assert s.score(ctx, "CE")["confidence"] > s.score(ctx, "PE")["confidence"]


def test_missing_factors_do_not_score_zero():
    """A missing factor should dilute, not silently count as maximum failure."""
    s = MomentumConvictionScorer()
    full = s.score({"rs_pct": 95, "breakout_quality": 0.9, "rvol": 2.5,
                    "atr_expansion": 1.4, "sector_pct": 80, "market_pct": 70,
                    "regime_strength": "STRONG"}, "CE")
    partial = s.score({"rs_pct": 95, "breakout_quality": 0.9}, "CE")
    assert partial["n_missing"] >= 5
    assert partial["confidence"] > 80      # normalised over available weights


def test_zero_weight_factor_is_journalled_but_powerless():
    weights = dict(ma.WEIGHTS)
    weights["relative_strength"] = 0.0
    s = MomentumConvictionScorer(weights)
    hi = s.score(dict(_strong_ctx(), rs_pct=95), "CE")["confidence"]
    lo = s.score(dict(_strong_ctx(), rs_pct=5), "CE")["confidence"]
    assert hi == lo                                  # no influence
    assert "relative_strength" in s.score(_strong_ctx(), "CE")["breakdown"]


def test_confidence_floor_blocks_and_observe_only_does_not():
    s = MomentumConvictionScorer()
    low = s.score({"rs_pct": 50, "breakout_quality": 0.0, "rvol": 1.0,
                   "regime_strength": "WEAK"}, "CE")
    ok, reason = s.passes(low)
    assert ok is False and "low_confidence" in reason

    ma.CONFIDENCE["observe_only"] = True
    try:
        ok2, reason2 = s.passes(low)
        assert ok2 is True and "observe_only" in reason2
    finally:
        ma.CONFIDENCE["observe_only"] = False


# --------------------------------------------------------------------------- #
# Breadth gate
# --------------------------------------------------------------------------- #
def test_breadth_blocks_ce_into_red_tape():
    blocked, reason = ma.breadth_blocks("CE", {"market_pct": 30.0})
    assert blocked is True and "market_breadth" in reason


def test_breadth_fails_open_without_data():
    assert ma.breadth_blocks("CE", {})[0] is False
    assert ma.breadth_blocks("CE", {"market_pct": None})[0] is False


def test_sector_breadth_needs_minimum_names():
    ctx = {"market_pct": 60.0, "sector_pct": 10.0, "sector_n": 1}
    assert ma.breadth_blocks("CE", ctx)[0] is False      # too few names to trust
    ctx["sector_n"] = 5
    assert ma.breadth_blocks("CE", ctx)[0] is True


def test_factor_note_is_compact_and_safe():
    s = MomentumConvictionScorer()
    ctx = _strong_ctx()
    note = ma.format_factor_note(s.score(ctx, "CE"), ctx)
    assert "conf=" in note and "rs=95" in note
    assert "\n" not in note and len(note) < 200
    assert ma.format_factor_note({}, {}) != None
