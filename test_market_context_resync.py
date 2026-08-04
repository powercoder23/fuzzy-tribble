# -*- coding: utf-8 -*-
"""
test_market_context_resync.py — REST recovery and the plan-tier probe.

Both are failure-path code, which means in production they run rarely and are
never exercised by the happy path. The properties tested here are the ones a
silent bug would corrupt:

  * a short gap must NOT trigger REST calls (REST is recovery, not data);
  * a resync must restore the volume BASELINE, otherwise every remaining bar
    that session writes NULL volume;
  * a seeded value must NOT make a dead socket look alive to the watchdog;
  * backfilled bars must be distinguishable from observed ones;
  * the probe must fail CONSERVATIVE — over-subscribing is worse than under.
"""

from datetime import datetime, timedelta

import pytest

from market_context import config as cfg
from market_context import instruments as inst
from market_context import store
from market_context.feed import probe as probe_mod
from market_context.feed import resync as resync_mod
from market_context.feed import subscription
from market_context.feed.cache import TickCache
from market_context.feed.normaliser import Tick
from market_context.feed.resync import Resyncer


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "market_context.db")
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(cfg, "DB_PATH", path)
    store.init_db(path)
    return path


def _plan(keys=("NSE_INDEX|Nifty 50", "NSE_EQ|INE009A01021")):
    plan = subscription.SubscriptionPlan(budget=cfg.subscription_budget(1))
    plan.mode_by_tier = {1: "full", 2: "ltpc", 3: "ltpc", 4: "full"}
    plan.by_tier = {
        1: [inst.Instrument(keys[0], "NIFTY", "index")],
        2: [], 3: [inst.Instrument(keys[1], "INFY", "equity")], 4: [],
    }
    return plan


def _tick(key, **kw):
    return Tick(instrument_key=key, received_at=kw.pop("at", datetime.now()), **kw)


# --------------------------------------------------------------------------- #
# Gate: REST is the recovery path, not the data path
# --------------------------------------------------------------------------- #
def test_short_gap_is_skipped_without_any_api_call(db, monkeypatch):
    called = {"n": 0}

    def factory():
        called["n"] += 1
        raise AssertionError("must not build a client for a short gap")

    r = Resyncer(_plan(), TickCache(), db_path=db, api_client_factory=factory)
    out = r.resync(gap_seconds=cfg.RESYNC_THRESHOLD_SEC - 1)
    assert out["skipped"] is True
    assert called["n"] == 0


def test_empty_plan_is_skipped(db):
    empty = subscription.SubscriptionPlan(budget=cfg.subscription_budget(1))
    r = Resyncer(empty, TickCache(), db_path=db,
                 api_client_factory=lambda: pytest.fail("no client expected"))
    assert r.resync(gap_seconds=9999)["skipped"] is True


def test_unavailable_api_client_degrades_quietly(db):
    def factory():
        raise RuntimeError("no token")

    out = Resyncer(_plan(), TickCache(), db_path=db,
                   api_client_factory=factory).resync(gap_seconds=9999)
    assert out["skipped"] is True
    assert "api client unavailable" in out["reason"]


# --------------------------------------------------------------------------- #
# Baseline restoration — the whole point of resync
# --------------------------------------------------------------------------- #
def test_seed_restores_volume_baseline(db):
    """After an outage the cache drops volume baselines. Without a resync,
    every remaining bar this session writes NULL volume."""
    cache = TickCache()
    base = datetime(2026, 8, 4, 10, 0, 0)
    cache.update(_tick("K", ltp=100, volume_today=5000, at=base))
    cache.mark_disconnected()

    # Without a seed the next bar has no baseline.
    nxt = base + timedelta(minutes=1)
    cache.update(_tick("K", ltp=101, volume_today=9000, at=nxt))
    assert cache._bars["K"].volume is None

    # With a seed, the bar AFTER it can compute a delta again.
    cache.seed(_tick("K", ltp=101, volume_today=9000))
    later = base + timedelta(minutes=2)
    cache.update(_tick("K", ltp=102, volume_today=9600, at=later))
    assert cache._bars["K"].volume == pytest.approx(600)


def test_seed_does_not_make_a_dead_socket_look_alive(db):
    """A REST-seeded value must not reset the staleness watchdog."""
    cache = TickCache()
    now = datetime(2026, 8, 4, 10, 0, 0)
    cache.update(_tick("T1", ltp=1, at=now - timedelta(seconds=120)))
    assert cache.stale_tier1(["T1"], 20.0, now) is True

    cache.seed(_tick("T1", ltp=2, at=now))
    assert cache.stale_tier1(["T1"], 20.0, now) is True      # still stale
    assert cache.last("T1").ltp == 2                          # but value updated


