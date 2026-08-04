# -*- coding: utf-8 -*-
"""
test_market_context_feed.py — Phase 1 tests: instrument resolution, tiered
subscription, frame normalisation, bar aggregation, and the collectors.

Phase 1 is observational, so these tests concentrate on the properties where a
silent error would poison the historical record we are building:

  * cumulative volume must become a DELTA, and must be NULL (never 0) when the
    baseline is unknown;
  * the futures quadrant must respect its deadbands;
  * capacity pressure must degrade breadth, never starve tier 1;
  * a malformed frame must be dropped, not crash the socket thread.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from market_context import config as cfg
from market_context import instruments as inst
from market_context import store
from market_context.collect import snapshots
from market_context.contracts import (
    LONG_BUILDUP, LONG_LIQUIDATION, POSITIONING_NEUTRAL, SHORT_BUILDUP,
    SHORT_COVERING, UNKNOWN,
)
from market_context.feed import subscription
from market_context.feed.cache import TickCache, floor_minute
from market_context.feed.normaliser import Tick, normalise_instrument, normalise_message


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "market_context.db")
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(cfg, "DB_PATH", path)
    store.init_db(path)
    return path


@pytest.fixture
def instrument_db(tmp_path):
    """Minimal stand-in for data/complete.db."""
    path = str(tmp_path / "complete.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE instruments (
            instrument_key TEXT, name TEXT, trading_symbol TEXT, segment TEXT,
            instrument_type TEXT, expiry INTEGER, lot_size INTEGER,
            underlying_symbol TEXT, isin TEXT
        )""")
    # expiry stored as epoch MILLISECONDS, as in the real master
    aug = int(datetime(2026, 8, 25).timestamp() * 1000)
    sep = int(datetime(2026, 9, 29).timestamp() * 1000)
    old = int(datetime(2026, 7, 28).timestamp() * 1000)
    rows = [
        ("NSE_INDEX|Nifty 50", "Nifty 50", "NIFTY", "NSE_INDEX", "INDEX", None, None, None, None),
        ("NSE_INDEX|Nifty Bank", "Nifty Bank", "BANKNIFTY", "NSE_INDEX", "INDEX", None, None, None, None),
        ("NSE_INDEX|India VIX", "India VIX", "INDIA VIX", "NSE_INDEX", "INDEX", None, None, None, None),
        ("NSE_INDEX|Nifty IT", "Nifty IT", "NIFTY IT", "NSE_INDEX", "INDEX", None, None, None, None),
        ("NSE_FO|OLD", "NIFTY FUT 28 JUL 26", None, "NSE_FO", "FUT", old, 65, "NIFTY", None),
        ("NSE_FO|58072", "NIFTY FUT 25 AUG 26", None, "NSE_FO", "FUT", aug, 65, "NIFTY", None),
        ("NSE_FO|68407", "NIFTY FUT 29 SEP 26", None, "NSE_FO", "FUT", sep, 65, "NIFTY", None),
        ("NSE_FO|58245", "INFY FUT 25 AUG 26", None, "NSE_FO", "FUT", aug, 400, "INFY", None),
        ("NSE_EQ|INE009A01021", "Infosys", "INFY", "NSE_EQ", "EQ", None, None, None, "INE009A01021"),
        ("NSE_EQ|INE002A01018", "Reliance", "RELIANCE", "NSE_EQ", "EQ", None, None, None, "INE002A01018"),
    ]
    # trading_symbol doubles as the FUT display name in the real master
    fixed = []
    for r in rows:
        key, name, sym, seg, itype, exp, lot, und, isin = r
        fixed.append((key, name, sym or name, seg, itype, exp, lot, und, isin))
    conn.executemany("INSERT INTO instruments VALUES (?,?,?,?,?,?,?,?,?)", fixed)
    conn.commit()
    conn.close()
    return path


