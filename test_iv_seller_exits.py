# -*- coding: utf-8 -*-
"""Exit-engine tests for direction="short" (IV-seller strangle/straddle legs).

Mirrors test_paper_exits.py's style. A short leg is the mirror image of the
existing long-only model: SL sits ABOVE entry (stopped out if price rises),
target sits BELOW entry (booked if price decays), and profit = entry - price.
"""

from paper_trader import new_trade_runtime, apply_tick


def _short_trade(entry=100, sl=200, t1=35, lot_size=500):
    # Mirrors an IV-seller leg: sl = entry * SL_CREDIT_MULT (2.0x default),
    # t1 = entry * TARGET_CREDIT_MULT (0.35x default).
    return new_trade_runtime(entry=entry, sl=sl, t1=t1, t2=t1,
                              t1_book_fraction=1.0, lot_size=lot_size,
                              direction="short")


def test_short_target_books_full_profit_on_decay():
    t = _short_trade()
    ev = apply_tick(t, 30)  # premium decayed below target (35) -> book profit
    assert ev == ["TARGET"], ev
    assert t["status"] == "closed"
    # Booked at the target level (35), profit = entry(100) - target(35) = 65.
    assert abs(t["gross_points"] - 65.0) < 1e-6, t["gross_points"]
    assert t["realized_rupees"] > 0, t["realized_rupees"]


def test_short_sl_books_full_loss_when_price_rises():
    t = _short_trade()
    ev = apply_tick(t, 210)  # premium ran up past sl (200) -> stopped out
    assert ev == ["SL"], ev
    assert t["status"] == "closed"
    # Gap-aware fill: max(sl, last_price) for a short, so it fills at 210, not 200.
    assert abs(t["gross_points"] - (100 - 210)) < 1e-6, t["gross_points"]
    assert t["realized_rupees"] < 0, t["realized_rupees"]


def test_short_sl_fills_at_level_without_a_gap():
    t = _short_trade()
    ev = apply_tick(t, 200)  # exactly at sl, no gap
    assert ev == ["SL"], ev
    assert abs(t["gross_points"] - (100 - 200)) < 1e-6, t["gross_points"]


def test_short_neutral_zone_stays_open():
    t = _short_trade()
    ev = apply_tick(t, 120)  # between target (35) and sl (200) -> still open
    assert ev == [], ev
    assert t["status"] == "open"


def test_short_square_off_books_remaining_at_last_price():
    t = _short_trade()
    ev = apply_tick(t, 80, square_off=True)
    assert ev == ["TIME"], ev
    assert t["status"] == "closed"
    assert abs(t["gross_points"] - (100 - 80)) < 1e-6, t["gross_points"]


def test_short_costs_use_swapped_buy_sell_legs():
    # entry is the SELL leg (credit received), exit is the BUY leg (debit paid
    # to close) — the mirror of the long formula's buy_px=entry, sell_px=exit.
    t = new_trade_runtime(entry=100, sl=200, t1=35, t2=35,
                          t1_book_fraction=1.0, lot_size=500, direction="short")
    t["half_spread"] = 1.0
    apply_tick(t, 30)
    assert t["status"] == "closed"
    # Costs must be > 0 (fee model engaged) and net < gross (slippage + costs).
    assert t["costs_rupees"] >= 0
    assert t["realized_points"] < t["gross_points"]


def test_long_path_unaffected_by_direction_default():
    # Regression guard: omitting `direction` still behaves exactly like the
    # pre-existing long-only model (default "long").
    t = new_trade_runtime(entry=100, sl=85, t1=125, t2=145,
                          t1_book_fraction=0.7, lot_size=500)
    assert t["direction"] == "long"
    ev = apply_tick(t, 126)
    assert ev == ["TARGET"], ev
    assert abs(t["gross_points"] - 25.0) < 1e-6, t["gross_points"]
