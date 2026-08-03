# -*- coding: utf-8 -*-
"""Regression tests for backtest/signals/momentum.py — the historical replay
of momentum_strategy.py's ORB/VWAP entry math (momentum_config thresholds:
range_candles=2, volume_mult=1.5, entry_cutoff 11:30)."""

import pandas as pd

from backtest.signals import momentum


def _bars(day, times, opens, highs, lows, closes, volumes):
    return pd.DataFrame({
        "ts": [pd.Timestamp(f"{day} {t}") for t in times],
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes,
    })


def test_orb_ce_breakout_with_volume_confirmation():
    # Opening range (09:15, 09:30) -> high=105, low=95. 09:45 closes above
    # the range high on 2x the opening-range volume -> CE breakout.
    df = _bars("2026-06-01",
               ["09:15", "09:30", "09:45", "10:00"],
               [100, 102, 106, 110],
               [103, 105, 110, 112],
               [99, 100, 105, 108],
               [102, 104, 110, 111],
               [100, 100, 200, 50])
    sigs = momentum.find_orb_signals(df, "TESTSTK")
    assert len(sigs) == 1
    assert sigs[0]["side"] == "CE"
    assert sigs[0]["trigger"] == "ORB"
    assert sigs[0]["entry_ts"] == pd.Timestamp("2026-06-01 09:45")


def test_orb_pe_breakdown_with_volume_confirmation():
    df = _bars("2026-06-01",
               ["09:15", "09:30", "09:45", "10:00"],
               [100, 98, 93, 90],
               [101, 99, 94, 91],
               [97, 95, 90, 88],
               [98, 96, 90, 89],
               [100, 100, 200, 50])
    sigs = momentum.find_orb_signals(df, "TESTSTK")
    assert len(sigs) == 1
    assert sigs[0]["side"] == "PE"


def test_orb_no_signal_when_volume_insufficient():
    # Same breakout price action as the CE test, but breakout-bar volume is
    # only 1.2x the opening-range average (< volume_mult=1.5) -> no signal.
    df = _bars("2026-06-01",
               ["09:15", "09:30", "09:45", "10:00"],
               [100, 102, 106, 110],
               [103, 105, 110, 112],
               [99, 100, 105, 108],
               [102, 104, 110, 111],
               [100, 100, 120, 50])
    assert momentum.find_orb_signals(df, "TESTSTK") == []


def test_orb_no_signal_inside_the_range():
    df = _bars("2026-06-01",
               ["09:15", "09:30", "09:45", "10:00"],
               [100, 102, 101, 103],
               [103, 105, 104, 105],
               [99, 100, 100, 101],
               [102, 104, 102, 103],
               [100, 100, 200, 50])
    assert momentum.find_orb_signals(df, "TESTSTK") == []


def test_orb_stops_scanning_after_entry_cutoff():
    # A breakout at 11:45 (past the 11:30 cutoff) must not fire.
    df = _bars("2026-06-01",
               ["09:15", "09:30", "11:45"],
               [100, 102, 106],
               [103, 105, 110],
               [99, 100, 105],
               [102, 104, 110],
               [100, 100, 200])
    assert momentum.find_orb_signals(df, "TESTSTK") == []


def test_vwap_reclaim_signal():
    # Cumulative VWAP after bar 1 sits above bar 2's close (below vwap), then
    # bar 3 closes back above vwap on 1.3x+ volume -> CE vwap_reclaim.
    df = _bars("2026-06-01",
               ["09:15", "09:30", "09:45"],
               [100, 95, 96],
               [101, 96, 102],
               [99, 90, 95],
               [100, 91, 101],
               [1000, 1000, 1400])
    sigs = momentum.find_vwap_signals(df, "TESTSTK")
    assert len(sigs) == 1
    assert sigs[0]["side"] == "CE"
    assert sigs[0]["trigger"] == "vwap_reclaim"


def test_find_signals_merges_and_sorts_both_rules():
    df = _bars("2026-06-01",
               ["09:15", "09:30", "09:45", "10:00"],
               [100, 102, 106, 110],
               [103, 105, 110, 112],
               [99, 100, 105, 108],
               [102, 104, 110, 111],
               [100, 100, 200, 50])
    only_orb = momentum.find_signals(df, "TESTSTK", rule="orb")
    only_vwap = momentum.find_signals(df, "TESTSTK", rule="vwap")
    both = momentum.find_signals(df, "TESTSTK", rule="both")
    assert both == sorted(only_orb + only_vwap, key=lambda s: s["entry_ts"])


def test_empty_candles_returns_no_signals():
    assert momentum.find_orb_signals(pd.DataFrame(), "TESTSTK") == []
    assert momentum.find_vwap_signals(pd.DataFrame(), "TESTSTK") == []
