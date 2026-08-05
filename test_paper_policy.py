# -*- coding: utf-8 -*-
"""
test_paper_policy.py — the paper-book allowlist.

REGRESSION CONTEXT (2026-08-05)
-------------------------------
Every strategy but momentum was supposed to have stopped paper trading on
2026-08-04. On 2026-08-05 the book still took 51 trades: 46 from
vol-expansion, 4 from convex-engine (-Rs 11,617 realized), 1 from
break-and-bounce. `profiles:` in docker-compose only controls what a fresh
`up` starts — the already-running containers never stopped — and B&B,
directional-IV and IV-seller have no paper flag of their own to switch off.

These tests pin the guarantee that replaced the gating: the allowlist is
enforced at PaperTradeBook.open_trade, the single INSERT, so no strategy can
write regardless of which container is alive.

The suite-wide conftest fixture opens the allowlist for every OTHER test file;
each test here sets it explicitly.
"""

import sqlite3

import pytest

import paper_policy
import paper_trader


@pytest.fixture
def book(tmp_path):
    return paper_trader.PaperTradeBook(db_path=str(tmp_path / "paper_trades.db"))


def _signal(strategy, symbol="INFY"):
    return {
        "symbol": symbol, "strike": 1500.0, "side": "CE", "strategy": strategy,
        "entry": 20.0, "sl": 14.0, "t1": 26.0, "t2": 32.0, "lot_size": 400,
        "security_id": "1", "exchange_segment": "NSE_FNO",
    }


def _set(monkeypatch, raw):
    monkeypatch.setattr(paper_policy, "ALLOWLIST", paper_policy._parse(raw))


def _count(book):
    with sqlite3.connect(book.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def test_default_is_momentum_only():
    """The shipped default, read straight from the module."""
    assert paper_policy._parse(paper_policy.DEFAULT_ALLOWLIST) == ["momentum"]


@pytest.mark.parametrize("strategy,allowed", [
    ("Momentum", True),
    ("Momentum-ORB", True),              # prefix covers a strategy's variants
    ("Momentum [hedge]", True),
    ("momentum", True),                  # case-insensitive
    ("Volatility Expansion Play", False),
    ("Volatility Expansion Play [hedge]", False),
    ("Convex-SONAR_BAND", False),
    ("Break & Bounce", False),
    ("Discounted Premium", False),
])
def test_allowlist_matches_by_prefix(monkeypatch, strategy, allowed):
    _set(monkeypatch, "Momentum")
    assert paper_policy.allows(strategy) is allowed


def test_star_allows_everything(monkeypatch):
    _set(monkeypatch, "*")
    assert paper_policy.allows("Convex-SONAR_BAND") is True
    assert paper_policy.allows("anything at all") is True


def test_empty_allowlist_fails_closed(monkeypatch):
    """A blank or malformed env var must BLOCK, not silently open the book —
    the failure mode of a kill switch has to be 'nothing trades'."""
    _set(monkeypatch, "")
    assert paper_policy.allows("Momentum") is False
    _set(monkeypatch, "   ,  ,")
    assert paper_policy.allows("Momentum") is False


def test_untagged_signal_is_not_momentum(monkeypatch):
    _set(monkeypatch, "Momentum")
    assert paper_policy.allows(None) is False
    assert paper_policy.allows("") is False


def test_multiple_entries(monkeypatch):
    _set(monkeypatch, "Momentum, Break & Bounce")
    assert paper_policy.allows("Break & Bounce [hedge]") is True
    assert paper_policy.allows("Convex-ORB") is False


# --------------------------------------------------------------------------- #
# Enforcement at the INSERT
# --------------------------------------------------------------------------- #
def test_disallowed_strategy_cannot_insert(monkeypatch, book):
    """THE REGRESSION: this is the write that took 51 trades on 2026-08-05."""
    _set(monkeypatch, "Momentum")
    assert book.open_trade(_signal("Volatility Expansion Play")) is None
    assert _count(book) == 0


def test_allowed_strategy_still_inserts(monkeypatch, book):
    _set(monkeypatch, "Momentum")
    assert book.open_trade(_signal("Momentum-ORB")) is not None
    assert _count(book) == 1


def test_book_signal_sends_no_alert_for_a_blocked_strategy(monkeypatch, book):
    """A refused signal must not fire "PAPER TRADE TAKEN" — the alert is what
    the user actually sees, so a silent block that still alerts is worse than
    no block at all."""
    sent = []
    monkeypatch.setattr(paper_trader, "send_telegram",
                        lambda *a, **kw: sent.append(a))
    _set(monkeypatch, "Momentum")
    assert paper_trader.book_signal(book, _signal("Convex-SONAR_BAND")) is None
    assert sent == []
    assert _count(book) == 0


def test_block_is_recorded_in_scan_log(monkeypatch, book):
    """Silent refusals are undebuggable: the reason has to reach the DB."""
    recorded = []
    monkeypatch.setattr(paper_trader.scan_log, "record_decision",
                        lambda **kw: recorded.append(kw))
    _set(monkeypatch, "Momentum")
    paper_trader.book_signal(book, _signal("Break & Bounce"))
    assert [r["decision"] for r in recorded] == ["paper_policy"]
    assert "momentum" in recorded[0]["reason"].lower()


def test_hedge_leg_is_blocked_with_its_primary(monkeypatch, book):
    """Hedge legs carry a '[hedge]' suffix on the same tag. Blocking the
    primary but booking the hedge would leave a naked SHORT option in the
    book — strictly worse than either outcome."""
    _set(monkeypatch, "Momentum")
    primary = _signal("Volatility Expansion Play")
    hedge = dict(_signal("Volatility Expansion Play [hedge]"),
                 strike=1600.0, direction="short")
    assert book.open_trade(primary) is None
    assert book.open_trade(hedge) is None
    assert _count(book) == 0


def test_describe_is_readable(monkeypatch):
    _set(monkeypatch, "Momentum")
    assert paper_policy.describe() == "momentum"
    _set(monkeypatch, "*")
    assert paper_policy.describe() == "all strategies"
    _set(monkeypatch, "")
    assert paper_policy.describe() == "none"