def _tick(key, ltp=None, **kw):
    return Tick(instrument_key=key, received_at=kw.pop("at", datetime.now()),
                ltp=ltp, **kw)


# --------------------------------------------------------------------------- #
# Instrument resolution
# --------------------------------------------------------------------------- #
def test_resolve_indices_skips_unknown_keys(instrument_db):
    found = inst.resolve_indices(
        ["NSE_INDEX|Nifty 50", "NSE_INDEX|Does Not Exist"], db_path=instrument_db)
    assert [i.instrument_key for i in found] == ["NSE_INDEX|Nifty 50"]


def test_vix_is_classified_as_its_own_kind(instrument_db):
    found = inst.resolve_indices([inst.INDIA_VIX], db_path=instrument_db)
    assert found[0].kind == "vix"


def test_futures_chain_is_near_first_and_skips_expired(instrument_db):
    chain = inst.futures_chain("NIFTY", count=3, on_date=datetime(2026, 8, 4).date(),
                               db_path=instrument_db)
    assert [i.instrument_key for i in chain] == ["NSE_FO|58072", "NSE_FO|68407"]
    # epoch MILLISECONDS must decode to the right calendar date, not 1970
    assert chain[0].expiry == "2026-08-25"
    assert chain[0].lot_size == 65


def test_missing_instrument_master_is_not_fatal(tmp_path):
    assert inst.futures_chain("NIFTY", db_path=str(tmp_path / "nope.db")) == []
    assert inst.resolve_indices(["NSE_INDEX|Nifty 50"], db_path=str(tmp_path / "nope.db")) == []


def test_liquid_symbols_excludes_indices(tmp_path):
    """REGRESSION: NIFTY/BANKNIFTY appear in iv_history as if they were stocks
    and rank top-2 by turnover, silently consuming breadth ranking slots."""
    path = str(tmp_path / "iv.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE iv_history (symbol TEXT, timestamp TEXT,
        data_type TEXT, spot_price REAL, total_call_volume REAL,
        total_put_volume REAL, total_call_oi REAL, total_put_oi REAL)""")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.executemany(
        "INSERT INTO iv_history VALUES (?,?,'intraday',?,?,?,?,?)",
        [("NIFTY", now, 24000.0, 1e6, 1e6, 1e6, 1e6),
         ("BANKNIFTY", now, 57000.0, 1e6, 1e6, 1e6, 1e6),
         ("RELIANCE", now, 1319.0, 5e5, 5e5, 5e5, 5e5),
         ("INFY", now, 1185.0, 4e5, 4e5, 4e5, 4e5)])
    conn.commit()
    conn.close()
    assert inst.liquid_symbols(3, iv_db=path) == ["RELIANCE", "INFY"]


def test_turnover_metric_outranks_share_count_for_pricey_names(tmp_path):
    """Rupee turnover must not be dominated by low-priced, high-share names."""
    path = str(tmp_path / "iv.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE iv_history (symbol TEXT, timestamp TEXT,
        data_type TEXT, spot_price REAL, total_call_volume REAL,
        total_put_volume REAL, total_call_oi REAL, total_put_oi REAL)""")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.executemany(
        "INSERT INTO iv_history VALUES (?,?,'intraday',?,?,?,?,?)",
        # PENNY: huge share counts, tiny price. BLUE: fewer shares, high price.
        [("PENNY", now, 13.0, 1e7, 1e7, 1e8, 1e8),
         ("BLUE", now, 1319.0, 5e5, 5e5, 5e5, 5e5)])
    conn.commit()
    conn.close()
    assert inst.liquid_symbols(2, iv_db=path, metric="turnover")[0] == "BLUE"
    assert inst.liquid_symbols(2, iv_db=path, metric="shares")[0] == "PENNY"


def test_equity_keys_preserve_input_order(instrument_db):
    found = inst.equity_keys(["RELIANCE", "INFY", "NOTLISTED"], db_path=instrument_db)
    assert [i.symbol for i in found] == ["RELIANCE", "INFY"]


