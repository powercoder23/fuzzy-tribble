# -*- coding: utf-8 -*-
"""Unit tests for morning_confluence pure logic (evaluate / decide_strike). No broker."""

import morning_confluence as mc
import morning_confluence_config as cfg


def _gap(bias, pct=2.0):
    return {"bias": bias, "gap_pct": pct, "direction": "GAP_UP" if bias == "CE" else "GAP_DOWN"}

def _oi(bias, px=1.2, oi=6.0):
    return {"bias": bias, "price_chg_pct": px, "oi_chg_pct": oi}


def test_aplus_when_gap_oi_agree_and_iv_cheap():
    r = mc.evaluate(_gap("CE"), _oi("CE"), "CHEAP")
    assert r["ok"] and r["direction"] == "CE" and r["caveats"] == []
    assert "cheap" in r["reason"].lower()


def test_rejected_when_oi_disagrees_strict():
    cfg.REQUIRE_GAP_OI_AGREE = True
    r = mc.evaluate(_gap("CE"), _oi("PE"), "CHEAP")
    assert r["ok"] is False


def test_expensive_iv_caveat_not_block_by_default():
    cfg.BLOCK_EXPENSIVE_IV = False
    r = mc.evaluate(_gap("PE"), _oi("PE"), "EXPENSIVE")
    assert r["ok"] is True and any("EXPENSIVE" in c for c in r["caveats"])


def test_expensive_iv_blocks_when_configured():
    cfg.BLOCK_EXPENSIVE_IV = True
    r = mc.evaluate(_gap("CE"), _oi("CE"), "EXPENSIVE")
    assert r["ok"] is False
    cfg.BLOCK_EXPENSIVE_IV = False


def test_no_gap_means_no_trade():
    r = mc.evaluate({}, _oi("CE"), "CHEAP")
    assert r["ok"] is False


def test_strike_from_discount_list_else_fallback():
    rows = [{"symbol": "RELIANCE", "type": "CE", "strike": 2600, "entry": 45,
             "stop_loss": 38, "t1": 56, "t2": 65, "expiry": "2026-06-25"}]
    hit = mc.decide_strike("RELIANCE", "CE", rows)
    assert hit["source"] == "discount_list" and hit["strike"] == 2600
    miss = mc.decide_strike("INFY", "CE", rows)
    assert miss["source"] in ("atm", "otm1") and miss["strike"] is None


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
