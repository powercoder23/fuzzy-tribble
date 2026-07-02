# -*- coding: utf-8 -*-
"""Engine persistence — decisions + regime tables (engine is their sole writer).

All connections go through collectors.iv_store.connect() (WAL + busy_timeout),
honoring the platform's SQLite contract. Falls back to sqlite3.connect for
unit tests with a bare tmp DB.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from engine import config as cfg
from engine.contracts import Decision, RegimeState

logger = logging.getLogger(__name__)


def _connect(db_path: str):
    try:
        from collectors import iv_store
        return iv_store.connect(db_path)
    except Exception:  # noqa: BLE001 — tests / bare envs
        return sqlite3.connect(db_path)


def ensure_tables(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {cfg.DECISIONS_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts DATETIME NOT NULL,
                symbol TEXT NOT NULL, security_id TEXT NOT NULL,
                status TEXT NOT NULL,             -- EMITTED | REJECTED | WATCH
                direction TEXT, score REAL, grade TEXT,
                trigger_kind TEXT, trigger_quality REAL,
                factor_json TEXT, gate_json TEXT, breakdown_json TEXT,
                reject_reason TEXT, why TEXT, formula_ver TEXT,
                UNIQUE(security_id, ts)
            )""")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_dec_sid_ts "
                     f"ON {cfg.DECISIONS_TABLE}(security_id, ts)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_dec_status_ts "
                     f"ON {cfg.DECISIONS_TABLE}(status, ts)")
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {cfg.REGIME_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts DATETIME NOT NULL,
                posture TEXT, lean TEXT, vix REAL, breadth_pct REAL,
                index_slope_pct REAL, size_mult REAL, reasons TEXT
            )""")
        conn.commit()


def persist_regime(db_path: str, r: RegimeState, ts: str | None = None) -> None:
    ts = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect(db_path) as conn:
        conn.execute(
            f"""INSERT INTO {cfg.REGIME_TABLE}
                (ts, posture, lean, vix, breadth_pct, index_slope_pct, size_mult, reasons)
                VALUES (?,?,?,?,?,?,?,?)""",
            (ts, r.posture, r.lean, r.vix, r.breadth_pct,
             r.index_slope_pct, r.size_mult, "; ".join(r.reasons)))
        conn.commit()


def persist_decisions(db_path: str, decisions: list[Decision],
                      ts: str | None = None) -> int:
    import json
    ts = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with _connect(db_path) as conn:
        for d in decisions:
            cur = conn.execute(
                f"""INSERT INTO {cfg.DECISIONS_TABLE}
                    (ts, symbol, security_id, status, direction, score, grade,
                     trigger_kind, trigger_quality, factor_json, gate_json,
                     breakdown_json, reject_reason, why, formula_ver)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(security_id, ts) DO NOTHING""",
                (ts, d.symbol, str(d.security_id), d.status, d.direction,
                 d.score, d.grade,
                 d.trigger.kind if d.trigger else None,
                 d.trigger.quality if d.trigger else None,
                 d.factor_json(), d.gate_json(), json.dumps(d.breakdown),
                 d.reject_reason, d.why, d.formula_ver))
            n += cur.rowcount
        conn.commit()
    logger.info("engine.store: persisted %d decisions", n)
    return n


def latest_decision_for(db_path: str, security_id: str,
                        max_age_min: float | None = None) -> dict:
    """Latest EMITTED decision for one symbol — the entry_gate (P1) surface.
    Empty dict if none, table missing, or the row is older than max_age_min."""
    try:
        with _connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"""SELECT * FROM {cfg.DECISIONS_TABLE}
                    WHERE security_id = ? AND status = 'EMITTED'
                    ORDER BY ts DESC LIMIT 1""",
                (str(security_id),)).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    d = dict(row)
    if max_age_min is not None:
        try:
            ts = datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - ts).total_seconds() > max_age_min * 60:
                return {}
        except (ValueError, TypeError, KeyError):
            return {}
    return d


def latest_decisions(db_path: str, status: str | None = None, limit: int = 50) -> list[dict]:
    """For the cockpit and the entry_gate shim (P1)."""
    q = f"SELECT * FROM {cfg.DECISIONS_TABLE}"
    args: tuple = ()
    if status:
        q += " WHERE status = ?"
        args = (status,)
    q += " ORDER BY ts DESC, score DESC LIMIT ?"
    args += (limit,)
    try:
        with _connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(q, args).fetchall()]
    except sqlite3.OperationalError:
        return []
