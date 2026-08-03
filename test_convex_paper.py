# -*- coding: utf-8 -*-
"""Tests for engine/paper.py's Convex booking math added 2026-08-03:
  * sub-strategy tagging from the trigger kind (Convex-ORB, Convex-SONAR_BAND, ...)
  * hard rupee SL cap (PAPER_MAX_LOSS_RUPEES) regardless of lot_size
  * target scaled to each stock's own 1-day expected ATM-premium move
  * book_emitted()'s daily cap counting across mixed sub-strategy tags

Uses real temp sqlite DBs for iv_history + scrip_master (the two zero-API
sources build_signal reads) rather than mocking build_signal's internals,
so this exercises the real query paths.
"""

from datetime import datetime, timedelta

from engine import config as cfg
from engine import paper
from engine.contracts import Decision, TriggerEvent, CE


def _mk_iv_db(path, security_id, spot, atm_iv, atm_strike):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE iv_history (
        security_id TEXT, data_type TEXT, timestamp TEXT,
        spot_price REAL, atm_iv REAL, atm_strike REAL)""")
    conn.execute("INSERT INTO iv_history VALUES (?,?,?,?,?,?)",
                 (security_id, "intraday", "2026-08-03 10:00:00", spot, atm_iv, atm_strike))
    conn.commit()
    conn.close()


def _mk_scrip_db(path, underlying, opt_type, expiry_iso, lot_size, strike, sec_id):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE scrip_master (
        SEM_TRADING_SYMBOL TEXT, SEM_OPTION_TYPE TEXT, SEM_EXPIRY_DATE TEXT,
        SEM_LOT_UNITS REAL, SEM_STRIKE_PRICE REAL, SEM_SMST_SECURITY_ID TEXT)""")
    conn.execute("INSERT INTO scrip_master VALUES (?,?,?,?,?,?)",
                 (f"{underlying}-{opt_type}", opt_type, expiry_iso, lot_size, strike, sec_id))
    conn.commit()
    conn.close()


def _build(tmp_path, monkeypatch, spot=1000.0, atm_iv=30.0, lot_size=500,
          expiry_days=5, kind="ORB", grade="A+"):
    import uuid
    now = datetime(2026, 8, 3, 10, 0, 0)
    uid = uuid.uuid4().hex
    # A distinct underlying symbol per call, not just per DB file: paper.py's
    # _nearest_expiry() module-level _contract_cache is keyed on
    # (underlying, opt_type, today_iso) only, not on SCRIP_MASTER_DB — reusing
    # one symbol name across calls with a fixed `now` would silently return
    # an earlier call's cached (expiry, lot_size) instead of this call's own
    # scrip_master row.
    underlying = f"TEST{uid[:10].upper()}"
    security_id = uid[10:16]
    iv_db = str(tmp_path / f"iv_{uid}.db")
    scrip_db = str(tmp_path / f"scrip_{uid}.db")
    strike = round(spot / 10) * 10
    _mk_iv_db(iv_db, security_id, spot, atm_iv, strike)
    expiry_iso = (now.date() + timedelta(days=expiry_days)).isoformat()
    _mk_scrip_db(scrip_db, underlying, "CE", expiry_iso, lot_size, strike, "OPT123")
    monkeypatch.setattr(cfg, "SCRIP_MASTER_DB", scrip_db)
    trig = TriggerEvent(kind=kind, direction=CE, quality=0.8) if kind else None
    decision = Decision(symbol=underlying, security_id=security_id, status="EMITTED",
                        direction=CE, score=80.0, grade=grade, trigger=trig,
                        breakdown={}, why="test")
    return paper.build_signal(decision, iv_db, now=now)


# --- sub-strategy tagging ----------------------------------------------------

def test_tag_includes_trigger_kind(tmp_path, monkeypatch):
    sig = _build(tmp_path, monkeypatch, kind="SONAR_BAND")
    assert sig is not None
    assert sig["strategy"] == f"{cfg.PAPER_STRATEGY_TAG}-SONAR_BAND"


def test_tag_varies_by_kind(tmp_path, monkeypatch):
    orb = _build(tmp_path, monkeypatch, kind="ORB")
    vwap = _build(tmp_path, monkeypatch, kind="VWAP")
    assert orb["strategy"] == "Convex-ORB"
    assert vwap["strategy"] == "Convex-VWAP"
    assert orb["strategy"] != vwap["strategy"]


def test_tag_falls_back_without_trigger_kind(tmp_path, monkeypatch):
    sig = _build(tmp_path, monkeypatch, kind=None)
    assert sig["strategy"] == cfg.PAPER_STRATEGY_TAG


