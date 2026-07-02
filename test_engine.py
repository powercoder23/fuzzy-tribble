# -*- coding: utf-8 -*-
"""Unit tests for the V2 Conviction Engine (pure logic + pipeline with fakes)."""

import os
import sqlite3
import tempfile

import pytest

from engine import config as cfg
from engine.contracts import FactorReading, TriggerEvent, CE, PE
from engine import conviction, regime, store
from engine.pipeline import EnginePipeline


# --------------------------------------------------------------------------- #
# Regime
# --------------------------------------------------------------------------- #
class TestRegime:
    def test_red_on_high_vix(self):
        r = regime.classify(vix=23.0, breadth_pct=70.0, index_slope_pct=0.2)
        assert r.posture == "RED" and r.size_mult == 0.0

    def test_red_on_blackout(self):
        r = regime.classify(vix=12.0, breadth_pct=70.0, event_blackout=True)
        assert r.posture == "RED"

    def test_green_bullish(self):
        r = regime.classify(vix=13.0, breadth_pct=65.0, index_slope_pct=0.2)
        assert r.posture == "GREEN" and r.lean == CE and r.size_mult == 1.0

    def test_amber_when_breadth_and_index_disagree(self):
        r = regime.classify(vix=13.0, breadth_pct=65.0, index_slope_pct=-0.3)
        assert r.posture == "AMBER" and r.lean is None

    def test_amber_on_elevated_vix(self):
        r = regime.classify(vix=19.0, breadth_pct=65.0, index_slope_pct=0.2)
        assert r.posture == "AMBER" and r.size_mult == 0.5

    def test_missing_inputs_degrade_to_amber(self):
        r = regime.classify(vix=None, breadth_pct=None)
        assert r.posture == "AMBER"


# --------------------------------------------------------------------------- #
# Conviction — gates
# --------------------------------------------------------------------------- #
def _factors(oi_bias=None, oi_strength=1.0, zone="FAIR", trend_bias=None):
    f = {
        "oi_flow": FactorReading("oi_flow", oi_bias, oi_strength if oi_bias else 0.0),
        "inst_flow": FactorReading("inst_flow"),
        "delivery": FactorReading("delivery"),
        "gap": FactorReading("gap"),
        "trend": FactorReading("trend", trend_bias, 0.8 if trend_bias else 0.0),
        "sector_rs": FactorReading("sector_rs"),
        "premium_value": FactorReading("premium_value",
                                       strength=1.0 if zone == "CHEAP" else 0.0,
                                       detail={"zone": zone}),
    }
    return f


def _trigger(direction=CE, quality=0.8):
    return TriggerEvent("ORB", direction, quality)


class TestGates:
    def test_expensive_iv_blocks(self):
        g = conviction.run_gates(regime.classify(13, 60, 0.2), _factors(zone="EXPENSIVE"),
                                 _trigger(CE))
        ok, why = conviction.gates_pass(g)
        assert not ok and "premium_not_expensive" in why

    def test_contradicting_factors_block(self):
        g = conviction.run_gates(regime.classify(13, 60, 0.2),
                                 _factors(oi_bias=PE, trend_bias=PE), _trigger(CE))
        ok, why = conviction.gates_pass(g)
        assert not ok and "factors_not_contradicting" in why

    def test_against_the_tape_blocks(self):
        # GREEN bullish regime, PE trigger with PE factors -> tape gate fails
        g = conviction.run_gates(regime.classify(13, 65, 0.2),
                                 _factors(oi_bias=PE), _trigger(PE))
        ok, why = conviction.gates_pass(g)
        assert not ok and "with_the_tape" in why

    def test_daily_loss_lockout(self):
        g = conviction.run_gates(regime.classify(13, 65, 0.2),
                                 _factors(oi_bias=CE), _trigger(CE),
                                 risk_state={"day_pnl_pct": -3.5})
        ok, why = conviction.gates_pass(g)
        assert not ok and "daily_loss_ok" in why

    def test_entry_cutoff(self):
        g = conviction.run_gates(regime.classify(13, 65, 0.2),
                                 _factors(oi_bias=CE), _trigger(CE), now_hhmm="14:45")
        ok, why = conviction.gates_pass(g)
        assert not ok and "entry_cutoff" in why

    def test_clean_pass(self):
        g = conviction.run_gates(regime.classify(13, 65, 0.2),
                                 _factors(oi_bias=CE), _trigger(CE), now_hhmm="10:15")
        ok, _ = conviction.gates_pass(g)
        assert ok


