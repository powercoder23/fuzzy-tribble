"""
paper_trader.py — Intraday paper-trading engine for the volatility-only
discount scanner.

What it does
------------
1. Takes the top-N "Volatility Expansion Play" signals from a scan and opens
   paper trades (one per symbol+strike+side per day, capped per day, no entries
   after INTRADAY["no_entry_after"]).
2. On each 5-min monitor tick, re-prices every open paper trade against the
   *actual* option LTP and runs the exit state machine:
       SL (-15%)            -> full exit
       T1 (+25%)            -> book t1_book_fraction; move runner stop to breakeven
       T2 (+45%)            -> exit runner
       breakeven (post-T1)  -> exit runner at entry
       15:20 square-off     -> force-close remainder at last price
3. Persists everything to paper_trades.db (SQLite).
4. Sends pro-level per-signal Telegram alerts + an EOD realized-P&L summary.

P&L is tracked on a 1-lot basis in premium points and rupees (points*lot_size)
plus a weighted % of the entry premium.

The exit math (`apply_tick`) is a pure function over a plain dict so it can be
unit-tested with synthetic price paths — no DB or API needed.
"""

import os
import logging
import sqlite3
from datetime import datetime

import notifications

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

def new_trade_runtime(entry, sl, t1, t2, t1_book_fraction, lot_size=1):
    """Return the mutable runtime fields for a fresh open paper trade."""
    return {
        "entry": float(entry),
        "sl": float(sl),
        "t1": float(t1),
        "t2": float(t2),
        "t1_book_fraction": float(t1_book_fraction),
        "lot_size": int(lot_size or 1),
        "status": "open",
        "t1_done": 0,
        "qty_frac": 1.0,
        "booked_points": 0.0,
        "runner_stop": float(sl),   # before T1 the whole position uses the hard SL
        "last_price": float(entry),
        "exit_reason": None,
        "realized_points": None,
        "realized_pct": None,
        "realized_rupees": None,
    }


def _book(trade, frac, price):
    """Realize `frac` of the position at `price` (premium points)."""
    frac = min(frac, trade["qty_frac"])
    if frac <= 0:
        return
    trade["booked_points"] += frac * (price - trade["entry"])
    trade["qty_frac"] = max(trade["qty_frac"] - frac, 0.0)


def _finalize(trade, reason):
    trade["status"] = "closed"
    trade["exit_reason"] = reason
    pts = trade["booked_points"]
    trade["realized_points"] = round(pts, 4)
    trade["realized_pct"] = round(pts / trade["entry"] * 100.0, 2) if trade["entry"] else 0.0
    trade["realized_rupees"] = round(pts * trade["lot_size"], 2)


