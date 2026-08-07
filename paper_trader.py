"""
paper_trader.py — Intraday paper-trading engine for the volatility-only
discount scanner.

What it does
------------
1. Takes the top-N "Volatility Expansion Play" signals from a scan and opens
   paper trades (one per symbol+strike+side per day, capped per day, no entries
   after INTRADAY["no_entry_after"]).
2. On each 5-min monitor tick, re-prices every open paper trade against the
   *actual* option LTP and runs the exit state machine (single SL + single
   target, no runner):
       SL               -> full exit at the stop (gap-aware)
       Target           -> full exit at the target (closes 100% immediately)
       15:20 square-off -> force-close anything still open at last price
3. Persists everything to paper_trades.db (SQLite).
4. Sends pro-level per-signal Telegram alerts + an EOD realized-P&L summary.

P&L is tracked on a 1-lot basis in premium points and rupees (points*lot_size)
plus a weighted % of the entry premium.

The exit math (`apply_tick`) is a pure function over a plain dict so it can be
unit-tested with synthetic price paths — no DB or API needed.
"""

import json
import os
import logging
import sqlite3
from datetime import datetime

import notifications
import paper_policy
import scan_log

try:
    from discount_config import INTRADAY, TRADE_PLAN
except Exception:  # keep importable even if config is missing
    INTRADAY = {
        "no_entry_after": "14:00",
        "square_off": "15:20",
        "max_signals_per_day": 5,
    }
    TRADE_PLAN = {"t1_book_fraction": 0.70}

logger = logging.getLogger(__name__)

# Live on the shared data volume (/app/data in Docker) so open paper trades
# survive a mid-session container restart, alongside iv_history.db.
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "paper_trades.db")

VOLATILITY_STRATEGY = "Volatility Expansion Play"


# ---------------------------------------------------------------------------
# Pure exit-state-machine (unit-testable, no DB / no API)
# ---------------------------------------------------------------------------

def new_trade_runtime(entry, sl, t1, t2, t1_book_fraction, lot_size=None, direction="long"):
    """Return the mutable runtime fields for a fresh open paper trade.

    lot_size is the option contract multiplier and is always >= 1 for a real
    F&O booking (even the lot-sizer's degraded fallback of 1). Pure state-machine
    unit tests pass lot_size=None to opt out of the fee model - the explicit
    "no lot context" sentinel introduced for review 2026-07-09 INTEG-1, which
    replaces overloading lot_size == 1 to mean "skip costs".

    direction is "long" (buy premium, the original behaviour: sl below entry,
    profit on price rising) or "short" (sell premium — IV-seller strangle/
    straddle legs: sl above entry, profit on price falling). See `_book`/
    `_finalize`/`apply_tick` for the mirrored math.
    """
    return {
        "entry": float(entry),
        "sl": float(sl),
        "t1": float(t1),
        "t2": float(t2),
        "t1_book_fraction": float(t1_book_fraction),
        "lot_size": (int(lot_size) if lot_size else None),
        "direction": direction or "long",
        "status": "open",
        "t1_done": 0,
        "qty_frac": 1.0,
        "booked_points": 0.0,
        "runner_stop": float(sl),   # before T1 the whole position uses the hard SL
        "last_price": float(entry),
        "peak_price": float(entry),  # most favorable price seen — trailing_config

        "exit_reason": None,
        "realized_points": None,
        "realized_pct": None,
        "realized_rupees": None,
    }


def _book(trade, frac, price):
    """Realize `frac` of the position at `price` (premium points).

    Long: profit when price rises above entry. Short (premium sold): profit
    when price falls below entry — the sign is simply flipped.
    """
    frac = min(frac, trade["qty_frac"])
    if frac <= 0:
        return
    if trade.get("direction") == "short":
        trade["booked_points"] += frac * (trade["entry"] - price)
    else:
        trade["booked_points"] += frac * (price - trade["entry"])
    trade["qty_frac"] = max(trade["qty_frac"] - frac, 0.0)


def _finalize(trade, reason):
    """Close the trade and compute NET realized P&L.

    Honest-economics model (STRATEGY_REVIEW_P1.md §6.1):
      * gross_points     — the raw state-machine P&L (old behaviour)
      * slippage_points  — 2 × half_spread (entry crosses the spread once,
                           exit once; partial exits approximated as one cross)
      * costs_rupees     — full NSE fee schedule via costs.py (brokerage, STT,
                           exchange txn, SEBI, stamp, IPFT, GST)
      * realized_*       — NET of slippage and costs

    Trades without half_spread/lot_size context (e.g. unit-test fixtures)
    degrade gracefully to gross == net with zero costs.
    """
    trade["status"] = "closed"
    trade["exit_reason"] = reason
    _unsubscribe_live_quote(trade.get("id"))
    gross = trade["booked_points"]
    entry = trade["entry"]
    raw_lot = trade.get("lot_size")
    lot = int(raw_lot) if raw_lot else 1

    half_spread = float(trade.get("half_spread") or 0.0)
    slippage = 2.0 * half_spread

    costs_total = 0.0
    # Apply the NSE fee model to EVERY real trade. lot_size is a contract
    # multiplier (>= 1 for F&O, even the lot-sizer's degraded fallback of 1),
    # so `raw_lot is not None` is the real-trade sentinel; only pure
    # state-machine fixtures pass lot_size=None to opt out. (review 2026-07-09
    # INTEG-1: the old `lot > 1` gate silently booked ZERO costs for every
    # trade on a day the sizer fell back to 1, inflating paper P&L precisely
    # when infrastructure was degraded.)
    if raw_lot is not None and entry:
        try:
            import costs as _costs
            if trade.get("direction") == "short":
                # Sell-to-open, buy-to-close: entry is the SELL leg (credit
                # received), the exit is the BUY leg (debit paid to close).
                # gross = entry - exit_price here, so exit_price = entry - gross.
                sell_px = max(entry - half_spread, 0.0)
                buy_px = max((entry - gross) + half_spread, 0.0)
            else:
                buy_px = entry + half_spread
                sell_px = max(entry + gross - half_spread, 0.0)
            # Single SL + single target: every trade is exactly one buy + one
            # sell (no partial book, no runner), so always 2 orders.
            n_orders = 2
            costs_total = _costs.option_trade_costs(buy_px, sell_px, lot, n_orders)["total"]
        except Exception:
            logger.debug("costs unavailable - finalizing without fee model")

    net = gross - slippage
    trade["gross_points"] = round(gross, 4)
    trade["slippage_points"] = round(slippage, 4)
    trade["costs_rupees"] = round(costs_total, 2)
    trade["realized_points"] = round(net, 4)
    trade["realized_pct"] = round(net / entry * 100.0, 2) if entry else 0.0
    trade["realized_rupees"] = round(net * lot - costs_total, 2)