# --------------------------------------------------------------------------- #
# Conviction — score
# --------------------------------------------------------------------------- #
class TestScore:
    def test_full_confluence_grades_a_plus(self):
        f = _factors(oi_bias=CE, zone="CHEAP", trend_bias=CE)
        f["inst_flow"] = FactorReading("inst_flow", CE, 1.0)
        f["sector_rs"] = FactorReading("sector_rs", CE, 1.0)
        f["gap"] = FactorReading("gap", CE, 1.0)
        res = conviction.score(_trigger(CE, quality=1.0), f, regime.classify(12, 65, 0.2))
        assert res["grade"] == "A+" and res["score"] >= cfg.GRADE_A_PLUS

    def test_trigger_alone_is_rejected_grade(self):
        res = conviction.score(_trigger(CE, quality=0.6), _factors(),
                               regime.classify(13, 60, 0.2))
        assert res["grade"] is None  # "trigger without context"

    def test_opposing_factors_drag_score_down(self):
        aligned = conviction.score(_trigger(CE, 0.8), _factors(oi_bias=CE, trend_bias=CE),
                                   regime.classify(13, 60, 0.2))
        opposed = conviction.score(_trigger(CE, 0.8), _factors(oi_bias=PE, trend_bias=CE),
                                   regime.classify(13, 60, 0.2))
        assert aligned["score"] > opposed["score"]

    def test_elevated_vix_penalizes(self):
        f = _factors(oi_bias=CE, trend_bias=CE, zone="CHEAP")
        calm = conviction.score(_trigger(CE, 0.9), f, regime.classify(13, 60, 0.2))
        hot = conviction.score(_trigger(CE, 0.9), f, regime.classify(19, 60, 0.2))
        assert calm["score"] > hot["score"]

    def test_score_bounded_0_100(self):
        f = _factors(oi_bias=CE, zone="CHEAP", trend_bias=CE)
        for k in ("inst_flow", "sector_rs", "gap"):
            f[k] = FactorReading(k, CE, 1.0)
        res = conviction.score(_trigger(CE, 1.0), f, regime.classify(11, 70, 0.5))
        assert 0.0 <= res["score"] <= 100.0

    def test_context_score_symmetric(self):
        assert conviction.context_score(_factors(oi_bias=PE, trend_bias=PE)) == \
               conviction.context_score(_factors(oi_bias=CE, trend_bias=CE))


# --------------------------------------------------------------------------- #
# Pipeline + store (fakes, tmp DB)
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


def _pipeline(tmp_db, trigger_map, factor_map, regime_state=None):
    return EnginePipeline(
        tmp_db,
        regime_fn=lambda: regime_state or regime.classify(13, 65, 0.2),
        universe_fn=lambda: [("101", "TATAMOTORS"), ("102", "RELIANCE")],
        factors_fn=lambda sid, sym: factor_map.get(sid, _factors()),
        trigger_fn=lambda sid: trigger_map.get(sid),
        # Fixed clock inside the entry window — gates must not depend on wall time.
        now_fn=lambda: __import__("datetime").datetime(2026, 7, 2, 10, 15),
    )


class TestPipeline:
    def test_emit_watch_reject_split(self, tmp_db):
        factor_map = {
            "101": _factors(oi_bias=CE, zone="CHEAP", trend_bias=CE),  # -> EMITTED
            "102": _factors(oi_bias=CE, oi_strength=1.0, trend_bias=CE, zone="CHEAP"),
        }
        factor_map["101"]["inst_flow"] = FactorReading("inst_flow", CE, 1.0)
        result = _pipeline(tmp_db, {"101": _trigger(CE, 0.9)}, factor_map).run()
        assert len(result["emitted"]) == 1
        assert result["emitted"][0].symbol == "TATAMOTORS"
        assert result["emitted"][0].why  # explain-or-die
        assert len(result["watch"]) == 1  # RELIANCE: context, no trigger

    def test_red_regime_emits_nothing(self, tmp_db):
        r = regime.classify(vix=25.0, breadth_pct=65.0)
        result = _pipeline(tmp_db, {"101": _trigger(CE, 0.9)},
                           {"101": _factors(oi_bias=CE, zone="CHEAP")}, r).run()
        assert not result["emitted"] and not result["watch"]
        assert len(result["rejected"]) == 1
        assert "regime_not_red" in result["rejected"][0].reject_reason

    def test_rejects_are_journaled_with_reason(self, tmp_db):
        result = _pipeline(tmp_db, {"101": _trigger(CE, 0.9)},
                           {"101": _factors(oi_bias=PE, trend_bias=PE)}).run()
        assert result["rejected"] and result["rejected"][0].reject_reason

    def test_persistence_roundtrip(self, tmp_db):
        factor_map = {"101": _factors(oi_bias=CE, zone="CHEAP", trend_bias=CE)}
        factor_map["101"]["inst_flow"] = FactorReading("inst_flow", CE, 1.0)
        _pipeline(tmp_db, {"101": _trigger(CE, 0.9)}, factor_map).run(persist=True)
        rows = store.latest_decisions(tmp_db, status="EMITTED")
        assert rows and rows[0]["symbol"] == "TATAMOTORS"
        assert rows[0]["formula_ver"] == cfg.FORMULA_VER
        with sqlite3.connect(tmp_db) as conn:
            n = conn.execute(f"SELECT COUNT(*) FROM {cfg.REGIME_TABLE}").fetchone()[0]
        assert n == 1