def apply_tick(trade, last_price, square_off=False):
    """Advance a paper trade by one observed `last_price`. Mutates `trade`.

    Returns a list of event tags among {"T1","T2","SL","BE","TIME"}.
    Fills are modelled at the level price (sl/t1/t2/runner_stop) and at
    last_price for the time square-off — a standard paper-trade simplification.
    """
    events = []
    if trade.get("status") != "open":
        return events

    last_price = float(last_price)
    trade["last_price"] = last_price
    entry = trade["entry"]

    # --- Phase 1: before T1 -------------------------------------------------
    if not trade["t1_done"]:
        if last_price <= trade["sl"]:
            _book(trade, 1.0, trade["sl"])
            _finalize(trade, "SL")
            events.append("SL")
            return events
        if last_price >= trade["t1"]:
            _book(trade, trade["t1_book_fraction"], trade["t1"])
            trade["t1_done"] = 1
            trade["runner_stop"] = entry           # move runner stop to breakeven
            events.append("T1")
            # fall through: a gap could also fill T2 on the same tick

    # --- Phase 2: runner (post-T1) -----------------------------------------
    if trade["status"] == "open" and trade["t1_done"] and trade["qty_frac"] > 1e-9:
        if last_price >= trade["t2"]:
            _book(trade, trade["qty_frac"], trade["t2"])
            _finalize(trade, "T2")
            events.append("T2")
            return events
        if last_price <= trade["runner_stop"]:
            _book(trade, trade["qty_frac"], trade["runner_stop"])
            _finalize(trade, "Runner BE")
            events.append("BE")
            return events

    # --- Forced square-off (15:20) -----------------------------------------
    if square_off and trade["status"] == "open":
        _book(trade, trade["qty_frac"], last_price)
        _finalize(trade, "Time 15:20")
        events.append("TIME")

    return events


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
    status TEXT, t1_done INTEGER DEFAULT 0, qty_frac REAL DEFAULT 1.0,
    booked_points REAL DEFAULT 0.0, runner_stop REAL, last_price REAL,
    exit_reason TEXT, realized_points REAL, realized_pct REAL, realized_rupees REAL
);
"""

_RUNTIME_FIELDS = (
    "status", "t1_done", "qty_frac", "booked_points", "runner_stop",
    "last_price", "exit_reason", "realized_points", "realized_pct",
    "realized_rupees", "closed_at",
)


class PaperTradeBook:
    """SQLite-backed store for intraday paper trades."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.execute(_SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
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

    def open_trade(self, signal, now=None):
        """Insert a new paper trade from a signal dict. Returns the row id."""
        now = now or datetime.now()
        date = now.date().isoformat()
        rt = new_trade_runtime(
            signal["entry"], signal["sl"], signal["t1"], signal["t2"],
            signal.get("t1_book_fraction", TRADE_PLAN["t1_book_fraction"]),
            signal.get("lot_size", 1),
        )
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO paper_trades
                   (date, opened_at, symbol, security_id, exchange_segment, side,
                    strike, expiry, entry, sl, t1, t2, t1_book_fraction, lot_size,
                    score, iv, hv, iv_rank, dte, status, t1_done, qty_frac,
                    booked_points, runner_stop, last_price)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    date, now.strftime("%Y-%m-%d %H:%M:%S"),
                    signal["symbol"], str(signal.get("security_id")),
                    signal.get("exchange_segment"), signal["side"],
                    float(signal["strike"]), signal.get("expiry"),
                    rt["entry"], rt["sl"], rt["t1"], rt["t2"],
                    rt["t1_book_fraction"], rt["lot_size"],
                    signal.get("score"), signal.get("iv"), signal.get("hv"),
                    signal.get("iv_rank"), signal.get("dte"),
                    "open", 0, 1.0, 0.0, rt["runner_stop"], rt["entry"],
                ),
            )
            return cur.lastrowid

    def open_trades(self, date):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE date=? AND status='open'", (date,)
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
    risk_rupees = (entry - float(sig["sl"])) * lot
    target_rupees = (float(sig["t2"]) - entry) * lot
    now_str = datetime.now().strftime("%H:%M:%S")
    lines = [
        f"{dot} <b>PAPER TRADE TAKEN</b> • <b>{sig['symbol']}</b> {side} {_fmt(sig['strike'],0)}",
        f"⏱ Entry time {now_str} • Expiry {sig.get('expiry','?')} • DTE {sig.get('dte','?')}",
        f"Score {_fmt(sig.get('score'),1)} | Spot {_fmt(sig.get('spot'),1)} | "
        f"IVR {_fmt(sig.get('iv_rank'),0)} • IV/HV {_fmt(sig.get('iv'),1)}/{_fmt(sig.get('hv'),1)}",
        f"Entry ₹{_fmt(entry)}  SL ₹{_fmt(sig['sl'])} (-15%)",
        f"T1 ₹{_fmt(sig['t1'])} (+25%, book {int(round(sig.get('t1_book_fraction',0.7)*100))}%)  "
        f"T2 ₹{_fmt(sig['t2'])} (+45%, trail)",
        f"Lot size {lot} • Qty {lot} (1 lot) • Risk ≈ ₹{_fmt(risk_rupees,0)} • Reward ≈ ₹{_fmt(target_rupees,0)}",
        f"Liq OI {sig.get('oi','?')} • Vol {sig.get('volume','?')} • Square-off {INTRADAY['square_off']}",
        "<i>#paper — simulated, no live order</i>",
    ]
    return "\n".join(lines)


def format_fill_update(trade, event):
    """Short HTML note when a paper trade books T1 or closes."""
    label = {
        "T1": "✅ T1 hit — booked partial",
        "T2": "🎯 T2 hit — runner closed",
        "SL": "🛑 SL hit — closed",
        "BE": "➖ Runner stopped at breakeven",
        "TIME": "⏱ Squared off 15:20",
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
        return "hit stop-loss (−15%); premium fell after entry"
    if reason == "T2":
        return "ran to T2 target (+45%); strong directional move"
    if reason == "Runner BE":
        return "booked T1, runner came back to breakeven (move stalled)"
    if reason and reason.startswith("Time"):
        if pct > 5:
            return "closed in profit at square-off (trend held, no target hit)"
        if pct < -5:
            return "closed in loss at square-off (drifted against us)"
        return "flat at square-off — premium barely moved (no momentum)"
    # T1-only partials that never fully closed elsewhere
    if reason == "T1":
        return "booked partial at T1; rest exited later"
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
    hit_rate = (len(wins) / len(closed) * 100.0) if closed else 0.0

    lines = [
        f"<b>📒 Paper EOD — {date}</b>",
        f"Trades {len(trades)} | Closed {len(closed)} | "
        f"Win {len(wins)} / Loss {len(losses)} / Flat {len(flats)} ({hit_rate:.0f}% hit)",
        f"<b>Net ₹{total_rupees:,.0f}</b> (1-lot basis)",
        "─────────────",
    ]
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
    }


