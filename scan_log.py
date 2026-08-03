"""scan_log.py — durable, queryable record of every strategy scan's
accept/reject decisions.

Several runner containers only log to stdout (see the paper_trading memory's
"log file gotcha" — logs/*.log go stale even while a container is actively
trading), so reconstructing *why* a candidate was or wasn't booked has been
impossible after the fact except for booked trades (which keep their
factors_json). This gives every decision point a durable row in the shared
paper_trades.db, independent of container log persistence.
"""
import json
import logging
import os
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "paper_trades.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    date TEXT,
    strategy TEXT,
    symbol TEXT,
    side TEXT,
    strike REAL,
    iv_rank REAL,
    score REAL,
    decision TEXT,
    reason TEXT,
    extra_json TEXT
)
"""


def record_decision(strategy, symbol, side, strike, iv_rank, score,
                     decision, reason=None, extra=None, db_path=None):
    """Persist one accept/reject decision. Never raises — a logging failure
    must never block a real trade decision."""
    try:
        now = datetime.now()
        conn = sqlite3.connect(db_path or DB_PATH, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
            conn.execute(
                "INSERT INTO scan_log "
                "(ts, date, strategy, symbol, side, strike, iv_rank, score, decision, reason, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now.isoformat(timespec="seconds"),
                    now.date().isoformat(),
                    strategy,
                    symbol,
                    side,
                    float(strike) if strike is not None else None,
                    float(iv_rank) if iv_rank is not None else None,
                    float(score) if score is not None else None,
                    decision,
                    reason,
                    json.dumps(extra) if extra is not None else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("scan_log.record_decision failed (non-fatal)")