# --------------------------------------------------------------------------- #
# Subscription planning
# --------------------------------------------------------------------------- #
def test_plan_tier1_is_indices_plus_index_futures(instrument_db, monkeypatch, db):
    monkeypatch.setattr(inst, "liquid_symbols", lambda *a, **kw: [])
    plan = subscription.build_plan(detected_connections=1, instrument_db=instrument_db)
    kinds = sorted({i.kind for i in plan.by_tier[1]})
    assert "futures" in kinds and "vix" in kinds and "index" in kinds
    assert inst.INDIA_VIX in plan.tier1_keys()


def test_plan_modes_are_full_for_tier1_and_ltpc_for_sectors(instrument_db, monkeypatch, db):
    monkeypatch.setattr(inst, "liquid_symbols", lambda *a, **kw: [])
    plan = subscription.build_plan(detected_connections=1, instrument_db=instrument_db)
    modes = plan.keys_by_mode()
    assert subscription.MODE_FULL in modes
    assert inst.NIFTY_INDEX in modes[subscription.MODE_FULL]


def test_capacity_pressure_degrades_breadth_not_tier1(instrument_db, monkeypatch, db):
    monkeypatch.setattr(inst, "liquid_symbols",
                        lambda n, **kw: ["INFY", "RELIANCE"][:n])
    monkeypatch.setattr(cfg, "WS_MAX_KEYS_PER_CONNECTION", 6)
    plan = subscription.build_plan(detected_connections=1, instrument_db=instrument_db)
    assert plan.total <= 6
    # tier 1 survives intact; the trimming came from the lower tiers
    assert len(plan.by_tier[1]) == 5          # 3 indices + NIFTY near/next
    assert len(plan.by_tier.get(3, [])) + len(plan.by_tier.get(2, [])) <= 1


def test_persist_writes_and_deactivates(instrument_db, monkeypatch, db):
    monkeypatch.setattr(inst, "liquid_symbols", lambda *a, **kw: [])
    plan = subscription.build_plan(detected_connections=1, instrument_db=instrument_db)
    n = subscription.persist(plan, db)
    assert n == plan.total
    with store.connect(db) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM mc_instruments WHERE active=1").fetchone()[0]
    assert active == plan.total


# --------------------------------------------------------------------------- #
# Frame normalisation
# --------------------------------------------------------------------------- #
def test_ltpc_frame():
    msg = {"feeds": {"NSE_EQ|INE002A01018": {"ltpc": {
        "ltp": 2924.9, "ltt": "1725877734631", "ltq": "1", "cp": 2929.65}}},
        "currentTs": "1725879879317"}
    ticks = normalise_message(msg)
    assert len(ticks) == 1
    t = ticks[0]
    assert t.ltp == pytest.approx(2924.9)
    assert t.prev_close == pytest.approx(2929.65)
    assert t.chg_pct == pytest.approx((2924.9 - 2929.65) / 2929.65 * 100)


def test_efeeddetails_supplies_vwap_volume_and_oi_change():
    """One `full`/option frame carries almost the whole collection list."""
    payload = {"oc": {
        "ltpc": {"ltp": 141, "cp": 233.95},
        "bidAskQuote": {"bq": 600, "bp": 141, "aq": 50, "ap": 141.35},
        "eFeedDetails": {"atp": 189.33, "cp": 233.95, "vtt": "32205075",
                         "oi": 2811525, "poi": 3021775, "tbq": 58850,
                         "tsq": 94825},
    }}
    t = normalise_instrument("NSE_FO|50201", payload)
    assert t.vwap == pytest.approx(189.33)          # atp -> VWAP
    assert t.volume_today == pytest.approx(32205075)  # string coerced
    assert t.oi == pytest.approx(2811525)
    assert t.oi_prev_day == pytest.approx(3021775)
    assert t.oi_chg_pct == pytest.approx((2811525 - 3021775) / 3021775 * 100)
    assert t.bid == pytest.approx(141) and t.ask == pytest.approx(141.35)
    assert t.depth_imbalance == pytest.approx(58850 / 94825)


