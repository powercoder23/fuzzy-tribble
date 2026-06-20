# -*- coding: utf-8 -*-
"""Unit tests for data_provider (pollers/cache/subscribe-move-unsubscribe). No broker."""

from data_provider import CandleCache, CandlePoller, DataProvider


class FakeFetch:
    def __init__(self):
        self.calls = []
    def __call__(self, instrument, segment, interval):
        self.calls.append((instrument, segment, interval))
        return [{"inst": instrument, "interval": interval}]   # stand-in DataFrame


def test_fetch_once_per_subscribed_instrument_per_tick():
    f = FakeFetch(); cache = CandleCache()
    p = CandlePoller(5, f, cache)
    p.subscribe("A", "s1", "NSE_EQ")
    p.subscribe("B", "s1", "NSE_EQ")
    p.subscribe("A", "s2", "NSE_EQ")        # second subscriber, same instrument
    n = p.tick()
    assert n == 2                            # A and B fetched ONCE each, not 3x
    assert sorted(i for i, _, _ in f.calls) == ["A", "B"]


def test_unsubscribe_drops_only_when_no_subscribers_left():
    f = FakeFetch(); p = CandlePoller(5, f, CandleCache())
    p.subscribe("A", "s1", "NSE_EQ"); p.subscribe("A", "s2", "NSE_EQ")
    p.unsubscribe("A", "s1")
    assert p.instruments() == ["A"]          # s2 still needs it
    p.unsubscribe("A", "s2")
    assert p.instruments() == []


def test_move_between_pollers():
    dp = DataProvider(fetch_intraday=FakeFetch())
    dp.subscribe("X", "bb", 15, "NSE_EQ")
    assert dp.poll_15m.instruments() == ["X"] and dp.poll_5m.instruments() == []
    dp.move("X", "bb", 15, 5, "NSE_EQ")      # breakout -> retest watch
    assert dp.poll_15m.instruments() == [] and dp.poll_5m.instruments() == ["X"]


def test_read_through_caches_and_falls_back():
    f = FakeFetch()
    dp = DataProvider(fetch_intraday=f)
    # cache miss -> fetch
    dp.intraday_candles("Z", "NSE_EQ", interval=15)
    assert len(f.calls) == 1
    # cache hit -> no extra fetch
    dp.intraday_candles("Z", "NSE_EQ", interval=15)
    assert len(f.calls) == 1


def test_poller_due_once_per_boundary():
    from datetime import datetime
    p = CandlePoller(5, FakeFetch(), CandleCache())
    t = datetime(2026, 6, 19, 9, 35, 40)     # 40s after the 9:35 boundary
    assert p.due(t) is True
    p.tick(t)
    assert p.due(t.replace(second=50)) is False   # same boundary already done


if __name__ == "__main__":
    import sys
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
