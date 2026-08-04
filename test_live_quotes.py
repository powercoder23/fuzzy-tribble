# -*- coding: utf-8 -*-
"""Tests for the live-quote fast path added 2026-08-04:
  * market_context.store: mc_live_quotes upsert/read round-trip
  * market_context.get_quote(): fresh / stale / missing
  * paper_trader._option_key_for / _requote(): live-quote fast path with
    REST fallback (regression guard — must behave exactly as before when
    market_context has nothing for this instrument)
"""

import os
import tempfile
from datetime import datetime, timedelta

import market_context
import market_context.config as mc_cfg
import market_context.store as mc_store
import paper_trader


def _tmp_mc_db():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    mc_store.init_db(p)
    return p


def _cleanup(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def test_upsert_and_read_live_quote():
    p = _tmp_mc_db()
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        n = mc_store.upsert_live_quotes([("NSE_FO|999", 12.5, ts)], p)
        assert n == 1
        row = mc_store.latest_quote("NSE_FO|999", p)
        assert row is not None and abs(row["ltp"] - 12.5) < 1e-9

        # overwrite (same key) must replace, not duplicate
        ts2 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mc_store.upsert_live_quotes([("NSE_FO|999", 13.0, ts2)], p)
        row2 = mc_store.latest_quote("NSE_FO|999", p)
        assert abs(row2["ltp"] - 13.0) < 1e-9
    finally:
        _cleanup(p)


def test_latest_quote_missing_key_returns_none():
    p = _tmp_mc_db()
    try:
        assert mc_store.latest_quote("NSE_FO|does_not_exist", p) is None
    finally:
        _cleanup(p)


def test_get_quote_fresh_vs_stale(monkeypatch):
    p = _tmp_mc_db()
    try:
        monkeypatch.setattr(mc_store, "DB_PATH", p)
        monkeypatch.setattr(mc_cfg, "MODE", "observe")
        market_context.invalidate()

        fresh_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mc_store.upsert_live_quotes([("NSE_FO|1", 100.0, fresh_ts)], p)
        q = market_context.get_quote("NSE_FO|1", max_age_seconds=0)
        assert q is not None and q["ltp"] == 100.0

        stale_ts = (datetime.now() - timedelta(seconds=mc_cfg.QUOTE_STALE_SEC + 5)
                   ).strftime("%Y-%m-%d %H:%M:%S")
        mc_store.upsert_live_quotes([("NSE_FO|2", 200.0, stale_ts)], p)
        q2 = market_context.get_quote("NSE_FO|2", max_age_seconds=0)
        assert q2 is None, "a quote older than QUOTE_STALE_SEC must report unavailable"
    finally:
        market_context.invalidate()
        _cleanup(p)


def test_get_quote_off_mode_returns_none(monkeypatch):
    p = _tmp_mc_db()
    try:
        monkeypatch.setattr(mc_store, "DB_PATH", p)
        monkeypatch.setattr(mc_cfg, "MODE", "off")
        market_context.invalidate()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mc_store.upsert_live_quotes([("NSE_FO|3", 50.0, ts)], p)
        assert market_context.get_quote("NSE_FO|3", max_age_seconds=0) is None
    finally:
        market_context.invalidate()
        _cleanup(p)


# --- paper_trader integration: fast path + REST fallback --------------------

class _FakeScanner:
    def __init__(self, last):
        self._last = last

    def get_current_option_premium(self, *a, **k):
        return {"last": self._last}


def _trade(symbol="RELIANCE", expiry="2026-08-25", strike=1500.0, side="CE"):
    return {"id": 42, "symbol": symbol, "expiry": expiry, "strike": strike,
            "side": side, "security_id": "999", "exchange_segment": "NSE_FNO"}


def test_requote_uses_live_quote_when_fresh(monkeypatch):
    p = _tmp_mc_db()
    try:
        monkeypatch.setattr(mc_store, "DB_PATH", p)
        monkeypatch.setattr(mc_cfg, "MODE", "observe")
        market_context.invalidate()

        import upstox_adapter
        monkeypatch.setattr(upstox_adapter, "option_instrument_key",
                            lambda *a, **k: "NSE_FO|LIVE")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mc_store.upsert_live_quotes([("NSE_FO|LIVE", 77.5, ts)], p)

        last = paper_trader._requote(_FakeScanner(last=999.0), _trade())
        assert last == 77.5, "must prefer the fresh live quote over the REST call"
    finally:
        market_context.invalidate()
        _cleanup(p)


def test_requote_falls_back_to_rest_when_no_live_quote(monkeypatch):
    p = _tmp_mc_db()
    try:
        monkeypatch.setattr(mc_store, "DB_PATH", p)
        monkeypatch.setattr(mc_cfg, "MODE", "observe")
        market_context.invalidate()

        import upstox_adapter
        monkeypatch.setattr(upstox_adapter, "option_instrument_key",
                            lambda *a, **k: "NSE_FO|NOTHING_SUBSCRIBED")

        last = paper_trader._requote(_FakeScanner(last=42.5), _trade())
        assert last == 42.5, "no live quote available -> must fall back to REST exactly as before"
    finally:
        market_context.invalidate()
        _cleanup(p)


def test_requote_falls_back_when_key_unresolvable(monkeypatch):
    import upstox_adapter
    monkeypatch.setattr(upstox_adapter, "option_instrument_key", lambda *a, **k: None)
    last = paper_trader._requote(_FakeScanner(last=10.0), _trade())
    assert last == 10.0


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
