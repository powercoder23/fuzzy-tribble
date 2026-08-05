# -*- coding: utf-8 -*-
"""
test_momentum_paper.py — momentum's hand-off into the shared paper book.

Momentum journalled to data/momentum_trades.csv and alerted, but never called
OrderManager.submit_external_signal, so it was invisible on the dashboard and
absent from every shared analytic. Wiring it in (2026-08-05) has three failure
modes worth pinning:

  * the shared ₹1,500 1-lot risk cap silently rejecting nearly every momentum
    signal, because momentum sizes to RISK_CONFIG["max_risk_pct"] x CAPITAL
    (₹4,000) — the book and the CSV journal would then disagree about what was
    traded, which is worse than not booking at all;
  * the book breaking momentum's OWN path (CSV journal + Telegram alert), which
    worked fine before and must keep working if booking fails;
  * booking without monitoring — a trade that can never fill, stop out or hit a
    target, because momentum stops scanning at 11:30.
"""

import sqlite3

import pytest

import paper_policy
import paper_trader
from momentum_config import CAPITAL, RISK_CONFIG, PAPER
from momentum_strategy import MomentumStrategyRunner, interval_times


@pytest.fixture
def book(tmp_path):
    return paper_trader.PaperTradeBook(db_path=str(tmp_path / "paper_trades.db"))


@pytest.fixture
def runner():
    return MomentumStrategyRunner()


def _strike_data(**kw):
    base = {
        "strike": 1500.0, "ltp": 30.0, "bid": 29.8, "ask": 30.2, "oi": 5000,
        "volume": 900, "iv": 28.5, "delta": 0.35, "spread_pct": 0.01,
        "side": "CE", "option_security_id": "54321", "atm": 1480.0,
        "strike_gap": 20.0,
    }
    base.update(kw)
    return base


def _paper_signal(runner, **kw):
    sig = {"trigger": "ORB", "composite_score": 72.0}
    sig.update(kw.pop("sig", {}))
    # A premium/lot pair momentum can actually AFFORD: 30 x 0.30 x 400 = ₹3,600
    # 1-lot risk, inside its ₹4,000 budget so calculate_lots() returns >= 1.
    premium = kw.pop("premium", 30.0)
    return runner._build_paper_signal(
        sig=sig, symbol="INFY", sec_id=1594, segment="NSE_FNO", side="CE",
        expiry="2026-08-25", strike_data=_strike_data(**kw.pop("strike_data", {})),
        spot=1495.0, premium=premium,
        sl=runner.risk_manager.sl_price(premium),
        tgts=runner.risk_manager.targets(premium),
        lot_size=kw.pop("lot_size", 400),
    )


# --------------------------------------------------------------------------- #
# Signal shape
# --------------------------------------------------------------------------- #
def test_strategy_tag_carries_the_trigger(runner):
    """Convex's idiom: "Momentum-ORB" / "Momentum-VWAP_RECLAIM" so the two
    momentum strategies are separable in the book and in EOD analytics."""
    assert _paper_signal(runner)["strategy"] == "Momentum-ORB"
    assert _paper_signal(runner, sig={"trigger": "vwap_reclaim"})["strategy"] \
        == "Momentum-VWAP_RECLAIM"


def test_untriggered_signal_falls_back_to_the_bare_tag(runner):
    assert _paper_signal(runner, sig={"trigger": ""})["strategy"] == "Momentum"


def test_tag_is_on_the_paper_allowlist(runner):
    """The whole point: momentum must pass the default momentum-only policy."""
    monkey_free = paper_policy._parse(paper_policy.DEFAULT_ALLOWLIST)
    tag = _paper_signal(runner)["strategy"]
    assert any(tag.lower().startswith(p) for p in monkey_free)


def test_security_id_is_the_underlying_not_the_option(runner):
    """The shared monitor re-looks-up the chain from the UNDERLYING id; passing
    the option's id would make every re-price fail."""
    sig = _paper_signal(runner)
    assert sig["security_id"] == 1594
    assert sig["option_security_id"] == "54321"


def test_sl_and_targets_match_momentums_own_risk_model(runner):
    sig = _paper_signal(runner, premium=30.0)
    assert sig["entry"] == 30.0
    assert sig["sl"] == pytest.approx(30.0 * (1 - RISK_CONFIG["sl_pct"]), abs=0.05)
    assert sig["t1"] == pytest.approx(30.0 * RISK_CONFIG["target1_mult"], abs=0.05)
    assert sig["t2"] == pytest.approx(30.0 * RISK_CONFIG["target2_mult"], abs=0.05)


# --------------------------------------------------------------------------- #
# The risk-cap collision
# --------------------------------------------------------------------------- #
def test_declared_budget_matches_momentums_own_sizing(runner):
    """The override must equal the budget calculate_lots() sized to, not a
    number picked to make trades fit."""
    sig = _paper_signal(runner)
    assert sig["max_risk_rupees"] == CAPITAL * RISK_CONFIG["max_risk_pct"]
    assert sig["max_risk_rupees"] == runner.risk_manager.max_risk()