def _apply_trailing(trade, last_price):
    """Ratchet `trade["sl"]` toward the best price seen so far, once the
    trade is up trailing_config.ACTIVATION_PCT on premium. Only ever
    tightens the stop (never loosens it) and never crosses the fixed
    target. Mutates `trade["peak_price"]`/`trade["sl"]` in place.

    Config-gated + fail-soft: any error, or ENABLED=False, leaves the trade
    untouched. Only meaningful for apply_tick's single-leg trades — combo
    legs are evaluated via apply_combo_tick's combined spread SL/target
    instead and never reach this function.
    """
    try:
        import trailing_config
        if not trailing_config.ENABLED:
            return
        entry = float(trade["entry"])
        if not entry:
            return
        short = trade.get("direction") == "short"

        peak = float(trade.get("peak_price", entry))
        peak = min(peak, last_price) if short else max(peak, last_price)
        trade["peak_price"] = peak

        unrealized_pct = ((entry - peak) if short else (peak - entry)) / entry
        if unrealized_pct < trailing_config.ACTIVATION_PCT:
            return

        target = float(trade["t1"])
        giveback = abs(peak - entry) * trailing_config.GIVEBACK_PCT
        if short:
            # sl sits above entry and walks DOWN toward peak+giveback as the
            # trade decays favorably; never let it go below the target.
            candidate = max(peak + giveback, target)
            trade["sl"] = min(float(trade["sl"]), candidate)
        else:
            # sl sits below entry and walks UP toward peak-giveback as the
            # trade runs favorably; never let it exceed the target.
            candidate = min(peak - giveback, target)
            trade["sl"] = max(float(trade["sl"]), candidate)
    except Exception:
        logger.debug("trailing stop update failed for trade %s", trade.get("id"))


def apply_tick(trade, last_price, square_off=False):
    """Advance a paper trade by one observed `last_price`. Mutates `trade`.

    Single SL + single target model (no T1/T2, no runner): the FIRST level the
    price touches closes the WHOLE position. The target field is `trade["t1"]`
    (the DB keeps t1/t2 columns for history, but only t1 is the live target).

    Returns a list of event tags among {"SL","TARGET","TIME"}.

    Fill model (review §3.5):
      * Stops fill at min(level, observed price) — an option premium that GAPS
        through the SL fills at the gapped price, not the level. Filling at the
        level systematically overstated paper P&L versus live.
      * The target fills AT the level (conservative: a gap above it books the
        target, not the gapped price).
      * Prices are 5-min sampled LTPs, so intrabar touches between samples are
        still missed — treat paper results as an estimate, not ground truth.

    direction="short" (premium sold, e.g. IV-seller strangle/straddle legs)
    mirrors every comparison: sl sits ABOVE entry (stopped out if price rises
    through it, gap-aware fill via max()), target sits BELOW entry (booked if
    price decays through it).
    """
    events = []
    if trade.get("status") != "open":
        return events

    last_price = float(last_price)
    trade["last_price"] = last_price
    target = trade["t1"]
    short = trade.get("direction") == "short"

    if not square_off:
        _apply_trailing(trade, last_price)

    # --- Stop-loss: full exit (gap-aware fill) ------------------------------
    sl_hit = (last_price >= trade["sl"]) if short else (last_price <= trade["sl"])
    if sl_hit:
        fill = max(trade["sl"], last_price) if short else min(trade["sl"], last_price)
        _book(trade, 1.0, fill)
        _finalize(trade, "SL")
        events.append("SL")
        return events

    # --- Target: full exit, close 100% of the position immediately ----------
    # This is the fix for "target hit but position stayed open and profit was
    # given back at square-off" — there is no runner to leak the gain.
    target_hit = (last_price <= target) if short else (last_price >= target)
    if target_hit:
        _book(trade, 1.0, target)
        _finalize(trade, "Target")
        events.append("TARGET")
        return events

    # --- Forced square-off (15:20) -----------------------------------------
    if square_off and trade["status"] == "open":
        _book(trade, trade["qty_frac"], last_price)
        _finalize(trade, "Time 15:20")
        events.append("TIME")

    return events


