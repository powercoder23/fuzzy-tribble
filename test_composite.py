# -*- coding: utf-8 -*-
"""Unit tests for composite_scanner.score_symbol (pure, no DB/broker)."""

import composite_scanner as cs


def _f(oi=None, smart=None, deliv=None, gap=None, iv=None):
    """Build a factors dict. bias strings; strength defaults to 1.0."""
    def v(b): return None if b is None else {"bias": b, "strength": 1.0}
    return {"oi": v(oi), "smart": v(smart), "deliv": v(deliv), "gap": v(gap), "iv_zone": iv}


def test_no_signal_below_min_factors():
    r = cs.score_symbol(_f(oi="CE"))  # only 1 vote, MIN_FACTORS=2
    assert r["direction"] == "NONE" and r["score"] == 0.0


def test_two_agree_ce():
    r = cs.score_symbol(_f(oi="CE", smart="CE"))
    assert r["direction"] == "CE" and r["score"] > 0


def test_opposing_factors_net_out():
    r = cs.score_symbol(_f(oi="CE", smart="PE", deliv="CE", gap="PE"))
    # oi(.30)+deliv(.20)=.50 CE vs smart(.25)+gap(.15)=.40 PE -> net CE, small
    assert r["direction"] == "CE"
    assert r["score"] < cs.score_symbol(_f(oi="CE", smart="CE", deliv="CE", gap="CE"))["score"]


def test_full_confluence_scores_high():
    r = cs.score_symbol(_f(oi="CE", smart="CE", deliv="CE", gap="CE", iv="CHEAP"), vix_regime="CALM")
    assert r["grade"] == "STRONG" and r["direction"] == "CE"


def test_cheap_iv_beats_expensive_iv():
    cheap = cs.score_symbol(_f(oi="CE", smart="CE", iv="CHEAP"))
    exp   = cs.score_symbol(_f(oi="CE", smart="CE", iv="EXPENSIVE"))
    assert cheap["score"] > exp["score"]


def test_elevated_vix_penalises():
    calm = cs.score_symbol(_f(oi="CE", smart="CE"), vix_regime="CALM")
    elev = cs.score_symbol(_f(oi="CE", smart="CE"), vix_regime="ELEVATED")
    assert calm["score"] > elev["score"]


if __name__ == "__main__":
    import sys
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
