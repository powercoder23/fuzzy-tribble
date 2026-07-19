# -*- coding: utf-8 -*-
"""Unit tests for vol_expansion pure helpers (no broker / no live DB)."""
import os, sqlite3, tempfile
import vol_expansion_strategy as ve


def test_select_atm_option_picks_nearest_and_side():
    oc = {
        "100.0": {"ce": {"last_price": 5}, "pe": {"last_price": 6}},
        "105.0": {"ce": {"last_price": 3}, "pe": {"last_price": 8}},
        "110.0": {"ce": {"last_price": 2}, "pe": {"last_price": 11}},
    }
    # spot 106 -> ATM 105 (gap 5)
    strike, opt = ve.select_atm_option(oc, spot=106, side="CE", offset=0)
    assert strike == 105.0 and opt["last_price"] == 3
    strike_p, opt_p = ve.select_atm_option(oc, spot=106, side="PE", offset=0)
    assert strike_p == 105.0 and opt_p["last_price"] == 8


def test_select_atm_offset_moves_otm_direction():
    oc = {str(float(k)): {"ce": {}, "pe": {}} for k in (100, 105, 110, 115)}
    for k in oc:  # give ce/pe a marker so non-empty
        oc[k]["ce"] = {"last_price": 1}
        oc[k]["pe"] = {"last_price": 1}
    s_ce, _ = ve.select_atm_option(oc, spot=105, side="CE", offset=1)  # +1 gap OTM call
    s_pe, _ = ve.select_atm_option(oc, spot=105, side="PE", offset=1)  # -1 gap OTM put
    assert s_ce == 110.0 and s_pe == 100.0


def test_select_atm_empty_chain_returns_none():
    assert ve.select_atm_option({}, spot=100, side="CE") is None
    assert ve.select_atm_option({"100.0": {"ce": {}}}, spot=0, side="CE") is None


def _mk_iv_db(path, symbol, spots):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS iv_history (symbol TEXT, timestamp TEXT, spot_price REAL, data_type TEXT)")
    for i, sp in enumerate(spots):
        c.execute("INSERT INTO iv_history VALUES(?,?,?,?)",
                  (symbol, f"2026-07-{10+i:02d} 15:30:00", sp, "daily"))
    c.commit(); c.close()


def test_underlying_bias_directions(monkeypatch=None):
    d = tempfile.mkdtemp()
    db = os.path.join(d, "iv_history.db")
    # rising ~ +5% -> CE ; falling -> PE ; flat -> None
    _mk_iv_db(db, "UP", [100, 101, 103, 105])
    _mk_iv_db(db, "DOWN", [105, 103, 101, 99])
    _mk_iv_db(db, "FLAT", [100, 100.2, 99.9, 100.1])
    ve.IV_DB = db
    assert ve.underlying_bias("UP", min_move_pct=1.0) == "CE"
    assert ve.underlying_bias("DOWN", min_move_pct=1.0) == "PE"
    assert ve.underlying_bias("FLAT", min_move_pct=1.0) is None
    assert ve.underlying_bias("MISSING", min_move_pct=1.0) is None


def test_exchange_segment():
    assert ve.exchange_segment("NIFTY") == "IDX_I"
    assert ve.exchange_segment("RELIANCE") == "NSE_FNO"


def _mk_iv_db_multi(path, symbol, day_to_spots):
    """day_to_spots: {day_int: [spot, ...]} - multiple 'daily' rows/day to
    simulate polluted history. Later rows in a day = later intraday."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS iv_history (symbol TEXT, timestamp TEXT, spot_price REAL, data_type TEXT)")
    for day, spots in day_to_spots.items():
        for j, sp in enumerate(spots):
            c.execute("INSERT INTO iv_history VALUES(?,?,?,?)",
                      (symbol, f"2026-07-{day:02d} {9+j:02d}:15:00", sp, "daily"))
    c.commit(); c.close()


def test_underlying_bias_dedup_survives_polluted_history():
    """Multiple daily rows per day must NOT collapse the lookback window. True
    EOD trend is DOWN (110 -> 100 across days); intraday noise on the last day
    must not flip it to CE."""
    d = tempfile.mkdtemp()
    db = os.path.join(d, "iv_history.db")
    _mk_iv_db_multi(db, "PZ", {
        10: [110, 111, 110],   # last-of-day = 110
        11: [108, 109, 108],   # 108
        12: [104, 103, 104],   # 104
        13: [101, 99, 100],    # last-of-day = 100  (EOD trend clearly down)
    })
    ve.IV_DB = db
    assert ve.underlying_bias("PZ", min_move_pct=2.0, lookback=6) == "PE"


def _mk_composite_db(path, rows):
    """rows: list of (symbol, direction, grade, days_ago)."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS composite_history "
              "(symbol TEXT, timestamp DATETIME, direction TEXT, grade TEXT)")
    for sym, direction, grade, days_ago in rows:
        c.execute("INSERT INTO composite_history(symbol,timestamp,direction,grade) "
                  "VALUES(?, datetime('now','localtime',?), ?, ?)",
                  (sym, f"-{days_ago} days", direction, grade))
    c.commit(); c.close()


def test_composite_direction_fresh_stale_and_grade():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "iv_history.db")
    _mk_composite_db(db, [
        ("FRESH_CE", "CE", "STRONG", 1),
        ("FRESH_PE", "PE", "MODERATE", 2),
        ("STALE", "CE", "STRONG", 10),
        ("WEAKGRADE", "CE", "WEAK", 1),
    ])
    ve.IV_DB = db
    assert ve.composite_direction("FRESH_CE", max_age_days=4) == "CE"
    assert ve.composite_direction("FRESH_PE", max_age_days=4) == "PE"
    assert ve.composite_direction("STALE", max_age_days=4) is None
    assert ve.composite_direction("MISSING", max_age_days=4) is None
    assert ve.composite_direction("WEAKGRADE", max_age_days=4) is None
    assert ve.composite_direction("FRESH_PE", max_age_days=4, min_grade="STRONG") is None


def test_composite_direction_no_table_is_safe():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "iv_history.db")
    sqlite3.connect(db).close()  # empty DB, no composite_history table
    ve.IV_DB = db
    assert ve.composite_direction("ANY") is None


def test_pick_direction_prefers_composite_then_momentum():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "iv_history.db")
    _mk_composite_db(db, [("HASCMP", "PE", "STRONG", 1)])
    # HASCMP momentum trends UP (would be CE) but composite says PE -> composite wins
    _mk_iv_db(db, "HASCMP", [100, 102, 104, 106])
    _mk_iv_db(db, "NOCMP", [100, 102, 104, 106])  # only momentum -> CE
    ve.IV_DB = db
    ve.CFG.DIRECTION_SOURCE = "composite"
    ve.CFG.COMPOSITE_MAX_AGE_DAYS = 4
    ve.CFG.COMPOSITE_MIN_GRADE = "MODERATE"
    ve.CFG.COMPOSITE_FALLBACK_MOMENTUM = True
    ve.CFG.MIN_MOVE_PCT = 2.0
    ve.CFG.TREND_LOOKBACK = 6
    assert ve.pick_direction("HASCMP") == ("PE", "composite")
    assert ve.pick_direction("NOCMP") == ("CE", "momentum")
    ve.CFG.COMPOSITE_FALLBACK_MOMENTUM = False
    assert ve.pick_direction("NOCMP") == (None, "none")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