def apply_combo_tick(long_leg, short_leg, long_price, short_price):
    """Evaluate a 2-leg debit-spread COMBO (one long + one short, same
    combo_id) as a single unit — combined SL/target on the spread's net value,
    not either leg's own SL/T1 (see hedge_config.py SPREAD_SL_PCT /
    SPREAD_TARGET_CAPTURE_PCT for the rationale). When triggered, BOTH legs
    are booked and finalized together, at their own observed prices (no
    per-leg gap-fill logic — the trigger is the combined value, so each leg
    just fills at its live LTP the instant the combined level is crossed).

    Mutates both `long_leg` and `short_leg` in place. Returns the trigger
    reason ("SPREAD_SL" / "SPREAD_TARGET"), or None if neither level is hit
    (both legs untouched in that case — the caller falls through to whatever
    handling it wants for a non-triggered combo).
    """
    import hedge_config

    if long_leg.get("status") != "open" or short_leg.get("status") != "open":
        return None

    long_price = float(long_price)
    short_price = float(short_price)
    entry_debit = float(long_leg["entry"]) - float(short_leg["entry"])
    if entry_debit <= 0:
        return None  # not a real debit spread (credit/zero-cost) — skip combo logic

    spread_value = long_price - short_price
    strike_width = abs(float(long_leg["strike"]) - float(short_leg["strike"]))
    sl_level = entry_debit * (1 - hedge_config.SPREAD_SL_PCT)
    max_profit_potential = max(strike_width - entry_debit, 0.0)
    target_level = entry_debit + max_profit_potential * hedge_config.SPREAD_TARGET_CAPTURE_PCT

    reason = None
    if spread_value <= sl_level:
        reason = "SPREAD_SL"
    elif spread_value >= target_level:
        reason = "SPREAD_TARGET"
    if reason is None:
        return None

    long_leg["last_price"] = long_price
    short_leg["last_price"] = short_price
    _book(long_leg, long_leg["qty_frac"], long_price)
    _finalize(long_leg, reason)
    _book(short_leg, short_leg["qty_frac"], short_price)
    _finalize(short_leg, reason)
    return reason


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, opened_at TEXT, closed_at TEXT,
    symbol TEXT, security_id TEXT, exchange_segment TEXT,
    side TEXT, strike REAL, expiry TEXT,
    entry REAL, sl REAL, t1 REAL, t2 REAL,
    t1_book_fraction REAL, lot_size INTEGER,
    score REAL, iv REAL, hv REAL, iv_rank REAL, dte INTEGER,
    strategy TEXT,
    status TEXT, t1_done INTEGER DEFAULT 0, qty_frac REAL DEFAULT 1.0,
    booked_points REAL DEFAULT 0.0, runner_stop REAL, last_price REAL,
    exit_reason TEXT, realized_points REAL, realized_pct REAL, realized_rupees REAL,
    half_spread REAL, gross_points REAL, slippage_points REAL,
    costs_rupees REAL, factors_json TEXT,
    direction TEXT DEFAULT 'long', combo_id TEXT, spot REAL, peak_price REAL
);
"""

_RUNTIME_FIELDS = (
    "status", "t1_done", "qty_frac", "booked_points", "runner_stop",
    "last_price", "exit_reason", "realized_points", "realized_pct",
    "realized_rupees", "closed_at", "gross_points", "slippage_points",
    "costs_rupees",
    # sl/peak_price ratchet via trailing_config (_apply_trailing) — must be
    # persisted each tick or the next monitor() re-read from the DB would
    # silently discard the trailing update and re-load the original fixed SL.
    "sl", "peak_price",
)

# Additive columns for DBs created before they existed (see _migrate).
_MIGRATE_COLUMNS = {
    "strategy":        "TEXT",
    "half_spread":     "REAL",
    "gross_points":    "REAL",
    "slippage_points": "REAL",
    "costs_rupees":    "REAL",
    "factors_json":    "TEXT",
    "direction":       "TEXT DEFAULT 'long'",
    "combo_id":        "TEXT",
    # Spot at signal time — needed to estimate the naked-short margin a hedge
    # leg would otherwise require (see hedge.py estimate_naked_short_margin).
    "spot":            "REAL",
    # Most favorable price seen — trailing_config.py's ratchet.
    "peak_price":      "REAL",
}


class PaperTradeBook:
    """SQLite-backed store for intraday paper trades."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.execute(_SCHEMA)
        self._migrate()

    def _migrate(self):
        """Additive, idempotent migrations for DBs created before a column
        existed (the prod DB is a long-lived volume)."""
        with self._conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_trades)")}
            for col, col_type in _MIGRATE_COLUMNS.items():
                if col not in cols:
                    conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {col_type}")

    def _conn(self):
        # Accessed from both the scan thread (booking) and the monitor thread
        # (re-pricing) — WAL + busy_timeout prevent writer starvation/locks.
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def has_trade_today(self, date, symbol, strike, side):
        with self._conn() as conn:
            row = conn.execute(
                """SELECT 1 FROM paper_trades
                   WHERE date=? AND symbol=? AND ABS(strike-?)<1e-6 AND side=? LIMIT 1""",
                (date, symbol, float(strike), side),
            ).fetchone()
        return row is not None

    def count_today(self, date):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM paper_trades WHERE date=?", (date,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def count_symbol_today(self, date, symbol):
        """How many paper trades already booked for this underlying today
        (across all strikes/sides/strategies). Used for the per-symbol/day cap
        so one symbol can't eat every slot via different strikes."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM paper_trades WHERE date=? AND symbol=?",
                (date, symbol),
            ).fetchone()
        return int(row["n"]) if row else 0

    def open_trade(self, signal, now=None):
        """Insert a new paper trade from a signal dict. Returns the row id, or
        None if paper_policy refuses the strategy.

        This is the single INSERT into paper_trades, so it is the LAST line of
        defence for the allowlist — callers check first (so they can skip the
        alert and the scan_log 'booked' row), but a caller that forgets must
        still not be able to write. See paper_policy for why the switch is
        here rather than in each strategy.
        """
        strategy = signal.get("strategy", VOLATILITY_STRATEGY)
        if not paper_policy.allows(strategy):
            logger.warning(
                "paper policy: refused INSERT for [%s] %s %s %s — allowed: %s",
                strategy, signal.get("symbol"), signal.get("strike"),
                signal.get("side"), paper_policy.describe())
            return None
        now = now or datetime.now()
        date = now.date().isoformat()
        rt = new_trade_runtime(
            signal["entry"], signal["sl"], signal["t1"], signal["t2"],
            signal.get("t1_book_fraction", TRADE_PLAN["t1_book_fraction"]),
            signal.get("lot_size", 1),
            signal.get("direction", "long"),
        )
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO paper_trades
                   (date, opened_at, symbol, security_id, exchange_segment, side,
                    strike, expiry, entry, sl, t1, t2, t1_book_fraction, lot_size,
                    score, iv, hv, iv_rank, dte, strategy, status, t1_done, qty_frac,
                    booked_points, runner_stop, last_price,
                    half_spread, factors_json, direction, combo_id, spot, peak_price)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    date, now.strftime("%Y-%m-%d %H:%M:%S"),
                    signal["symbol"], str(signal.get("security_id")),
                    signal.get("exchange_segment"), signal["side"],
                    float(signal["strike"]), signal.get("expiry"),
                    rt["entry"], rt["sl"], rt["t1"], rt["t2"],
                    rt["t1_book_fraction"], rt["lot_size"],
                    signal.get("score"), signal.get("iv"), signal.get("hv"),
                    signal.get("iv_rank"), signal.get("dte"),
                    signal.get("strategy", VOLATILITY_STRATEGY),
                    "open", 0, 1.0, 0.0, rt["runner_stop"], rt["entry"],
                    signal.get("half_spread"), signal.get("factors_json"),
                    rt["direction"], signal.get("combo_id"), signal.get("spot"),
                    rt["peak_price"],
                ),
            )
            trade_id = cur.lastrowid
        _subscribe_live_quote(trade_id, signal)
        return trade_id

    def open_trades(self, date):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE status = 'open' AND date <= ?", (date,)
            ).fetchall()
        return [dict(r) for r in rows]

    def all_trades(self, date):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE date=? ORDER BY score DESC", (date,)
            ).fetchall()
        return [dict(r) for r in rows]

    def save_runtime(self, trade, now=None):
        """Persist the mutable runtime fields after an apply_tick()."""
        now = now or datetime.now()
        if trade.get("status") == "closed" and not trade.get("closed_at"):
            trade["closed_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        sets = ", ".join(f"{f}=?" for f in _RUNTIME_FIELDS)
        vals = [trade.get(f) for f in _RUNTIME_FIELDS] + [trade["id"]]
        with self._conn() as conn:
            conn.execute(f"UPDATE paper_trades SET {sets} WHERE id=?", vals)


# ---------------------------------------------------------------------------
# Telegram formatting + send
# ---------------------------------------------------------------------------

def _fmt(v, nd=2, dash="N/A"):
    try:
        if v is None:
            return dash
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return dash


def format_signal_alert(sig):
    """Pro-level per-signal HTML alert for one Volatility Expansion Play."""
    side = "CE" if str(sig.get("side", "")).upper() in ("CALL", "CE") else "PE"
    dot = "🟢" if side == "CE" else "🔴"
    entry = float(sig["entry"])
    lot = int(sig.get("lot_size", 1) or 1)
    target = float(sig["t1"])
    risk_rupees = (entry - float(sig["sl"])) * lot
    target_rupees = (target - entry) * lot
    tgt_pct = ((target - entry) / entry * 100.0) if entry else 0.0
    now_str = datetime.now().strftime("%H:%M:%S")
    lines = [
        f"{dot} <b>PAPER TRADE TAKEN</b> • <b>{sig['symbol']}</b> {side} {_fmt(sig['strike'],0)}",
        f"⏱ Entry time {now_str} • Expiry {sig.get('expiry','?')} • DTE {sig.get('dte','?')}",
        f"Score {_fmt(sig.get('score'),1)} | Spot {_fmt(sig.get('spot'),1)} | "
        f"IVR {_fmt(sig.get('iv_rank'),0)} • IV/HV {_fmt(sig.get('iv'),1)}/{_fmt(sig.get('hv'),1)}",
        f"Entry ₹{_fmt(entry)}  SL ₹{_fmt(sig['sl'])}",
        f"Target ₹{_fmt(target)} ({'+' if tgt_pct >= 0 else ''}{tgt_pct:.0f}%, full exit)",
        f"Lot size {lot} • Qty {lot} (1 lot) • Risk ≈ ₹{_fmt(risk_rupees,0)} • Reward ≈ ₹{_fmt(target_rupees,0)}",
        f"Liq OI {sig.get('oi','?')} • Vol {sig.get('volume','?')} • Square-off {INTRADAY['square_off']}",
        "<i>#paper — simulated, no live order</i>",
    ]
    return "\n".join(lines)


def format_fill_update(trade, event):
    """Short HTML note when a paper trade books T1 or closes."""
    label = {
        "TARGET": "🎯 Target hit — full exit",
        "SL": "🛑 SL hit — closed",
        "TIME": "⏱ Squared off 15:20",
        "RISK_EXIT": "🚪 Auto-exit — risk contradiction",
        "SPREAD_TARGET": "🎯 Spread target hit — both legs closed",
        "SPREAD_SL": "🛑 Spread SL hit — both legs closed",
    }.get(event, event)
    side = "CE" if str(trade.get("side", "")).upper() in ("CALL", "CE") else "PE"
    bits = [f"{label} • <b>{trade['symbol']}</b> {side} {_fmt(trade['strike'],0)} @ ₹{_fmt(trade['last_price'])}"]
    if trade.get("status") == "closed":
        bits.append(
            f"Realized {_fmt(trade.get('realized_pct'),1)}% "
            f"(₹{_fmt(trade.get('realized_rupees'),0)}/lot) • {trade.get('exit_reason')}"
        )
    return "\n".join(bits)


def _hhmmss(ts):
    """Pull HH:MM:SS out of a stored 'YYYY-mm-dd HH:MM:SS' string."""
    if not ts:
        return "—"
    s = str(ts)
    return s[11:19] if len(s) >= 19 else s


def _why(trade):
    """Plain-language reason for how a trade ended (or why it didn't move)."""
    reason = trade.get("exit_reason")
    pct = trade.get("realized_pct") or 0.0
    if trade.get("status") == "open":
        return "still open at EOD"
    if reason == "SL":
        return "hit stop-loss; premium fell after entry"
    if reason in ("Target", "Target full", "T1", "T2"):
        # "Target full"/T1/T2 are legacy exit reasons on pre-change rows.
        return "hit target; booked 100% (full exit)"
    if reason == "Runner BE":  # legacy rows only
        return "runner came back to breakeven (move stalled)"
    if reason and reason.startswith("Time"):
        if pct > 5:
            return "closed in profit at square-off (trend held, no target hit)"
        if pct < -5:
            return "closed in loss at square-off (drifted against us)"
        return "flat at square-off — premium barely moved (no momentum)"
    return reason or "—"


def format_eod_summary(trades, date):
    """EOD realized-P&L HTML summary with per-trade lot/time/entry/exit/reason."""
    closed = [t for t in trades if t.get("status") == "closed"]
    if not trades:
        return f"<b>📒 Paper EOD — {date}</b>\nNo paper trades were taken today."

    wins = [t for t in closed if (t.get("realized_rupees") or 0) > 0]
    losses = [t for t in closed if (t.get("realized_rupees") or 0) < 0]
    flats = [t for t in closed if (t.get("realized_rupees") or 0) == 0]
    total_rupees = sum((t.get("realized_rupees") or 0) for t in closed)
    total_costs = sum((t.get("costs_rupees") or 0) for t in closed)
    total_slip = sum(
        (t.get("slippage_points") or 0) * (t.get("lot_size") or 1) for t in closed
    )
    hit_rate = (len(wins) / len(closed) * 100.0) if closed else 0.0

    lines = [
        f"<b>📒 Paper EOD — {date}</b>",
        f"Trades {len(trades)} | Closed {len(closed)} | "
        f"Win {len(wins)} / Loss {len(losses)} / Flat {len(flats)} ({hit_rate:.0f}% hit)",
        f"<b>Net ₹{total_rupees:,.0f}</b> (1-lot, after costs)",
    ]
    if total_costs or total_slip:
        lines.append(
            f"Frictions: charges ₹{total_costs:,.0f} + spread ₹{total_slip:,.0f} "
            f"(already deducted)"
        )

    # Per-strategy breakdown (only when more than one strategy traded today).
    by_strat: dict = {}
    for t in trades:
        s = t.get("strategy") or VOLATILITY_STRATEGY
        agg = by_strat.setdefault(s, {"n": 0, "rupees": 0.0})
        agg["n"] += 1
        agg["rupees"] += (t.get("realized_rupees") or 0)
    if len(by_strat) > 1:
        for s, agg in sorted(by_strat.items(), key=lambda kv: kv[1]["rupees"], reverse=True):
            lines.append(f"  • {s}: {agg['n']} trade(s), ₹{agg['rupees']:,.0f}")

    lines.append("─────────────")
    for t in sorted(trades, key=lambda x: (x.get("realized_rupees") or 0), reverse=True):
        side = "CE" if str(t.get("side", "")).upper() in ("CALL", "CE") else "PE"
        rr = t.get("realized_rupees")
        pct = t.get("realized_pct")
        lot = int(t.get("lot_size", 1) or 1)
        tag = "🟩" if (rr or 0) > 0 else "🟥" if (rr or 0) < 0 else "⬜"
        # Header line: instrument + result
        lines.append(
            f"{tag} <b>{t['symbol']} {side} {_fmt(t['strike'],0)}</b> "
            f"→ {_fmt(pct,1)}% (₹{_fmt(rr,0)})"
        )
        # Detail line: lot, times, prices
        lines.append(
            f"    lot {lot} • in {_hhmmss(t.get('opened_at'))} @ ₹{_fmt(t['entry'])} "
            f"→ out {_hhmmss(t.get('closed_at'))} @ ₹{_fmt(t.get('last_price'))}"
        )
        # Reason line
        lines.append(f"    why: {_why(t)}")
    lines.append("<i>#paper — simulated on actual option LTP, 1-lot basis</i>")
    return "\n".join(lines)


def send_telegram(text, bot_token=None, chat_id=None, parse_mode="HTML"):
    """Send via Telegram, falling back to Discord. Returns True on success."""
    # `or None` so empty/unset creds defer to the env inside notify().
    return notifications.notify(
        text,
        bot_token=bot_token or None,
        chat_id=chat_id or None,
        parse_mode=parse_mode,
    )


# ---------------------------------------------------------------------------
# Orchestration (called by main.py)
# ---------------------------------------------------------------------------

def _hhmm(now):
    return now.strftime("%H:%M")


def _risk_rupees(signal) -> float:
    """1-lot rupee risk of a signal. Accepts either the paper-signal shape
    ('sl') or a raw scan row ('stop_loss').

    Long: risk = entry - sl (sl sits below entry). Short (premium sold, e.g.
    IV-seller legs): risk = sl - entry (sl sits above entry — the loss if the
    written option's premium runs up against you)."""
    try:
        entry = float(signal.get("entry") or 0)
        sl = float(signal.get("sl") if signal.get("sl") is not None
                   else signal.get("stop_loss") or 0)
        lot = int(signal.get("lot_size") or 1)
        diff = (sl - entry) if signal.get("direction") == "short" else (entry - sl)
        return max(diff, 0.0) * lot
    except (TypeError, ValueError):
        return 0.0


def _flag_override(key):
    """Raw settings-DB override for a UI flag, or None (fail-open)."""
    try:
        import settings_store
        return settings_store.get_flag_raw(key)
    except Exception:
        return None


def _max_risk_rupees() -> float:
    """Per-trade rupee-risk budget (0/None disables the cap). Settings-DB
    override (MAX_RISK_RUPEES) wins over the discount_config default."""
    try:
        ov = _flag_override("MAX_RISK_RUPEES")
        v = ov if ov is not None else INTRADAY.get("max_risk_rupees")
        return float(v) if v else 0.0
    except Exception:
        return 0.0


def _max_per_symbol_per_day() -> int:
    """Max paper trades per underlying per day (0 disables the cap). Settings-DB
    override (MAX_PER_SYMBOL_PER_DAY) wins over the discount_config default."""
    try:
        ov = _flag_override("MAX_PER_SYMBOL_PER_DAY")
        v = ov if ov is not None else INTRADAY.get("max_per_symbol_per_day")
        return int(float(v)) if v is not None else 0
    except Exception:
        return 0


def _half_spread_from_row(row) -> float:
    """Half the bid/ask spread from the scan row; the honest entry/exit
    slippage estimate. Falls back to a conservative % of entry when quotes
    are missing (STRATEGY_REVIEW_P1.md §6.1)."""
    try:
        bid = float(row.get("bid") or 0)
        ask = float(row.get("ask") or 0)
        if ask > 0 and bid > 0 and ask >= bid:
            return (ask - bid) / 2.0
    except (TypeError, ValueError):
        pass
    fallback_pct = float(os.getenv("PAPER_FALLBACK_SPREAD_PCT", "0.02"))  # 2% full spread
    entry = float(row.get("entry") or 0)
    return entry * fallback_pct / 2.0


def collect_factor_snapshot(row) -> str:
    """JSON snapshot of every factor visible at entry, persisted per trade so
    edge attribution is possible after the fact (STRATEGY_REVIEW_P1.md §5:
    'without that, even 500 paper trades won't tell you WHICH component
    carries the edge'). Fail-open: any unavailable factor is null."""
    sec_id = str(row.get("security_id") or "")
    snap = {
        "score": row.get("score"),
        "iv": row.get("iv"),
        "hv": row.get("hv"),
        "iv_rank": row.get("iv_rank"),
        "spread_half": _half_spread_from_row(row),
        "expected_move_ratio": row.get("expected_move_ratio"),
        "pcr": row.get("pcr_value"),
        "trade_type": row.get("trade_type"),
    }
    try:
        from sonar_laplace_scanner import get_latest_sonar
        s = get_latest_sonar(sec_id) if sec_id else {}
        snap["sonar"] = {k: s.get(k) for k in ("signal", "trend", "bias", "slope_pct")} if s else None
    except Exception:
        snap["sonar"] = None
    try:
        from oi_buildup_scanner import get_latest_buildup
        b = get_latest_buildup(sec_id) if sec_id else {}
        snap["oi_buildup"] = {k: b.get(k) for k in ("classification", "bias", "strength", "oi_chg_pct")} if b else None
    except Exception:
        snap["oi_buildup"] = None
    try:
        from composite_scanner import get_latest_composite
        c = get_latest_composite(sec_id) if sec_id else {}
        snap["composite"] = {k: c.get(k) for k in ("score", "direction", "grade")} if c else None
    except Exception:
        snap["composite"] = None
    try:
        import breadth
        bs = breadth.compute()
        snap["breadth_market_pct"] = getattr(bs, "market_pct", None)
    except Exception:
        snap["breadth_market_pct"] = None
    # Market context — six independent description axes (trend / volatility /
    # liquidity / participation / positioning / breadth).
    #
    # OBSERVATIONAL ONLY. This is persisted so that, after enough paper
    # trades, we can measure whether market context predicts outcomes BEFORE
    # it is ever allowed to influence one. It does not veto this entry, and
    # nothing downstream reads it. get() is fail-open and returns an inert
    # neutral snapshot when the subsystem is off or not yet deployed.
    try:
        import market_context
        snap["market_context"] = market_context.get().as_dict()
    except Exception:
        snap["market_context"] = None
    try:
        return json.dumps(snap, default=str)
    except Exception:
        return "{}"


def signal_from_row(row, lot_size_fn=None):
    """Map a scan opportunity dict/row to a paper-trade signal dict."""
    symbol = row.get("symbol")
    lot = 1
    if lot_size_fn and symbol:
        try:
            lot = int(lot_size_fn(symbol) or 1)
        except Exception:
            lot = 1
    return {
        "half_spread": round(_half_spread_from_row(row), 4),
        "factors_json": collect_factor_snapshot(row),
        "symbol": symbol,
        "security_id": row.get("security_id"),
        "exchange_segment": row.get("exchange_segment"),
        "side": row.get("type"),
        "strike": row.get("strike"),
        "expiry": row.get("expiry"),
        "entry": row.get("entry"),
        "sl": row.get("stop_loss"),
        "t1": row.get("t1"),
        "t2": row.get("t2"),
        "t1_book_fraction": row.get("t1_book_fraction", TRADE_PLAN["t1_book_fraction"]),
        "score": row.get("score"),
        "iv": row.get("iv"),
        "hv": row.get("hv"),
        "iv_rank": row.get("iv_rank"),
        "dte": row.get("dte"),
        "oi": row.get("oi"),
        "volume": row.get("volume"),
        "spot": row.get("spot"),
        "lot_size": lot,
        "strategy": row.get("strategy", VOLATILITY_STRATEGY),
    }


def process_signals(book, opportunities, now=None, bot_token=None, chat_id=None,
                    lot_size_fn=None, hedge_chain_fetcher=None, hedge_n_strikes=None):
    """Open paper trades for the top volatility plays and send alerts.

    `opportunities` may be a pandas DataFrame or a list of dicts. Only
    "Volatility Expansion Play" rows are considered. Honors no_entry_after,
    the daily cap, and per symbol+strike+side dedup.

    hedge_chain_fetcher : optional callable(sig) -> {"oc": {...}, "last_price": n}
        or None. When provided and hedge_config.ENABLED, every opened primary
        also gets an OTM short hedge leg booked as the second side of a debit
        spread (mirrors OrderManager.submit_with_hedge, but inline here since
        discount's batch already passed the shared quality gates before
        reaching this function — the hedge leg itself never needs those gates,
        same as every other strategy's hedge leg).
    hedge_n_strikes : optional int override for how many strikes OTM the hedge
        leg sits (falls back to hedge_config.HEDGE_STRIKES_OTM).
    """
    now = now or datetime.now()
    date = now.date().isoformat()

    if _hhmm(now) >= INTRADAY["no_entry_after"]:
        logger.info("Past no_entry_after (%s); no new paper trades", INTRADAY["no_entry_after"])
        return []

    # Normalize to a list of dicts.
    if hasattr(opportunities, "to_dict"):
        rows = opportunities.to_dict("records")
    else:
        rows = list(opportunities or [])

    # Book EVERY strategy the scanner emits, not only "Volatility Expansion
    # Play". Rows with no explicit tag default to VOLATILITY_STRATEGY at
    # signal_from_row(), so every opportunity can paper-trade.
    rows = [r for r in rows if r]
    rows.sort(key=lambda r: (r.get("score") or 0), reverse=True)

    # Sonar-Laplace direction gate (VETO, never a flip):
    # FLAT                        -> skip (whipsaw / no trend)
    # bullish signal + PUT setup  -> skip (contradiction)
    # bearish signal + CALL setup -> skip (contradiction)
    # agrees / SOFT_* / no data   -> keep scanner original side
    #
    # NOTE: the old behaviour FLIPPED the side ("force CALL") while keeping the
    # row's entry/sl/t1/t2 — computed from the OTHER option's premium. Flipped
    # trades booked with the wrong price plan and fired phantom SL/T1 events
    # (review §3.2). Sides are never mutated any more.
    try:
        from sonar_laplace_scanner import get_latest_sonar
        _sonar_available = True
    except Exception:
        _sonar_available = False

    cap = INTRADAY["max_signals_per_day"]  # 0 = unlimited
    opened = []
    for row in rows:
        if cap and book.count_today(date) >= cap:
            break
        symbol, strike, side = row.get("symbol"), row.get("strike"), row.get("type")
        if symbol is None or strike is None or side is None:
            continue

        # Before the Sonar read and the hedge chain fetch — a strategy that
        # cannot book should not spend broker calls reaching that conclusion.
        _strategy = row.get("strategy", VOLATILITY_STRATEGY)
        if not paper_policy.allows(_strategy):
            logger.info("[%s] not on the paper allowlist (%s) — skipping %s",
                        _strategy, paper_policy.describe(), symbol)
            scan_log.record_decision(
                strategy=_strategy, symbol=symbol, side=side, strike=strike,
                iv_rank=row.get("iv_rank"), score=row.get("score"),
                decision="paper_policy", reason=f"allowed: {paper_policy.describe()}",
            )
            continue

        # Sonar gate — veto only; the side (and its price plan) never changes.
        if _sonar_available:
            sec_id = str(row.get("security_id") or "")
            sonar  = get_latest_sonar(sec_id) if sec_id else {}
            # Stale-guard: only SAME-DAY sonar rows may veto (mirrors
            # OrderManager._check_position_risks). get_latest_sonar returns the
            # latest row EVER, so without this a FLAT/BREAKDOWN persisted at
            # yesterday's close vetoes this morning's entries until sonar
            # writes fresh rows (review 2026-07-09 BUG-2). No data = no veto.
            if str(sonar.get("timestamp", ""))[:10] != date:
                sonar = {}
            signal = sonar.get("signal", "")
            if signal == "FLAT":
                logger.info("Sonar FLAT — skipping %s", symbol)
                scan_log.record_decision(
                    strategy=row.get("strategy", VOLATILITY_STRATEGY), symbol=symbol,
                    side=side, strike=strike, iv_rank=row.get("iv_rank"), score=row.get("score"),
                    decision="sonar_flat", reason="sonar signal FLAT",
                )
                continue
            side_norm = "CALL" if str(side).upper() in ("CALL", "CE") else "PUT"
            bullish = signal in ("BREAKOUT_UP", "REVERSAL_UP")
            bearish = signal in ("BREAKDOWN", "REVERSAL_DOWN")
            if (bullish and side_norm == "PUT") or (bearish and side_norm == "CALL"):
                logger.info("Sonar %s contradicts %s %s — skipping (no side-flip)",
                            signal, symbol, side_norm)
                scan_log.record_decision(
                    strategy=row.get("strategy", VOLATILITY_STRATEGY), symbol=symbol,
                    side=side, strike=strike, iv_rank=row.get("iv_rank"), score=row.get("score"),
                    decision="sonar_contradiction", reason=f"sonar {signal} contradicts {side_norm}",
                )
                continue

        if book.has_trade_today(date, symbol, strike, side):
            continue
        # Per-symbol/day cap — one underlying can't eat every slot via different
        # strikes (e.g. 7 ABCAPITAL strikes in a single day).
        sym_cap = _max_per_symbol_per_day()
        if sym_cap and book.count_symbol_today(date, symbol) >= sym_cap:
            logger.info("Per-symbol cap — %s already has %d trade(s) today (max %d)",
                        symbol, book.count_symbol_today(date, symbol), sym_cap)
            scan_log.record_decision(
                strategy=row.get("strategy", VOLATILITY_STRATEGY), symbol=symbol,
                side=side, strike=strike, iv_rank=row.get("iv_rank"), score=row.get("score"),
                decision="per_symbol_cap", reason=f"{symbol} already has {sym_cap}+ trade(s) today",
            )
            continue
        if not row.get("entry") or not row.get("t1"):
            continue
        # Min premium gate — skip cheap/illiquid far-OTM options
        min_prem = INTRADAY.get("min_premium", 5.0)
        if float(row.get("entry") or 0) < min_prem:
            logger.info("Min premium filter — skipping %s %s @ ₹%.2f < ₹%.2f",
                        symbol, side, float(row.get("entry") or 0), min_prem)
            scan_log.record_decision(
                strategy=row.get("strategy", VOLATILITY_STRATEGY), symbol=symbol,
                side=side, strike=strike, iv_rank=row.get("iv_rank"), score=row.get("score"),
                decision="min_premium", reason=f"entry {row.get('entry')} < {min_prem}",
            )
            continue
        sig = signal_from_row(row, lot_size_fn)
        # Rupee-risk cap — a big-lot cheap option must not risk many multiples of
        # a small-lot one. (entry-sl)*lot_size is the 1-lot risk.
        max_risk = _max_risk_rupees()
        if max_risk:
            risk = _risk_rupees(sig)
            if risk > max_risk:
                logger.info("Risk cap — skipping %s %s: 1-lot risk ₹%.0f > ₹%.0f "
                            "(entry ₹%.2f, sl ₹%.2f, lot %s)",
                            symbol, side, risk, max_risk,
                            float(sig.get("entry") or 0), float(sig.get("sl") or 0),
                            sig.get("lot_size"))
                continue
        hedge_sig = None
        if hedge_chain_fetcher is not None:
            import uuid
            sig["combo_id"] = f"{symbol}_{date}_{uuid.uuid4().hex[:6]}"

            # Determine the hedge outcome BEFORE opening the primary so the
            # reason (booked, or exactly why not) can be persisted into the
            # primary's own factors_json pre-insert — this system's
            # containers don't reliably persist stdout logs, so a silent,
            # systemic hedge-booking failure (as happened for ~half of
            # discount's first hedged batch on 2026-07-30) must be
            # diagnosable from the DB, not require guesswork.
            hedge_reason = None
            try:
                import hedge_config
                import hedge as hedge_mod
                if not hedge_config.ENABLED:
                    hedge_reason = "hedge_config.ENABLED=False"
                else:
                    chain = hedge_chain_fetcher(sig)
                    if not chain:
                        hedge_reason = "chain fetch failed or returned no usable data"
                    else:
                        hedge_sig, hedge_reason = hedge_mod.find_hedge_leg(
                            chain=chain.get("oc"),
                            spot=chain.get("last_price"),
                            primary_strike=float(sig.get("strike") or 0),
                            primary_side=str(sig.get("side") or ""),
                            n_strikes=hedge_n_strikes or hedge_config.HEDGE_STRIKES_OTM,
                            signal_template=sig,
                        )
            except Exception:
                logger.exception("Discount hedge lookup failed (non-fatal) for %s %s", symbol, side)
                hedge_sig, hedge_reason = None, "exception during hedge lookup (see logs)"

            try:
                f = json.loads(sig.get("factors_json") or "{}")
            except (TypeError, ValueError):
                f = {}
            f["hedge_attempt"] = {"booked": bool(hedge_sig), "reason": hedge_reason}
            sig["factors_json"] = json.dumps(f)

        book.open_trade(sig, now)
        send_telegram(format_signal_alert(sig), bot_token, chat_id)
        opened.append(sig)
        logger.info("Opened paper trade: %s %s %s", symbol, side, strike)
        scan_log.record_decision(
            strategy=sig.get("strategy", VOLATILITY_STRATEGY), symbol=symbol,
            side=side, strike=strike, iv_rank=row.get("iv_rank"), score=row.get("score"),
            decision="booked", reason=None, extra={"factors_json": sig.get("factors_json")},
        )

        if hedge_sig:
            hedge_sig["combo_id"] = sig["combo_id"]
            book.open_trade(hedge_sig, now)
            send_telegram(format_signal_alert(hedge_sig), bot_token, chat_id)
            opened.append(hedge_sig)
            logger.info("Opened hedge leg: %s %s %s",
                        symbol, hedge_sig.get("side"), hedge_sig.get("strike"))
        elif hedge_chain_fetcher is not None:
            logger.info("Discount hedge leg not booked for %s %s — %s", symbol, side, hedge_reason)
    return opened


def book_signal(book, signal, now=None, bot_token=None, chat_id=None):
    """Book ONE already-vetted signal into paper_trades.db, regardless of
    strategy. Unlike `process_signals` this applies NO discount-specific logic
    (no Volatility-Play filter, no Sonar side-override, no shared daily cap) —
    the caller owns selection. Enforces the universal guards: entry cutoff,
    per symbol+strike+side dedup, the min-premium floor, and the hard
    max_risk_rupees cap (₹1500 default — same ceiling as the discount path).
    Sends the standard "PAPER TRADE TAKEN" alert. Returns the booked signal
    dict, or None.

    Used by OrderManager.submit_external_signal so non-discount strategies
    (e.g. Break & Bounce) land in the same book / EOD / monitor / risk pipeline
    with their own `strategy` tag.
    """
    now = now or datetime.now()
    date = now.date().isoformat()

    # Checked before anything else so a disallowed strategy costs nothing and,
    # critically, never fires the "PAPER TRADE TAKEN" alert below.
    _strategy = signal.get("strategy", VOLATILITY_STRATEGY)
    if not paper_policy.allows(_strategy):
        logger.info("book_signal: [%s] not on the paper allowlist (%s) — skip %s",
                    _strategy, paper_policy.describe(), signal.get("symbol"))
        scan_log.record_decision(
            strategy=_strategy, symbol=signal.get("symbol"),
            side=signal.get("side"), strike=signal.get("strike"),
            iv_rank=signal.get("iv_rank"), score=signal.get("score"),
            decision="paper_policy", reason=f"allowed: {paper_policy.describe()}",
        )
        return None

    if _hhmm(now) >= INTRADAY["no_entry_after"]:
        logger.info("book_signal: past no_entry_after (%s) — skip %s",
                    INTRADAY["no_entry_after"], signal.get("symbol"))
        return None

    symbol = signal.get("symbol")
    strike = signal.get("strike")
    side   = signal.get("side")
    if symbol is None or strike is None or side is None:
        return None
    if not signal.get("entry") or not signal.get("t1"):
        return None
    if book.has_trade_today(date, symbol, strike, side):
        logger.info("book_signal: %s %s %s already booked today", symbol, strike, side)
        return None

    sym_cap = _max_per_symbol_per_day()
    if sym_cap and book.count_symbol_today(date, symbol) >= sym_cap:
        logger.info("book_signal: per-symbol cap — %s already has %d trade(s) today (max %d)",
                    symbol, book.count_symbol_today(date, symbol), sym_cap)
        scan_log.record_decision(
            strategy=signal.get("strategy", VOLATILITY_STRATEGY), symbol=symbol,
            side=side, strike=strike, iv_rank=signal.get("iv_rank"), score=signal.get("score"),
            decision="per_symbol_cap", reason=f"{symbol} already has {sym_cap}+ trade(s) today",
        )
        return None

    # Per-signal floor override: external strategies (e.g. B&B) own their own
    # affordability/liquidity model — a ₹1.8 NHPC option with a 6,950 lot is a
    # valid B&B trade but sits below the discount path's ₹5 far-OTM-junk floor.
    _ov = signal.get("min_premium")
    min_prem = float(_ov) if _ov is not None else INTRADAY.get("min_premium", 5.0)
    if float(signal.get("entry") or 0) < min_prem:
        logger.info("book_signal: %s %s premium ₹%.2f < min ₹%.2f — skip",
                    symbol, side, float(signal.get("entry") or 0), min_prem)
        scan_log.record_decision(
            strategy=signal.get("strategy", VOLATILITY_STRATEGY), symbol=symbol,
            side=side, strike=strike, iv_rank=signal.get("iv_rank"), score=signal.get("score"),
            decision="min_premium", reason=f"entry {signal.get('entry')} < {min_prem}",
        )
        return None

    # Hard rupee-risk cap — applies to EVERY strategy that lands here (B&B,
    # Vol-Expansion, etc.), not just the discount path. A big-lot cheap option
    # must not risk many multiples of a small-lot one. (entry-sl)*lot_size is
    # the 1-lot risk. 0/None (max_risk_rupees) disables the cap.
    # A signal may opt out with skip_risk_cap=True: the Convex measurement book
    # records signal quality (% edge per grade), where 1-lot rupee affordability
    # (epic E5-1) is orthogonal — honest-economics fields still apply, so
    # realized_pct stays realistic.
    #
    # Per-signal ceiling override, same idiom as `min_premium` above: a strategy
    # that owns its own sizing model declares the budget it actually sized to.
    # Momentum sizes lots from RISK_CONFIG["max_risk_pct"] x CAPITAL (₹4,000 by
    # default) — held to the ₹1,500 shared default it would book almost nothing,
    # and its CSV journal would disagree with the book about what it traded.
    # Anything that does NOT declare one keeps the shared default untouched.
    _risk_ov = signal.get("max_risk_rupees")
    max_risk = (0.0 if signal.get("skip_risk_cap")
                else float(_risk_ov) if _risk_ov is not None
                else _max_risk_rupees())
    if max_risk:
        risk = _risk_rupees(signal)
        if risk > max_risk:
            logger.info("book_signal: risk cap — skipping %s %s: 1-lot risk ₹%.0f > ₹%.0f "
                        "(entry ₹%.2f, sl ₹%.2f, lot %s) [%s]",
                        symbol, side, risk, max_risk,
                        float(signal.get("entry") or 0), float(signal.get("sl") or 0),
                        signal.get("lot_size"), signal.get("strategy", VOLATILITY_STRATEGY))
            scan_log.record_decision(
                strategy=signal.get("strategy", VOLATILITY_STRATEGY), symbol=symbol,
                side=side, strike=strike, iv_rank=signal.get("iv_rank"), score=signal.get("score"),
                decision="risk_cap", reason=f"1-lot risk Rs{risk:.0f} > Rs{max_risk:.0f}",
            )
            return None

    # External strategies (e.g. B&B) may not pre-fill the honest-economics
    # fields — capture them here so every booked trade carries them.
    if signal.get("half_spread") is None:
        signal["half_spread"] = round(_half_spread_from_row(signal), 4)
    if not signal.get("factors_json"):
        signal["factors_json"] = collect_factor_snapshot(signal)

    book.open_trade(signal, now)
    send_telegram(format_signal_alert(signal), bot_token, chat_id)
    logger.info("book_signal: opened %s %s %s [%s]",
                symbol, side, strike, signal.get("strategy", VOLATILITY_STRATEGY))
    scan_log.record_decision(
        strategy=signal.get("strategy", VOLATILITY_STRATEGY), symbol=symbol,
        side=side, strike=strike, iv_rank=signal.get("iv_rank"), score=signal.get("score"),
        decision="booked", reason=None, extra={"factors_json": signal.get("factors_json")},
    )
    return signal


def _combo_pairs(open_trades):
    """Group open trades into 2-leg debit-spread pairs (one long + one short
    sharing a combo_id). Combos with only one leg still open (the other
    already closed independently, or a non-spread combo like iv-seller's
    strangle/straddle where both legs are short) are excluded — those fall
    through to the ordinary per-leg handling below."""
    by_combo: dict = {}
    for t in open_trades:
        cid = t.get("combo_id")
        if cid:
            by_combo.setdefault(cid, []).append(t)
    pairs = {}
    for cid, legs in by_combo.items():
        if len(legs) != 2:
            continue
        long_leg = next((l for l in legs if l.get("direction") != "short"), None)
        short_leg = next((l for l in legs if l.get("direction") == "short"), None)
        if long_leg is not None and short_leg is not None:
            pairs[cid] = (long_leg, short_leg)
    return pairs


def _option_key_for(trade_or_signal):
    """Resolve the Upstox instrument_key for a trade/signal's option contract,
    for market_context live-quote subscription. None on any failure — this
    must never block booking or exits, it's purely an acceleration path."""
    try:
        import upstox_adapter
        symbol = trade_or_signal.get("symbol")
        expiry = trade_or_signal.get("expiry")
        strike = trade_or_signal.get("strike")
        side = str(trade_or_signal.get("side") or "").upper()
        opt_type = "CE" if side in ("CE", "CALL") else "PE"
        if not (symbol and expiry and strike):
            return None
        return upstox_adapter.option_instrument_key(symbol, expiry, float(strike), opt_type)
    except Exception:
        return None


def _subscribe_live_quote(trade_id, signal) -> None:
    """Ask market_context to stream this position's option live (see
    _option_key_for / _requote's fast path). Best-effort: a failure here just
    means this trade falls back to REST monitoring, same as before this
    feature existed."""
    if not trade_id:
        return
    key = _option_key_for(signal)
    if not key:
        return
    try:
        import market_context
        market_context.request_quote(key, owner=f"trade:{trade_id}")
    except Exception:
        pass


def _unsubscribe_live_quote(trade_id) -> None:
    """Release the live-quote request opened in _subscribe_live_quote, once
    a position closes. Best-effort, never raises."""
    if not trade_id:
        return
    try:
        import market_context
        market_context.release_quote(owner=f"trade:{trade_id}")
    except Exception:
        pass


def _requote(scanner, trade):
    """Fetch the latest LTP for one trade. Returns None if unavailable.

    Fast path first: market_context.get_quote() reads a live-streamed price
    (see market_context/service.py's mc_live_quotes, populated for every
    dynamically-subscribed open position — request_quote() is called from
    open_trade()/here on open, release_quote() on close). Falls back to the
    existing REST call exactly as before when unavailable, stale, or the
    subsystem is off — zero behavior change for anything not subscribed.
    """
    key = _option_key_for(trade)
    if key:
        try:
            import market_context
            q = market_context.get_quote(key)
            if q and q.get("ltp"):
                return q["ltp"]
        except Exception:
            pass
    try:
        quote = scanner.get_current_option_premium(
            trade["security_id"], trade["exchange_segment"],
            trade["expiry"], trade["strike"], trade["side"],
        )
    except Exception:
        logger.exception("Re-price failed for trade %s", trade.get("id"))
        return None
    if not quote or quote.get("last") in (None, 0):
        return None
    return quote.get("last") or quote.get("mid")


def monitor(book, scanner, now=None, bot_token=None, chat_id=None, square_off=False):
    """Re-price every open paper trade and advance its exit state machine.

    Debit-spread combos (one long + one short leg sharing a combo_id) enter
    and exit TOGETHER: their combined spread value is checked against the
    combined SL/target (hedge_config.SPREAD_SL_PCT / SPREAD_TARGET_CAPTURE_PCT)
    instead of either leg's own individual SL/T1 — see apply_combo_tick.
    Everything else (single-leg trades, and iv-seller's 2-short strangle/
    straddle combos which aren't a defined-risk spread) keeps the ordinary
    per-trade apply_tick behaviour unchanged. square_off always force-closes
    per-leg at last known price regardless of combo status, so both legs of
    a combo still exit in the SAME monitor pass at EOD.
    """
    now = now or datetime.now()
    date = now.date().isoformat()
    closed = []
    open_trades = book.open_trades(date)

    # Re-quote everything up front so the combo check and the per-leg
    # fallback below both see the same tick's prices.
    prices = {}
    for trade in open_trades:
        prices[trade["id"]] = _requote(scanner, trade)

    combo_pairs = _combo_pairs(open_trades) if not square_off else {}
    handled_ids = set()

    for cid, (long_leg, short_leg) in combo_pairs.items():
        lp, sp = prices.get(long_leg["id"]), prices.get(short_leg["id"])
        if lp is None or sp is None:
            continue  # can't evaluate the combined value this tick — skip, try next tick
        reason = apply_combo_tick(long_leg, short_leg, lp, sp)
        if reason is None:
            continue
        handled_ids.add(long_leg["id"])
        handled_ids.add(short_leg["id"])
        for leg in (long_leg, short_leg):
            book.save_runtime(leg, now)
            send_telegram(format_fill_update(leg, reason), bot_token=bot_token, chat_id=chat_id)
            closed.append(leg)

    for trade in open_trades:
        if trade["id"] in handled_ids:
            continue
        last_price = prices.get(trade["id"])
        if last_price is None:
            if not square_off:
                continue
            last_price = trade.get("last_price") or trade["entry"]

        events = apply_tick(trade, last_price, square_off=square_off)
        book.save_runtime(trade, now)
        # Mid-session fill alerts — fire on every actionable event so the
        # trader knows in real time when a SL/T1/T2 is hit.
        for event in ("SL", "TARGET", "TIME"):
            if event in events:
                send_telegram(
                    format_fill_update(trade, event),
                    bot_token=bot_token,
                    chat_id=chat_id,
                )
        if events and trade.get("status") == "closed":
            closed.append(trade)
    return closed


def close_position(book, scanner, trade, reason, now=None,
                   bot_token=None, chat_id=None, notify=True):
    """Force-close ONE open paper trade at its current option LTP (market exit).

    Used by risk-driven auto-exits (e.g. OI contradiction) that must close a
    *specific* position immediately, independent of the SL/T1/T2 state machine.
    Re-prices via the scanner; falls back to the last known price if the quote
    is unavailable. Books the remaining quantity, finalizes with `reason`,
    persists, and (by default) fires one fill alert. Returns the trade if it was
    closed, else None.
    """
    now = now or datetime.now()
    if trade.get("status") != "open":
        return None

    last_price = trade.get("last_price") or trade["entry"]
    try:
        quote = scanner.get_current_option_premium(
            trade["security_id"], trade["exchange_segment"],
            trade["expiry"], trade["strike"], trade["side"],
        )
        if quote and quote.get("last") not in (None, 0):
            last_price = quote.get("last") or quote.get("mid") or last_price
    except Exception:
        logger.exception("close_position re-price failed for trade %s", trade.get("id"))

    trade["last_price"] = float(last_price)
    _book(trade, trade["qty_frac"], float(last_price))
    _finalize(trade, reason)
    book.save_runtime(trade, now)
    if notify:
        send_telegram(format_fill_update(trade, "RISK_EXIT"), bot_token, chat_id)
    return trade


def run_eod(book, scanner=None, now=None, bot_token=None, chat_id=None):
    """Force square-off any still-open trades, then send the EOD summary."""
    now = now or datetime.now()
    date = now.date().isoformat()
    if scanner is not None:
        monitor(book, scanner, now=now, bot_token=bot_token, chat_id=chat_id, square_off=True)
    summary = format_eod_summary(book.all_trades(date), date)
    send_telegram(summary, bot_token, chat_id)
    return summary