def process_signals(book, opportunities, now=None, bot_token=None, chat_id=None,
                    lot_size_fn=None):
    """Open paper trades for the top volatility plays and send alerts.

    `opportunities` may be a pandas DataFrame or a list of dicts. Only
    "Volatility Expansion Play" rows are considered. Honors no_entry_after,
    the daily cap, and per symbol+strike+side dedup.
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

    rows = [r for r in rows if r.get("strategy") == VOLATILITY_STRATEGY]
    rows.sort(key=lambda r: (r.get("score") or 0), reverse=True)

    # Sonar-Laplace direction gate:
    # FLAT        -> skip (whipsaw / no trend)
    # BREAKOUT_UP / REVERSAL_UP   -> force CALL
    # BREAKDOWN   / REVERSAL_DOWN -> force PUT
    # SOFT_BULL / SOFT_BEAR / no data -> keep scanner original side
    try:
        from sonar_laplace_scanner import get_latest_sonar
        _sonar_available = True
    except Exception:
        _sonar_available = False

    cap = INTRADAY["max_signals_per_day"]
    opened = []
    for row in rows:
        if book.count_today(date) >= cap:
            break
        symbol, strike, side = row.get("symbol"), row.get("strike"), row.get("type")
        if symbol is None or strike is None or side is None:
            continue

        # Sonar gate
        if _sonar_available:
            sec_id = str(row.get("security_id") or "")
            sonar  = get_latest_sonar(sec_id) if sec_id else {}
            signal = sonar.get("signal", "")
            bias   = sonar.get("bias", "")
            if signal == "FLAT":
                logger.info("Sonar FLAT — skipping %s", symbol)
                continue
            if signal in ("BREAKOUT_UP", "REVERSAL_UP") and bias == "CE":
                side = "CALL"
            elif signal in ("BREAKDOWN", "REVERSAL_DOWN") and bias == "PE":
                side = "PUT"

        if book.has_trade_today(date, symbol, strike, side):
            continue
        if not row.get("entry") or not row.get("t1"):
            continue
        # Min premium gate — skip cheap/illiquid far-OTM options
        min_prem = INTRADAY.get("min_premium", 5.0)
        if float(row.get("entry") or 0) < min_prem:
            logger.info("Min premium filter — skipping %s %s @ ₹%.2f < ₹%.2f",
                        symbol, side, float(row.get("entry") or 0), min_prem)
            continue
        row = dict(row)
        row["type"] = side
        sig = signal_from_row(row, lot_size_fn)
        book.open_trade(sig, now)
        send_telegram(format_signal_alert(sig), bot_token, chat_id)
        opened.append(sig)
        logger.info("Opened paper trade: %s %s %s", symbol, side, strike)
    return opened


def monitor(book, scanner, now=None, bot_token=None, chat_id=None, square_off=False):
    """Re-price every open paper trade and advance its exit state machine."""
    now = now or datetime.now()
    date = now.date().isoformat()
    closed = []
    for trade in book.open_trades(date):
        quote = None
        try:
            quote = scanner.get_current_option_premium(
                trade["security_id"], trade["exchange_segment"],
                trade["expiry"], trade["strike"], trade["side"],
            )
        except Exception:
            logger.exception("Re-price failed for trade %s", trade.get("id"))
        if not quote or quote.get("last") in (None, 0):
            # No usable price; only act if we must square off (use last known).
            if not square_off:
                continue
            last_price = trade.get("last_price") or trade["entry"]
        else:
            last_price = quote.get("last") or quote.get("mid") or trade["entry"]

        events = apply_tick(trade, last_price, square_off=square_off)
        book.save_runtime(trade, now)
        # Mid-session fill alerts — fire on every actionable event so the
        # trader knows in real time when a SL/T1/T2 is hit.
        for event in ("SL", "T1", "T2", "BE"):
            if event in events:
                send_telegram(
                    format_fill_update(trade, event),
                    bot_token=bot_token,
                    chat_id=chat_id,
                )
        if events and trade.get("status") == "closed":
            closed.append(trade)
    return closed


def run_eod(book, scanner=None, now=None, bot_token=None, chat_id=None):
    """Force square-off any still-open trades, then send the EOD summary."""
    now = now or datetime.now()
    date = now.date().isoformat()
    if scanner is not None:
        monitor(book, scanner, now=now, bot_token=bot_token, chat_id=chat_id, square_off=True)
    summary = format_eod_summary(book.all_trades(date), date)
    send_telegram(summary, bot_token, chat_id)
    return summary
