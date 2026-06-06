# -*- coding: utf-8 -*-
"""
Unit tests for the OI Validation Layer.

Pure / offline: no network, no DB. The data layer is exercised through a fake
provider so classification, scoring, strict/normal gating, fallback behaviour
and message formatting can all be verified deterministically.

Run:
    python -m unittest test_oi_validator -v
    # or
    python test_oi_validator.py
"""

import unittest

import oi_config as cfg
import oi_validator as oiv
from oi_validator import OISnapshot, OIValidator, classify, role_for, score_for


def _snap(price_change, oi_change, available=True):
    """Build an OISnapshot with the given percentage changes."""
    return OISnapshot(
        futures_key="NSE_FO|1",
        price_now=100.0, price_prev=100.0,
        oi_now=100.0, oi_prev=100.0,
        price_change_pct=price_change,
        oi_change_pct=oi_change,
        available=available,
        source="test",
    )


class FakeProvider:
    """Returns a preset snapshot (or None) regardless of symbol."""
    def __init__(self, snap):
        self._snap = snap

    def fetch_snapshot(self, symbol):
        return self._snap


def make_validator(snap):
    return OIValidator(provider=FakeProvider(snap))


class _CfgGuard(unittest.TestCase):
    """Save/restore the mutable config flags around each test."""

    def setUp(self):
        self._saved = {
            k: getattr(cfg, k) for k in (
                "OI_VALIDATION_ENABLED", "OI_STRICT_MODE", "OI_BLOCK_ON_REJECT",
                "OI_MIN_OI_CHANGE_PCT", "OI_MIN_PRICE_CHANGE_PCT",
            )
        }
        # Sensible defaults for most tests: enabled, normal mode, blocking.
        cfg.OI_VALIDATION_ENABLED = True
        cfg.OI_STRICT_MODE = False
        cfg.OI_BLOCK_ON_REJECT = True
        cfg.OI_MIN_OI_CHANGE_PCT = 0.0
        cfg.OI_MIN_PRICE_CHANGE_PCT = 0.0

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(cfg, k, v)


# ---------------------------------------------------------------------------
# Pure classification / scoring
# ---------------------------------------------------------------------------

class TestClassify(unittest.TestCase):
    def test_quadrants(self):
        self.assertEqual(classify(+1, +1), cfg.LONG_BUILDUP)
        self.assertEqual(classify(-1, +1), cfg.SHORT_BUILDUP)
        self.assertEqual(classify(+1, -1), cfg.SHORT_COVERING)
        self.assertEqual(classify(-1, -1), cfg.LONG_UNWINDING)

    def test_flat_counts_as_down_side(self):
        # zero change is treated as the "down/flat" side
        self.assertEqual(classify(0, +1), cfg.SHORT_BUILDUP)
        self.assertEqual(classify(+1, 0), cfg.SHORT_COVERING)
        self.assertEqual(classify(0, 0), cfg.LONG_UNWINDING)


class TestRolesAndScores(unittest.TestCase):
    def test_bullish_roles(self):
        self.assertEqual(role_for("BULLISH", cfg.LONG_BUILDUP), "preferred")
        self.assertEqual(role_for("BULLISH", cfg.SHORT_COVERING), "acceptable")
        self.assertEqual(role_for("BULLISH", cfg.LONG_UNWINDING), "weak")
        self.assertEqual(role_for("BULLISH", cfg.SHORT_BUILDUP), "reject")

    def test_bearish_roles(self):
        self.assertEqual(role_for("BEARISH", cfg.SHORT_BUILDUP), "preferred")
        self.assertEqual(role_for("BEARISH", cfg.LONG_UNWINDING), "acceptable")
        self.assertEqual(role_for("BEARISH", cfg.SHORT_COVERING), "weak")
        self.assertEqual(role_for("BEARISH", cfg.LONG_BUILDUP), "reject")

    def test_scores_match_spec(self):
        self.assertEqual(score_for("preferred"), 100)
        self.assertEqual(score_for("acceptable"), 60)
        self.assertEqual(score_for("weak"), 60)
        self.assertEqual(score_for("reject"), 0)


