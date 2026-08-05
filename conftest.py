# -*- coding: utf-8 -*-
"""
conftest.py — shared pytest fixtures.

paper_policy.ALLOWLIST defaults to momentum-only, which is a DEPLOYMENT policy,
not a property of the paper-trading machinery. Tests that exercise exits,
auto-exit, daily-loss lockout and EOD aggregation book under tags like
"Break & Bounce" because that is what those code paths carry in production —
they would all fail against the live default for a reason unrelated to what
they assert. So the policy is opened for the suite, and tested explicitly and
in isolation in test_paper_policy.py.
"""

import sys
import types

import pytest

# ---------------------------------------------------------------------------
# `fcntl` is POSIX-only. upstox_token_manager imports it at module scope for
# the token-refresh file lock, so on a Windows dev box `import discount` — and
# therefore momentum_strategy, break_bounce_strategy, everything downstream —
# fails at collection with ModuleNotFoundError.
#
# The lock is real and matters in the container (concurrent token refresh
# across services); it is meaningless in a single-process test run. So the
# shim is a no-op stub installed ONLY where the real module cannot exist. On
# Linux/CI the genuine fcntl is imported and this does nothing.
# ---------------------------------------------------------------------------
if "fcntl" not in sys.modules:
    try:
        import fcntl  # noqa: F401
    except ModuleNotFoundError:
        _stub = types.ModuleType("fcntl")
        _stub.LOCK_EX = 2
        _stub.LOCK_SH = 1
        _stub.LOCK_UN = 8
        _stub.LOCK_NB = 4
        _stub.flock = lambda *a, **kw: None
        _stub.lockf = lambda *a, **kw: None
        sys.modules["fcntl"] = _stub


@pytest.fixture(autouse=True)
def _allow_all_paper_strategies(monkeypatch):
    import paper_policy
    monkeypatch.setattr(paper_policy, "ALLOWLIST", [paper_policy.ALLOW_ALL])
