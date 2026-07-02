# -*- coding: utf-8 -*-
"""Unit tests for engine/candles.py + engine/triggers.py (pure, synthetic candles)."""

from engine import candles as cd
from engine import triggers as tg
from engine.contracts import CE, PE


def c(ts, o, h, l, cl, v=100.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": cl, "volume": v}


def _t(i):  # 5-min timestamps from 09:15
    m = 15 + i * 5
    return f"2026-07-02 {9 + m // 60:02d}:{m % 60:02d}:00"


def _t15(i):  # 15-min timestamps from 09:15
    m = 15 + i * 15
    return f"2026-07-02 {9 + m // 60:02d}:{m % 60:02d}:00"


class TestAggregation:
    def test_5m_to_15m(self):
        c5 = [c(_t(i), 100 + i, 101 + i, 99 + i, 100.5 + i, 10) for i in range(7)]
        c15 = cd.aggregate(c5, 3)
        assert len(c15) == 2                       # 7 -> 2 complete groups, partial dropped
        assert c15[0]["open"] == 100 and c15[0]["close"] == 102.5
        assert c15[0]["high"] == 103 and c15[0]["low"] == 99
        assert c15[0]["volume"] == 30

    def test_vwap_monotone_inputs(self):
        c5 = [c(_t(i), 100, 100, 100, 100, 10) for i in range(4)]
        assert all(abs(v - 100) < 1e-9 for v in cd.session_vwap(c5))


class TestORB:
    def _base(self, closes, vols):
        # candle 0 = opening range 09:15-09:30 (high 102 / low 98)
        out = [c(_t15(0), 100, 102, 98, 101, 100)]
        for i, (cl, v) in enumerate(zip(closes, vols), start=1):
            out.append(c(_t15(i), cl - 0.5, cl + 0.5, cl - 1, cl, v))
        return out

    def test_breakout_up_with_volume(self):
        c15 = self._base([101, 101.5, 101, 101.2, 104], [100, 100, 100, 100, 250])
        t = tg.detect_orb(c15)
        assert t and t.direction == CE and t.kind == "ORB"

    def test_no_volume_no_trigger(self):
        c15 = self._base([101, 101.5, 101, 101.2, 104], [100, 100, 100, 100, 110])
        assert tg.detect_orb(c15) is None

    def test_inside_range_no_trigger(self):
        c15 = self._base([101, 101.5, 101, 101.2, 101.8], [100, 100, 100, 100, 300])
        assert tg.detect_orb(c15) is None

    def test_breakdown_pe(self):
        c15 = self._base([99, 99.5, 99, 98.8, 96], [100, 100, 100, 100, 300])
        t = tg.detect_orb(c15)
        assert t and t.direction == PE

    def test_after_cutoff_no_trigger(self):
        c15 = self._base([101] * 8 + [104], [100] * 8 + [300])  # last candle 11:30+... build 9 candles
        # candle index 9 -> 09:15 + 9*15 = 11:30 start; craft one past 11:30
        c15.append(c("2026-07-02 11:45:00", 103.5, 104.5, 103, 104.2, 400))
        assert tg.detect_orb(c15) is None


class TestVWAP:
    def test_reclaim_ce(self):
        # prices below vwap then close above on volume
        c15 = [c(_t15(0), 100, 101, 99, 99.2, 100),
               c(_t15(1), 99.2, 99.5, 98.5, 98.8, 100),
               c(_t15(2), 98.8, 101.5, 98.7, 101.2, 200)]
        vwap = cd.session_vwap(c15)
        t = tg.detect_vwap(c15, vwap)
        assert t and t.direction == CE and t.detail["kind"] == "vwap_reclaim"

    def test_no_cross_no_trigger(self):
        c15 = [c(_t15(i), 100, 101, 99, 100.5, 150) for i in range(3)]
        assert tg.detect_vwap(c15, cd.session_vwap(c15)) is None


class TestBreakRetest:
    def _c15_breakout_up(self, prev_high=110.0):
        return [c(_t15(0), 108, 109, 107, 108.5, 100),
                c(_t15(1), 108.5, 111.5, 108, 111.2, 220)]  # closes above yest high

    def test_hammer_retest_ce(self):
        prev_high = 110.0
        c15 = self._c15_breakout_up()
        # 5m: pullback into the level, then hammer tagging 110 with long lower wick
        c5 = [c(_t(0), 111.0, 111.3, 110.8, 111.0),
              c(_t(1), 111.0, 111.1, 110.4, 110.6),
              c(_t(2), 110.6, 110.75, 109.95, 110.7)]   # low tags 110, tiny body, long wick
        t = tg.detect_break_retest(c5, c15, prev_high, 100.0)
        assert t and t.direction == CE and t.kind == "BREAK_RETEST"
        assert t.detail["pattern"] == "hammer"

    def test_no_tag_no_trigger(self):
        c5 = [c(_t(0), 112, 112.5, 111.8, 112.2),
              c(_t(1), 112.2, 112.6, 112.0, 112.4),
              c(_t(2), 112.4, 112.9, 112.2, 112.7)]     # never comes back to 110
        t = tg.detect_break_retest(c5, self._c15_breakout_up(), 110.0, 100.0)
        assert t is None

    def test_no_breakout_no_trigger(self):
        c15 = [c(_t15(0), 108, 109, 107, 108.5, 100)]   # never closes above 110
        c5 = [c(_t(0), 108, 109, 107, 108.5)] * 3
        assert tg.detect_break_retest(c5, c15, 110.0, 100.0) is None

    def test_missing_levels_fail_open(self):
        assert tg.detect_break_retest([{}] * 3, [{}], None, None) is None


class TestCandleStoreRoundtrip:
    def test_save_load_and_levels(self, tmp_path):
        import pandas as pd
        db = str(tmp_path / "t.db")
        df = pd.DataFrame({
            "timestamp": [_t(i) for i in range(3)],
            "open": [100, 101, 102], "high": [101, 102, 103],
            "low": [99, 100, 101], "close": [100.5, 101.5, 102.5],
            "volume": [10, 20, 30]})
        n = cd.save_candles(db, "101", "TATAMOTORS", df)
        assert n == 3
        rows = cd.load_today(db, "101", day="2026-07-02")
        assert len(rows) == 3 and rows[0]["close"] == 100.5
        # yesterday levels from delivery_daily
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE delivery_daily (date TEXT, symbol TEXT, "
                         "open REAL, high REAL, low REAL, close REAL, "
                         "volume INTEGER, deliv_qty INTEGER, deliv_pct REAL, "
                         "PRIMARY KEY (date, symbol))")
            conn.execute("INSERT INTO delivery_daily VALUES "
                         "('2026-07-01','TATAMOTORS',100,110,95,108,1000,500,50)")
            conn.commit()
        ph, pl = cd.yesterday_levels(db, "TATAMOTORS", day="2026-07-02")
        assert ph == 110.0 and pl == 95.0