# ---------------------------------------------------------------------------
# validate() — normal mode
# ---------------------------------------------------------------------------

class TestValidateNormal(_CfgGuard):
    def test_bullish_long_buildup_approved_strong(self):
        v = make_validator(_snap(+1.8, +4.6))
        r = v.validate("RELIANCE", "BULLISH", breakout_level=1540)
        self.assertEqual(r.classification, cfg.LONG_BUILDUP)
        self.assertEqual(r.decision, "ALLOW")
        self.assertEqual(r.score, 100)
        self.assertEqual(r.confidence, "Strong")
        self.assertTrue(r.approved)
        self.assertTrue(r.available)

    def test_bullish_short_covering_accepted(self):
        v = make_validator(_snap(+1.0, -2.0))
        r = v.validate("X", "BULLISH")
        self.assertEqual(r.classification, cfg.SHORT_COVERING)
        self.assertEqual(r.decision, "ALLOW")
        self.assertEqual(r.score, 60)

    def test_bullish_short_buildup_rejected(self):
        v = make_validator(_snap(-1.0, +3.0))
        r = v.validate("X", "BULLISH")
        self.assertEqual(r.classification, cfg.SHORT_BUILDUP)
        self.assertEqual(r.decision, "REJECT")
        self.assertEqual(r.score, 0)
        self.assertFalse(r.approved)

    def test_bullish_long_unwinding_weak_blocked_in_normal(self):
        # "weak" is not in the normal allow-list per the spec.
        v = make_validator(_snap(-1.0, -3.0))
        r = v.validate("X", "BULLISH")
        self.assertEqual(r.classification, cfg.LONG_UNWINDING)
        self.assertEqual(r.role, "weak")
        self.assertEqual(r.decision, "REJECT")

    def test_bearish_short_buildup_approved(self):
        v = make_validator(_snap(-1.5, +5.0))
        r = v.validate("X", "BEARISH")
        self.assertEqual(r.classification, cfg.SHORT_BUILDUP)
        self.assertEqual(r.decision, "ALLOW")
        self.assertEqual(r.score, 100)

    def test_bearish_long_buildup_rejected(self):
        v = make_validator(_snap(+1.5, +5.0))
        r = v.validate("X", "BEARISH")
        self.assertEqual(r.classification, cfg.LONG_BUILDUP)
        self.assertEqual(r.decision, "REJECT")


# ---------------------------------------------------------------------------
# validate() — strict mode
# ---------------------------------------------------------------------------

class TestValidateStrict(_CfgGuard):
    def setUp(self):
        super().setUp()
        cfg.OI_STRICT_MODE = True

    def test_strict_allows_only_preferred(self):
        v = make_validator(_snap(+1.0, +1.0))          # LONG_BUILDUP
        self.assertEqual(v.validate("X", "BULLISH").decision, "ALLOW")

    def test_strict_rejects_acceptable(self):
        v = make_validator(_snap(+1.0, -1.0))          # SHORT_COVERING (acceptable)
        self.assertEqual(v.validate("X", "BULLISH").decision, "REJECT")


# ---------------------------------------------------------------------------
# Fallback / fail-open behaviour
# ---------------------------------------------------------------------------