# --- hard rupee SL cap --------------------------------------------------------

def test_sl_capped_at_max_loss_rupees_for_large_lot(tmp_path, monkeypatch):
    sig = _build(tmp_path, monkeypatch, spot=1840.0, atm_iv=35.0, lot_size=850, expiry_days=5)
    entry, sl, lot = sig["entry"], sig["sl"], sig["lot_size"]
    loss_rupees = (entry - sl) * lot
    # sl is rounded to 2 decimal places before this multiply, so a large
    # lot_size amplifies that rounding into a few rupees of slack — assert
    # the cap held (didn't blow past it) rather than an exact rupee match.
    assert loss_rupees <= cfg.PAPER_MAX_LOSS_RUPEES + 5.0, loss_rupees
    assert loss_rupees >= cfg.PAPER_MAX_LOSS_RUPEES - 5.0, loss_rupees
    # the pure-percentage SL would have been far looser than the cap here
    assert (entry - sl) < entry * cfg.PAPER_SL_PCT


def test_sl_uses_percentage_when_tighter_than_cap(tmp_path, monkeypatch):
    # small lot -> the rupee-cap distance is huge -> percentage SL wins
    sig = _build(tmp_path, monkeypatch, spot=1000.0, atm_iv=25.0, lot_size=5, expiry_days=5)
    entry, sl, lot = sig["entry"], sig["sl"], sig["lot_size"]
    expected_pct_points = entry * cfg.PAPER_SL_PCT
    assert abs((entry - sl) - expected_pct_points) < 0.05
    assert (entry - sl) * lot < cfg.PAPER_MAX_LOSS_RUPEES


# --- IV-scaled target ---------------------------------------------------------

def test_target_scales_with_stock_iv(tmp_path, monkeypatch):
    low = _build(tmp_path, monkeypatch, spot=1000.0, atm_iv=15.0, lot_size=500, expiry_days=10)
    high = _build(tmp_path, monkeypatch, spot=1000.0, atm_iv=45.0, lot_size=500, expiry_days=10)
    low_gain = low["t1"] - low["entry"]
    high_gain = high["t1"] - high["entry"]
    assert high_gain > low_gain, "a higher-IV stock must get a bigger target than a lower-IV one"


def test_target_floors_at_min_rr_for_very_low_iv(tmp_path, monkeypatch):
    sig = _build(tmp_path, monkeypatch, spot=1000.0, atm_iv=5.0, lot_size=500, expiry_days=10)
    entry, sl, target = sig["entry"], sig["sl"], sig["t1"]
    sl_points = entry - sl
    target_gain = target - entry
    assert target_gain >= sl_points * cfg.PAPER_MIN_RR - 0.05


def test_t1_and_t2_stay_equal_single_target_book(tmp_path, monkeypatch):
    sig = _build(tmp_path, monkeypatch)
    assert sig["t1"] == sig["t2"]


# --- book_emitted() daily cap across mixed sub-strategy tags ------------------

class _FakeBook:
    def __init__(self, trades):
        self._t = trades

    def all_trades(self, date):
        return self._t


def test_book_emitted_cap_counts_mixed_sub_strategies(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_MODE", "paper")
    monkeypatch.setattr(cfg, "PAPER_MAX_TRADES", 5)
    existing = ([{"strategy": "Convex-ORB"}] * 3) + ([{"strategy": "Convex-VWAP"}] * 2)
    book = _FakeBook(existing)
    result = paper.book_emitted({"emitted": []}, db_path="unused.db",
                                now=datetime(2026, 8, 3, 10, 0, 0), book=book)
    assert result["cap_left"] == 0, "5 already-booked convex legs (mixed tags) must fill a cap of 5"


def test_book_emitted_cap_leaves_room_below_cap(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_MODE", "paper")
    monkeypatch.setattr(cfg, "PAPER_MAX_TRADES", 5)
    existing = [{"strategy": "Convex-ORB"}] * 2
    book = _FakeBook(existing)
    result = paper.book_emitted({"emitted": []}, db_path="unused.db",
                                now=datetime(2026, 8, 3, 10, 0, 0), book=book)
    assert result["cap_left"] == 3


def test_book_emitted_off_mode_is_a_hard_noop(monkeypatch):
    monkeypatch.setattr(cfg, "PAPER_MODE", "off")
    result = paper.book_emitted({"emitted": []}, db_path="unused.db")
    assert result == {"mode": "off", "booked": [], "skipped": 0, "cap_left": 0}


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