def test_day_ohlc_picks_the_1d_interval_not_1m():
    """Picking the wrong interval would make day_high a one-minute range —
    silently wrong rather than obviously broken."""
    payload = {"marketOHLC": {"ohlc": [
        {"interval": "I1", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
        {"interval": "1d", "open": 100, "high": 110, "low": 95, "close": 108},
    ]}}
    t = normalise_instrument("NSE_INDEX|Nifty 50", payload)
    assert t.day_high == pytest.approx(110)
    assert t.day_low == pytest.approx(95)


def test_depth_quote_as_list_of_levels():
    payload = {"marketLevel": {"bidAskQuote": [
        {"bidP": 100.5, "askP": 100.7, "bidQ": 10, "askQ": 12},
        {"bidP": 100.4, "askP": 100.8, "bidQ": 20, "askQ": 22},
    ]}, "ltpc": {"ltp": 100.6}}
    t = normalise_instrument("NSE_FO|1", payload)
    assert t.bid == pytest.approx(100.5)      # best level, not the second
    assert t.ask == pytest.approx(100.7)
    assert t.spread_pct == pytest.approx(0.2 / 100.6 * 100)


def test_rest_full_quote_schema_is_understood():
    """REGRESSION: the REST full-quote endpoint (used by resync) names fields
    last_price / volume / average_price, NOT ltp / vtt / atp. Before this was
    handled, restore_quotes() seeded nothing and a resync accomplished zero."""
    payload = {
        "last_price": 24000.0,
        "volume": 987654.0,
        "average_price": 23980.5,
        "oi": 12345.0,
        "total_buy_quantity": 100.0,
        "total_sell_quantity": 50.0,
        "ohlc": {"open": 23900, "high": 24100, "low": 23850, "close": 23950},
    }
    t = normalise_instrument("NSE_INDEX|Nifty 50", payload)
    assert t.ltp == pytest.approx(24000.0)
    assert t.volume_today == pytest.approx(987654.0)   # seeds the vol baseline
    assert t.vwap == pytest.approx(23980.5)
    assert t.oi == pytest.approx(12345.0)
    assert t.day_high == pytest.approx(24100)
    assert t.day_low == pytest.approx(23850)
    assert t.depth_imbalance == pytest.approx(2.0)


def test_plain_ohlc_dict_and_interval_list_both_work():
    """The two schemas must not interfere with each other."""
    ws = normalise_instrument("K", {"ltpc": {"ltp": 1}, "marketOHLC": {"ohlc": [
        {"interval": "I1", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
        {"interval": "1d", "open": 100, "high": 110, "low": 95, "close": 108}]}})
    assert ws.day_high == pytest.approx(110)
    rest = normalise_instrument("K", {"last_price": 5,
                                      "ohlc": {"open": 4, "high": 6, "low": 3, "close": 5}})
    assert rest.day_high == pytest.approx(6)


def test_heartbeat_and_garbage_frames_yield_nothing():
    assert normalise_message({"currentTs": "123"}) == []
    assert normalise_message({"feeds": {}}) == []
    assert normalise_message("not a dict") == []
    assert normalise_message(None) == []


def test_frame_without_price_is_dropped():
    assert normalise_message({"feeds": {"K": {"eFeedDetails": {"oi": 5}}}}) == []


def test_unparseable_numeric_is_missing_not_zero():
    """A false zero in volume would corrupt volume breadth."""
    t = normalise_instrument("K", {"ltpc": {"ltp": 10}, "eFeedDetails": {"vtt": "n/a"}})
    assert t.volume_today is None


# --------------------------------------------------------------------------- #
# Tick cache / bar aggregation
# --------------------------------------------------------------------------- #
def test_bar_aggregates_ohlc_within_a_minute():
    cache = TickCache()
    base = datetime(2026, 8, 4, 10, 0, 5)
    for i, price in enumerate([100, 105, 98, 102]):
        cache.update(_tick("K", ltp=price, at=base + timedelta(seconds=i * 10)))
    bar = cache._bars["K"]
    assert (bar.open, bar.high, bar.low, bar.close) == (100, 105, 98, 102)
    assert bar.tick_count == 4
    assert bar.ts == floor_minute(base)


def test_first_bar_volume_is_null_not_zero():
    """vtt is cumulative; the first bar has no baseline. NULL, never 0."""
    cache = TickCache()
    base = datetime(2026, 8, 4, 10, 0, 0)
    cache.update(_tick("K", ltp=100, volume_today=5000, at=base))
    cache.update(_tick("K", ltp=101, volume_today=5200, at=base + timedelta(seconds=5)))
    assert cache._bars["K"].volume is None


def test_second_bar_volume_is_a_delta():
    cache = TickCache()
    base = datetime(2026, 8, 4, 10, 0, 0)
    cache.update(_tick("K", ltp=100, volume_today=5000, at=base))
    nxt = base + timedelta(minutes=1)
    cache.update(_tick("K", ltp=101, volume_today=5200, at=nxt))
    cache.update(_tick("K", ltp=102, volume_today=5500, at=nxt + timedelta(seconds=5)))
    assert cache._bars["K"].volume == pytest.approx(500)


def test_reconnect_clears_volume_baseline():
    cache = TickCache()
    base = datetime(2026, 8, 4, 10, 0, 0)
    cache.update(_tick("K", ltp=100, volume_today=5000, at=base))
    cache.mark_disconnected()
    nxt = base + timedelta(minutes=1)
    cache.update(_tick("K", ltp=101, volume_today=9000, at=nxt))
    # delta would have spanned the outage — must be NULL instead
    assert cache._bars["K"].volume is None


def test_negative_volume_delta_is_rejected():
    cache = TickCache()
    base = datetime(2026, 8, 4, 10, 0, 0)
    cache.update(_tick("K", ltp=100, volume_today=5000, at=base))
    nxt = base + timedelta(minutes=1)
    cache.update(_tick("K", ltp=101, volume_today=5000, at=nxt))
    bar = cache._bars["K"]
    bar.volume_end = 4000                      # simulate a vtt reset
    assert bar.volume is None


def test_rolled_over_bar_is_not_dropped(db):
    """REGRESSION: a bar opened at 10:00 used to be DISCARDED the moment a
    10:01 tick arrived, because update() replaced _bars[key]. Bars and flushes
    are both on a 60s cadence, so every bar that rolled over between two flush
    ticks was silently lost."""
    cache = TickCache()
    base = datetime(2026, 8, 4, 10, 0, 0)
    cache.update(_tick("K", ltp=100, at=base))
    cache.update(_tick("K", ltp=104, at=base + timedelta(seconds=30)))
    # new minute arrives BEFORE any flush
    cache.update(_tick("K", ltp=101, at=base + timedelta(minutes=1)))
    written = cache.flush_completed_bars(base + timedelta(minutes=1), db)
    assert written == 1
    with store.connect(db) as conn:
        row = conn.execute(
            "SELECT ts, open, high, close FROM mc_bars_1m").fetchone()
    assert row["ts"] == "2026-08-04 10:00:00"
    assert row["open"] == 100 and row["high"] == 104 and row["close"] == 104


def test_bar_for_instrument_that_stopped_ticking_is_still_flushed(db):
    """An instrument that goes quiet must still have its last bar persisted."""
    cache = TickCache()
    base = datetime(2026, 8, 4, 10, 0, 0)
    cache.update(_tick("QUIET", ltp=50, at=base))
    assert cache.flush_completed_bars(base + timedelta(minutes=2), db) == 1


def test_flush_is_idempotent(db):
    cache = TickCache()
    base = datetime(2026, 8, 4, 10, 0, 0)
    cache.update(_tick("K", ltp=100, at=base))
    later = base + timedelta(minutes=1)
    cache.update(_tick("K", ltp=101, at=later))
    assert cache.flush_completed_bars(later, db) == 1
    assert cache.flush_completed_bars(later, db) == 0      # nothing left open
    with store.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mc_bars_1m").fetchone()[0] == 1


def test_stale_tier1_requires_all_silent():
    cache = TickCache()
    now = datetime(2026, 8, 4, 10, 0, 0)
    cache.update(_tick("A", ltp=1, at=now - timedelta(seconds=60)))
    cache.update(_tick("B", ltp=1, at=now - timedelta(seconds=1)))
    # B is fresh -> not a dead socket
    assert cache.stale_tier1(["A", "B"], 20.0, now) is False
    assert cache.stale_tier1(["A"], 20.0, now) is True


def test_never_seen_instrument_is_not_stale():
    cache = TickCache()
    assert cache.stale_tier1(["NEVER"], 20.0, datetime.now()) is False


# --------------------------------------------------------------------------- #
# Futures quadrant + basis
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("price,oi,expected", [
    (1.0, 5.0, LONG_BUILDUP),
    (-1.0, 5.0, SHORT_BUILDUP),
    (1.0, -5.0, SHORT_COVERING),
    (-1.0, -5.0, LONG_LIQUIDATION),
])
def test_quadrants(price, oi, expected):
    assert snapshots.classify_quadrant(price, oi) == expected


def test_deadbands_prevent_confident_noise():
    """Without deadbands a 0.01% drift produces a confident LONG_BUILDUP."""
    assert snapshots.classify_quadrant(0.01, 5.0) == POSITIONING_NEUTRAL
    assert snapshots.classify_quadrant(1.0, 0.01) == POSITIONING_NEUTRAL


def test_quadrant_unknown_without_inputs():
    assert snapshots.classify_quadrant(None, 5.0) == UNKNOWN
    assert snapshots.classify_quadrant(1.0, None) == UNKNOWN


def test_annualised_basis_scales_by_dte():
    near = snapshots.annualised_basis(0.4, 3)
    far = snapshots.annualised_basis(0.4, 30)
    assert near > far                       # same basis, very different carry
    assert snapshots.annualised_basis(0.4, 0) is None
    assert snapshots.annualised_basis(None, 10) is None


# --------------------------------------------------------------------------- #
# Collectors
# --------------------------------------------------------------------------- #
def _plan_with(tier2=(), tier3=(), tier4=()):
    plan = subscription.SubscriptionPlan(budget=cfg.subscription_budget(1))
    plan.mode_by_tier = {1: "full", 2: "ltpc", 3: "ltpc", 4: "full"}
    plan.by_tier = {1: [], 2: list(tier2), 3: list(tier3), 4: list(tier4)}
    return plan


def test_collect_vix_writes_intraday_row(db):
    cache = TickCache()
    cache.update(_tick(inst.INDIA_VIX, ltp=14.2, prev_close=13.5))
    row = snapshots.collect_vix(cache, db_path=db)
    assert row["ltp"] == pytest.approx(14.2)
    assert row["chg_pct"] == pytest.approx((14.2 - 13.5) / 13.5 * 100)
    with store.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM mc_vix").fetchone()[0] == 1


def test_collect_vix_without_data_returns_none(db):
    assert snapshots.collect_vix(TickCache(), db_path=db) is None


def test_collect_breadth_uses_previous_close(db, monkeypatch):
    monkeypatch.setattr(cfg, "BREADTH_MIN_NAMES", 2)
    names = [inst.Instrument(f"NSE_EQ|X{i}", f"S{i}", "equity") for i in range(4)]
    cache = TickCache()
    # 3 up, 1 down, all measured against prev_close
    for i, (ltp, cp) in enumerate([(110, 100), (105, 100), (102, 100), (90, 100)]):
        cache.update(_tick(f"NSE_EQ|X{i}", ltp=ltp, prev_close=cp))
    row = snapshots.collect_breadth(cache, _plan_with(tier3=names), db_path=db)
    assert (row["advancing"], row["declining"]) == (3, 1)
    assert row["adv_dec_pct"] == pytest.approx(75.0)
    assert row["is_subsample"] == 1
    assert row["sample_quality"] == pytest.approx(1.0)


def test_breadth_below_min_names_reports_no_reading(db, monkeypatch):
    monkeypatch.setattr(cfg, "BREADTH_MIN_NAMES", 20)
    names = [inst.Instrument("NSE_EQ|X0", "S0", "equity")]
    cache = TickCache()
    cache.update(_tick("NSE_EQ|X0", ltp=110, prev_close=100))
    row = snapshots.collect_breadth(cache, _plan_with(tier3=names), db_path=db)
    assert row["adv_dec_pct"] is None       # too few names to be a reading


def test_collect_sector_ranks_and_computes_relative_strength(db):
    sectors = [inst.Instrument("NSE_INDEX|Nifty IT", "NIFTY IT", "sector_index"),
               inst.Instrument("NSE_INDEX|Nifty Auto", "NIFTY AUTO", "sector_index")]
    cache = TickCache()
    cache.update(_tick(inst.NIFTY_INDEX, ltp=101, prev_close=100))     # +1%
    cache.update(_tick("NSE_INDEX|Nifty IT", ltp=103, prev_close=100))  # +3%
    cache.update(_tick("NSE_INDEX|Nifty Auto", ltp=100, prev_close=100))  # 0%
    rows = snapshots.collect_sector(cache, _plan_with(tier2=sectors), db_path=db)
    assert [r["sector"] for r in rows] == ["NIFTY IT", "NIFTY AUTO"]
    assert rows[0]["rs_rank"] == 1
    assert rows[0]["rel_strength"] == pytest.approx(2.0)
    assert rows[1]["rel_strength"] == pytest.approx(-1.0)


def test_sector_dispersion_needs_enough_sectors(monkeypatch):
    monkeypatch.setattr(cfg, "DISPERSION_MIN_SECTORS", 3)
    assert snapshots.sector_dispersion([{"ret_pct": 1.0}, {"ret_pct": 2.0}]) is None
    value = snapshots.sector_dispersion(
        [{"ret_pct": 1.0}, {"ret_pct": 2.0}, {"ret_pct": 3.0}])
    assert value == pytest.approx(0.816, abs=1e-3)


def test_collect_futures_computes_basis_against_matched_spot(db):
    fut = inst.Instrument("NSE_FO|58072", "NIFTY FUT 25 AUG 26", "futures",
                          underlying="NIFTY", expiry="2026-08-25")
    plan = _plan_with()
    plan.by_tier[1] = [fut]
    cache = TickCache()
    cache.update(_tick(inst.NIFTY_INDEX, ltp=24000.0, prev_close=23900.0))
    cache.update(_tick("NSE_FO|58072", ltp=24060.0, prev_close=23950.0,
                       oi=1000.0, oi_prev_day=900.0))
    rows = snapshots.collect_futures(cache, plan, now=datetime(2026, 8, 4, 10, 0),
                                     db_path=db)
    assert len(rows) == 1
    row = rows[0]
    assert row["basis"] == pytest.approx(60.0)
    assert row["dte"] == 21
    assert row["quadrant"] == LONG_BUILDUP      # price up, OI up
    assert row["basis_annualised"] is not None


def test_collect_all_is_resilient_to_empty_cache(db):
    out = snapshots.collect_all(TickCache(), _plan_with(), db_path=db)
    assert out["vix"] is None
    assert out["futures"] == [] and out["sector"] == []
