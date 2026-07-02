# -*- coding: utf-8 -*-
"""Tests for the expected-move buyer-viability gate."""

import sqlite3
import tempfile
import os

from engine import expected_move as em
from engine import conviction, regime, config as cfg
from engine.contracts import FactorReading, TriggerEvent, CE


def test_em_math():
    # IV 32% -> 1-day move = 32/sqrt(252) ~ 2.016%
    assert abs(em.em_pct(32.0) - 2.016) < 0.01
    assert em.em_pct(None) is None
    assert em.em_pct(0) is None
    # ATM premium ~ 0.4 x move
    assert abs(em.est_atm_premium_pct(32.0) - 0.806) < 0.01


def _factors():
    return {"oi_flow": FactorReading("oi_flow", CE, 1.0),
            "premium_value": FactorReading("premium_value", detail={"zone": "FAIR"})}


def test_dead_vol_name_rejected():
    g = conviction.run_gates(regime.classify(13, 65, 0.2), _factors(),
                             TriggerEvent("ORB", CE, 0.9),
                             expected_move_pct=0.4)   # IV ~6% — moves nothing
    ok, why = conviction.gates_pass(g)
    assert not ok and "moves_enough" in why and "theta outruns" in why


def test_mover_passes():
    g = conviction.run_gates(regime.classify(13, 65, 0.2), _factors(),
                             TriggerEvent("ORB", CE, 0.9),
                             expected_move_pct=1.9)
    assert conviction.gates_pass(g)[0]


def test_missing_iv_fails_open():
    g = conviction.run_gates(regime.classify(13, 65, 0.2), _factors(),
                             TriggerEvent("ORB", CE, 0.9),
                             expected_move_pct=None)
    assert "moves_enough" not in [x.name for x in g]
    assert conviction.gates_pass(g)[0]


def test_loader_reads_iv_history():
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE iv_history (security_id TEXT, symbol TEXT, "
                      "spot_price REAL, atm_iv REAL, data_type TEXT, timestamp TEXT)")
            c.execute("INSERT INTO iv_history VALUES "
                      "('101','TATAMOTORS',1080.0,30.0,'intraday','2026-07-02 10:00:00')")
        d = em.load(db, "101")
        assert d["spot"] == 1080.0 and d["atm_iv"] == 30.0
        assert abs(d["em_pct"] - 1.89) < 0.01
        assert em.load(db, "999") == {}          # unknown name -> fail-open
    finally:
        os.unlink(db)