def test_seed_does_not_open_a_bar_or_count_a_frame():
    cache = TickCache()
    cache.seed(_tick("K", ltp=100, volume_today=1))
    assert cache.frames == 0
    assert "K" not in cache._bars
    assert cache.last_frame_at is None


# --------------------------------------------------------------------------- #
# Quote restore
# --------------------------------------------------------------------------- #
class _FakeQuoteApi:
    def __init__(self, payload, recorder):
        self._payload = payload
        self._recorder = recorder

    def get_full_market_quote(self, symbol, api_version):
        self._recorder.append((symbol, api_version))
        return type("R", (), {"data": self._payload})()


def test_restore_quotes_seeds_cache_from_one_batched_call(db, monkeypatch):
    plan = _plan()
    cache = TickCache()
    calls = []
    payload = {
        "NSE_INDEX|Nifty 50": {"last_price": 24000.0,
                               "ohlc": {"open": 23900, "high": 24100, "low": 23850,
                                        "close": 23950},
                               "volume": 1234.0},
        "NSE_EQ:INFY": {"last_price": 1185.0, "volume": 555.0},
    }
    fake = _FakeQuoteApi(payload, calls)
    monkeypatch.setattr(resync_mod, "_get", resync_mod._get)   # keep helper
    import upstox_client
    monkeypatch.setattr(upstox_client, "MarketQuoteApi", lambda c: fake)

    r = Resyncer(plan, cache, db_path=db, api_client_factory=lambda: object())
    seeded = r.restore_quotes(object())

    assert seeded == 2
    assert len(calls) == 1                       # ONE batched call, not per key
    # 'NSE_EQ:INFY' is keyed by trading symbol and must map back to our key
    assert cache.last("NSE_EQ|INE009A01021") is not None
    assert cache.last("NSE_INDEX|Nifty 50").ltp == pytest.approx(24000.0)


def test_match_key_falls_back_to_trading_symbol(db):
    r = Resyncer(_plan(), TickCache(), db_path=db,
                 api_client_factory=lambda: object())
    assert r._match_key("NSE_EQ:INFY", {}, []) == "NSE_EQ|INE009A01021"
    assert r._match_key("NSE_EQ:UNKNOWN", {}, []) is None


# --------------------------------------------------------------------------- #
# Bar backfill
# --------------------------------------------------------------------------- #
def test_candle_row_is_marked_reconstructed():
    """tick_count=0 is what distinguishes a backfilled bar from an observed
    one. Without the marker the two are indistinguishable in research."""
    row = resync_mod._candle_to_bar_row(
        "K", ["2026-08-04T10:15:00+05:30", 1, 2, 0.5, 1.5, 100, 7], None, None)
    assert row[1] == "2026-08-04 10:15:00"
    assert row[-1] == 0
    assert row[2:7] == (1.0, 2.0, 0.5, 1.5, 100.0)


def test_candle_rows_outside_the_gap_window_are_dropped():
    inside = resync_mod._candle_to_bar_row(
        "K", ["2026-08-04T10:15:00+05:30", 1, 2, 0.5, 1.5, 100],
        "2026-08-04 10:00:00", "2026-08-04 10:30:00")
    before = resync_mod._candle_to_bar_row(
        "K", ["2026-08-04T09:15:00+05:30", 1, 2, 0.5, 1.5, 100],
        "2026-08-04 10:00:00", "2026-08-04 10:30:00")
    after = resync_mod._candle_to_bar_row(
        "K", ["2026-08-04T11:15:00+05:30", 1, 2, 0.5, 1.5, 100],
        "2026-08-04 10:00:00", "2026-08-04 10:30:00")
    assert inside is not None and before is None and after is None


def test_malformed_candle_is_ignored():
    assert resync_mod._candle_to_bar_row("K", ["short"], None, None) is None
    assert resync_mod._candle_to_bar_row("K", None, None, None) is None


def test_backfilled_bars_do_not_overwrite_observed_bars(db):
    """INSERT OR IGNORE: an observed bar must win over a reconstructed one."""
    observed = ("K", "2026-08-04 10:15:00", 10, 11, 9, 10.5, 500, None, None,
                None, None, None, None, None, 42)
    with store.connect(db) as conn:
        conn.execute(
            "INSERT INTO mc_bars_1m (instrument_key, ts, open, high, low, close,"
            " volume, oi, oi_chg, vwap, bid, ask, bid_qty, ask_qty, tick_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", observed)
        conn.execute(
            "INSERT OR IGNORE INTO mc_bars_1m (instrument_key, ts, open, high,"
            " low, close, volume, oi, oi_chg, vwap, bid, ask, bid_qty, ask_qty,"
            " tick_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("K", "2026-08-04 10:15:00", 1, 1, 1, 1, 1, None, None, None,
             None, None, None, None, 0))
        conn.commit()
        row = conn.execute("SELECT tick_count, close FROM mc_bars_1m").fetchone()
    assert row["tick_count"] == 42 and row["close"] == 10.5


