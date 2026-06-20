# -*- coding: utf-8 -*-
"""Unit tests for trade_suggester.score_candidate (pure, no DB/broker)."""

import trade_suggester as ts


def _f(oi=None, gap=None, smart=None, deliv=None, iv=None):
    def v(b): return None if b is None else {"bias": b, "strength": 1.0}
    return {"oi": v(oi), "gap": v(gap), "smart": v(smart), "deliv": v(deliv), "iv_zone": iv}


def test_no_scanner_support_keeps_base_score():
    r = ts.score_candidate("CE", _f(), discount_score=60)
    assert r["suggestion_score"] == 60.0  # agree_sum 0 -> unchanged
    assert r["n_agree"] == 0 and r["confidence"] == "LOW"


def test_full_agreement_boosts():
    r = ts.score_candidate("CE", _f(oi="CE", gap="CE", smart="CE", deliv="CE"), discount_score=60)
    assert r["suggestion_score"] > 60 and r["confidence"] == "HIGH" and r["n_agree"] == 4


def test_disagreement_lowers_but_never_excludes():
    r = ts.score_candidate("CE", _f(oi="PE", gap="PE", smart="PE", deliv="PE"), discount_score=60)
    assert 0 <= r["suggestion_score"] < 60  # soft: lowered, still present


def test_cheap_iv_beats_expensive():
    cheap = ts.score_candidate("CE", _f(oi="CE"), discount_score=50, vix_regime="NORMAL")
    exp   = ts.score_candidate("CE", _f(oi="CE"), discount_score=50, vix_regime="NORMAL")
    cheap2 = ts.score_candidate("CE", _f(oi="CE", iv="CHEAP"), discount_score=50)
    expv   = ts.score_candidate("CE", _f(oi="CE", iv="EXPENSIVE"), discount_score=50)
    assert cheap2["suggestion_score"] > expv["suggestion_score"]


def test_pe_side_uses_pe_biases():
    r = ts.score_candidate("PE", _f(oi="PE", smart="PE"), discount_score=70)
    assert r["suggestion_score"] > 70 and r["n_agree"] == 2


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