def test_typical_momentum_signal_would_be_rejected_by_the_shared_default(runner, book, monkeypatch):
    """THE REGRESSION this override exists for: ₹30 premium x 400 lot x 30% SL
    = ₹3,600 1-lot risk. Well inside momentum's own ₹4,000 budget, and more
    than double the shared ₹1,500 default."""
    monkeypatch.setattr(paper_policy, "ALLOWLIST", ["momentum"])
    monkeypatch.setattr(paper_trader, "send_telegram", lambda *a, **kw: True)
    monkeypatch.setattr(paper_trader, "_max_risk_rupees", lambda: 1500.0)

    sig = _paper_signal(runner)
    assert paper_trader._risk_rupees(sig) > 1500.0

    without = dict(sig)
    without.pop("max_risk_rupees")
    assert paper_trader.book_signal(book, without) is None      # would be lost
    assert paper_trader.book_signal(book, sig) is not None      # declared budget


@pytest.mark.parametrize("premium,lot_size", [
    (30.0, 400), (12.0, 1100), (5.0, 2000), (95.0, 125), (40.0, 400),
])
def test_declared_cap_never_rejects_a_signal_momentum_could_size(runner, premium, lot_size):
    """The override is not a loosening — it makes the two gates the SAME
    inequality. calculate_lots() needs premium x sl_pct x lot_size <= max_risk()
    for lots >= 1; the book caps (entry - sl) x lot_size against the same
    number, and sl = entry x (1 - sl_pct) makes those expressions identical.
    So any signal momentum can afford is inside the declared cap, and anything
    outside it momentum would have refused upstream as unaffordable.
    """
    sig = _paper_signal(runner, premium=premium, lot_size=lot_size)
    lots = runner.risk_manager.calculate_lots(premium, lot_size)
    affordable = lots >= 1
    within_cap = paper_trader._risk_rupees(sig) <= sig["max_risk_rupees"] + 0.01
    assert affordable == within_cap


def test_override_does_not_weaken_the_cap_for_other_strategies(book, monkeypatch):
    """Strategies that declare nothing must keep the shared ceiling."""
    monkeypatch.setattr(paper_policy, "ALLOWLIST", ["*"])
    monkeypatch.setattr(paper_trader, "send_telegram", lambda *a, **kw: True)
    monkeypatch.setattr(paper_trader, "_max_risk_rupees", lambda: 1500.0)
    sig = {
        "symbol": "X", "strike": 100.0, "side": "CE", "strategy": "Break & Bounce",
        "entry": 40.0, "sl": 28.0, "t1": 72.0, "t2": 120.0, "lot_size": 400,
    }
    assert paper_trader.book_signal(book, sig) is None


def test_override_still_enforces_the_budget_it_declares(book, monkeypatch):
    """A declared budget is a ceiling, not an exemption — 10,000-share lots
    must still be refused."""
    monkeypatch.setattr(paper_policy, "ALLOWLIST", ["momentum"])
    monkeypatch.setattr(paper_trader, "send_telegram", lambda *a, **kw: True)
    sig = {
        "symbol": "X", "strike": 100.0, "side": "CE", "strategy": "Momentum-ORB",
        "entry": 40.0, "sl": 28.0, "t1": 72.0, "t2": 120.0, "lot_size": 10_000,
        "max_risk_rupees": 4000.0,
    }
    assert paper_trader._risk_rupees(sig) > 4000.0
    assert paper_trader.book_signal(book, sig) is None


def test_signal_actually_lands_in_the_book(runner, book, monkeypatch):
    monkeypatch.setattr(paper_policy, "ALLOWLIST", ["momentum"])
    monkeypatch.setattr(paper_trader, "send_telegram", lambda *a, **kw: True)
    assert paper_trader.book_signal(book, _paper_signal(runner)) is not None
    with sqlite3.connect(book.db_path) as conn:
        row = conn.execute(
            "SELECT symbol, strategy, side, entry FROM paper_trades").fetchone()
    assert row == ("INFY", "Momentum-ORB", "CE", 30.0)


# --------------------------------------------------------------------------- #
# Lifecycle — booking without monitoring is worse than not booking
# --------------------------------------------------------------------------- #
def test_monitor_window_is_scheduled_past_the_last_scan(runner):
    """Scans stop at 11:30; positions live to square-off. A monitor that ended
    with the scans would strand every afternoon position."""
    times = interval_times(
        PAPER["monitor_from"], PAPER["monitor_until"],
        PAPER["monitor_interval_min"])
    assert times[0] == PAPER["monitor_from"]
    assert times[-1] <= PAPER["monitor_until"]
    assert max(times) > "11:30"
    assert max(times) < PAPER["square_off"]


def test_bad_monitor_window_schedules_nothing(caplog):
    """A malformed env value must not loop forever building times."""
    assert interval_times("bogus", "15:10", 5) == []
    assert interval_times("15:10", "09:35", 5) == []
    assert interval_times("09:35", "15:10", 0) == []


@pytest.mark.parametrize("method", ["run_monitor", "run_square_off", "run_paper_eod"])
def test_lifecycle_methods_are_noops_when_paper_is_off(runner, monkeypatch, method):
    """MOMENTUM_PAPER_MODE=off must restore the exact pre-2026-08-05 behaviour,
    with no OrderManager built and so no paper_trades.db opened."""
    monkeypatch.setitem(PAPER, "mode", "off")
    called = []
    monkeypatch.setattr(runner, "_get_order_manager",
                        lambda: called.append(1))
    getattr(runner, method)()
    assert called == []


def test_order_manager_is_not_built_at_construction(runner):
    """Importing/constructing must not open the shared book — the backtester,
    the dashboard and these tests all build this object."""
    assert runner._order_manager is None