def test_window_from_gap():
    start, end = resync_mod.window_from_gap(
        datetime(2026, 8, 4, 10, 3, 30), datetime(2026, 8, 4, 10, 9, 45))
    assert start == "2026-08-04 10:03:00"
    assert end == "2026-08-04 10:09:00"


# --------------------------------------------------------------------------- #
# Plan-tier probe
# --------------------------------------------------------------------------- #
def test_explicit_tier_skips_probing(db, monkeypatch):
    monkeypatch.setattr(cfg, "PLAN_TIER", "plus")
    monkeypatch.setattr(probe_mod, "probe_max_connections",
                        lambda *a, **k: pytest.fail("must not probe"))
    assert probe_mod.detect_connections(db) == cfg.WS_CONNECTIONS_PLUS

    monkeypatch.setattr(cfg, "PLAN_TIER", "standard")
    assert probe_mod.detect_connections(db) == cfg.WS_CONNECTIONS_STANDARD


def test_probe_disabled_assumes_standard(db, monkeypatch):
    monkeypatch.setattr(cfg, "PLAN_TIER", "auto")
    monkeypatch.setattr(cfg, "PLAN_PROBE_ENABLED", False)
    monkeypatch.setattr(probe_mod, "probe_max_connections",
                        lambda *a, **k: pytest.fail("must not probe"))
    assert probe_mod.detect_connections(db) == cfg.WS_CONNECTIONS_STANDARD


def test_two_concurrent_connections_resolve_to_plus(db, monkeypatch):
    monkeypatch.setattr(cfg, "PLAN_TIER", "auto")
    monkeypatch.setattr(cfg, "PLAN_PROBE_ENABLED", True)
    monkeypatch.setattr(probe_mod, "probe_max_connections", lambda *a, **k: 2)
    assert probe_mod.detect_connections(db, force=True) == cfg.WS_CONNECTIONS_PLUS


def test_one_connection_resolves_to_standard(db, monkeypatch):
    monkeypatch.setattr(cfg, "PLAN_TIER", "auto")
    monkeypatch.setattr(cfg, "PLAN_PROBE_ENABLED", True)
    monkeypatch.setattr(probe_mod, "probe_max_connections", lambda *a, **k: 1)
    assert probe_mod.detect_connections(db, force=True) == cfg.WS_CONNECTIONS_STANDARD


def test_probe_result_is_cached_and_reused(db, monkeypatch):
    monkeypatch.setattr(cfg, "PLAN_TIER", "auto")
    monkeypatch.setattr(cfg, "PLAN_PROBE_ENABLED", True)
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return 2

    monkeypatch.setattr(probe_mod, "probe_max_connections", counting)
    first = probe_mod.detect_connections(db)
    second = probe_mod.detect_connections(db)
    assert first == second == cfg.WS_CONNECTIONS_PLUS
    assert calls["n"] == 1                      # second call used the cache


def test_expired_cache_triggers_a_reprobe(db, monkeypatch):
    monkeypatch.setattr(cfg, "PLAN_TIER", "auto")
    monkeypatch.setattr(cfg, "PLAN_PROBE_ENABLED", True)
    monkeypatch.setattr(cfg, "PLAN_PROBE_TTL_DAYS", 7)
    stale = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    store.set_meta(probe_mod._META_CONNECTIONS, 5, db_path=db)
    store.set_meta(probe_mod._META_PROBED_AT, stale, db_path=db)

    monkeypatch.setattr(probe_mod, "probe_max_connections", lambda *a, **k: 1)
    assert probe_mod.detect_connections(db) == cfg.WS_CONNECTIONS_STANDARD


def test_probe_failure_is_conservative(db, monkeypatch):
    """Under-subscribing degrades breadth visibly; over-subscribing would have
    the socket silently carry fewer instruments than the plan claims."""
    monkeypatch.setattr(cfg, "PLAN_TIER", "auto")
    monkeypatch.setattr(probe_mod, "_api_client",
                        lambda: (_ for _ in ()).throw(RuntimeError("no token")))
    assert probe_mod.probe_max_connections() == 1


def test_budget_follows_the_detected_allowance(monkeypatch):
    monkeypatch.setattr(cfg, "PLAN_TIER", "auto")
    standard = cfg.subscription_budget(1)
    plus = cfg.subscription_budget(5)
    assert standard.tier == "standard" and standard.breadth_is_subsample is True
    assert plus.tier == "plus" and plus.breadth_is_subsample is False
    assert plus.tier3 > standard.tier3