class TestFallback(_CfgGuard):
    def test_disabled_is_passthrough(self):
        cfg.OI_VALIDATION_ENABLED = False
        v = make_validator(_snap(-1.0, +3.0))          # would be REJECT if enabled
        r = v.validate("X", "BULLISH")
        self.assertEqual(r.decision, "ALLOW")
        self.assertFalse(r.available)
        self.assertEqual(r.classification, cfg.NO_DATA)

    def test_no_snapshot_allows(self):
        v = make_validator(None)
        r = v.validate("X", "BULLISH")
        self.assertEqual(r.decision, "ALLOW")
        self.assertFalse(r.available)
        self.assertEqual(r.reason, "oi_unavailable")

    def test_unavailable_snapshot_allows(self):
        v = make_validator(_snap(-1.0, +3.0, available=False))
        self.assertEqual(v.validate("X", "BULLISH").decision, "ALLOW")

    def test_provider_exception_allows(self):
        class Boom:
            def fetch_snapshot(self, symbol):
                raise RuntimeError("api down")
        v = OIValidator(provider=Boom())
        r = v.validate("X", "BULLISH")
        self.assertEqual(r.decision, "ALLOW")
        self.assertFalse(r.available)

    def test_bad_direction_allows(self):
        v = make_validator(_snap(+1, +1))
        self.assertEqual(v.validate("X", "NONE").decision, "ALLOW")

    def test_deadband_inconclusive_allows(self):
        cfg.OI_MIN_OI_CHANGE_PCT = 2.0
        cfg.OI_MIN_PRICE_CHANGE_PCT = 2.0
        v = make_validator(_snap(+0.1, +0.1))          # tiny moves
        r = v.validate("X", "BULLISH")
        self.assertEqual(r.decision, "ALLOW")
        self.assertEqual(r.reason, "inconclusive")


# ---------------------------------------------------------------------------
# Annotate-only mode (OI_BLOCK_ON_REJECT = False)
# ---------------------------------------------------------------------------

class TestAnnotateOnly(_CfgGuard):
    def test_reject_role_does_not_block_when_annotate_only(self):
        cfg.OI_BLOCK_ON_REJECT = False
        v = make_validator(_snap(-1.0, +3.0))          # SHORT_BUILDUP on bullish
        r = v.validate("X", "BULLISH")
        self.assertEqual(r.classification, cfg.SHORT_BUILDUP)
        self.assertEqual(r.role, "reject")
        self.assertEqual(r.score, 0)
        self.assertEqual(r.decision, "ALLOW")          # not voided


# ---------------------------------------------------------------------------
# State + presentation helpers
# ---------------------------------------------------------------------------

class TestHelpers(_CfgGuard):
    def test_as_state_keys(self):
        v = make_validator(_snap(+1.8, +4.6))
        r = v.validate("RELIANCE", "BULLISH", 1540)
        st = r.as_state()
        self.assertEqual(st["oi_classification"], cfg.LONG_BUILDUP)
        self.assertEqual(st["oi_score"], 100)
        self.assertEqual(st["oi_decision"], "ALLOW")

    def test_log_line(self):
        v = make_validator(_snap(+1.8, +4.6))
        r = v.validate("RELIANCE", "BULLISH", 1540)
        line = oiv.log_line(r)
        self.assertIn("RELIANCE", line)
        self.assertIn("LONG_BUILDUP", line)
        self.assertIn("APPROVED", line)

    def test_format_breakout_batch(self):
        breakouts = [{
            "symbol": "RELIANCE", "direction": "BULLISH",
            "level": 1540.0, "candle_close": 1545.0,
            "oi_classification": cfg.LONG_BUILDUP, "oi_confidence": "Strong",
            "oi_decision": "ALLOW", "oi_available": True,
            "oi_price_change_pct": 1.8, "oi_oi_change_pct": 4.6,
        }]
        msg = oiv.format_breakout_batch(breakouts)
        self.assertIn("RELIANCE", msg)
        self.assertIn("Long Build-up", msg)
        self.assertIn("Strong", msg)

    def test_format_breakout_batch_unavailable(self):
        breakouts = [{
            "symbol": "X", "direction": "BEARISH",
            "level": 100.0, "candle_close": 98.0, "oi_available": False,
        }]
        msg = oiv.format_breakout_batch(breakouts)
        self.assertIn("unavailable", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
