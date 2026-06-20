# -*- coding: utf-8 -*-
"""Unit tests for entry_gate. No broker; composite lookup is monkeypatched."""

import sys
import types

import entry_gate
import entry_gate_config as gcfg


def _fake_composite(result):
    """Install a fake composite_scanner.get_latest_composite returning `result`."""
    m = types.ModuleType("composite_scanner")
    m.get_latest_composite = lambda sid: result
    sys.modules["composite_scanner"] = m


def test_off_mode_always_allows():
    gcfg.GATE_MODE = "off"
    assert entry_gate.passes("123", "CE") is True


def test_soft_mode_allows_but_returns_score():
    gcfg.GATE_MODE = "soft"
    _fake_composite({"direction": "PE", "score": 80, "grade": "STRONG"})
    r = entry_gate.evaluate("123", "CE")
    assert r["allow"] is True and r["score"] == 80   # never blocks, surfaces info


def test_hard_blocks_direction_mismatch():
    gcfg.GATE_MODE = "hard"
    _fake_composite({"direction": "PE", "score": 80, "grade": "STRONG"})
    assert entry_gate.passes("123", "CE") is False


def test_hard_allows_agreeing_strong():
    gcfg.GATE_MODE = "hard"
    _fake_composite({"direction": "CE", "score": 75, "grade": "STRONG"})
    assert entry_gate.passes("123", "CE") is True


def test_hard_blocks_weak_or_low_score():
    gcfg.GATE_MODE = "hard"
    _fake_composite({"direction": "CE", "score": 20, "grade": "WEAK"})
    assert entry_gate.passes("123", "CE") is False


def test_hard_fail_open_when_no_composite():
    gcfg.GATE_MODE = "hard"
    gcfg.ALLOW_IF_NO_COMPOSITE = True
    _fake_composite({})
    assert entry_gate.passes("123", "CE") is True


if __name__ == "__main__":
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    gcfg.GATE_MODE = "off"
    sys.exit(1 if failed else 0)
