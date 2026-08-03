# -*- coding: utf-8 -*-
"""
results_store.py — owns data/backtest_results.db (backtest_runs +
backtest_trades). This is the ONLY module that writes to that DB; the
FastAPI routes and the engine both go through here.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

DB_PATH = str(Path(os.path.dirname(__file__)).parent / "data" / "backtest_results.db")


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                strategy     TEXT NOT NULL,
                symbols_json TEXT NOT NULL,
                start_date   TEXT NOT NULL,
                end_date     TEXT NOT NULL,
                params_json  TEXT,
                status       TEXT NOT NULL DEFAULT 'queued',
                progress_pct REAL NOT NULL DEFAULT 0,
                error        TEXT,
                finished_at  TEXT,
                summary_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_trades (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id         INTEGER NOT NULL,
                symbol         TEXT NOT NULL,
                side           TEXT NOT NULL,
                strike         REAL,
                expiry         TEXT,
                entry_ts       TEXT,
                entry_premium  REAL,
                exit_ts        TEXT,
                exit_premium   REAL,
                exit_reason    TEXT,
                pnl_rupees     REAL,
                pnl_pct        REAL,
                lot_size       INTEGER,
                trigger        TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_run ON backtest_trades(run_id)")


def create_run(strategy: str, symbols: list[str], start_date: str, end_date: str,
              params: dict | None = None, db_path: str = DB_PATH) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO backtest_runs (strategy, symbols_json, start_date, end_date, "
            "params_json, status) VALUES (?,?,?,?,?, 'queued')",
            (strategy, json.dumps(symbols), start_date, end_date, json.dumps(params or {})),
        )
        return cur.lastrowid


def update_run(run_id: int, db_path: str = DB_PATH, **fields) -> None:
    """fields: any of status/progress_pct/error/finished_at/summary (dict, auto-json'd)."""
    if "summary" in fields:
        fields["summary_json"] = json.dumps(fields.pop("summary"))
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE backtest_runs SET {cols} WHERE id=?",
                    (*fields.values(), run_id))


def get_run(run_id: int, db_path: str = DB_PATH) -> dict | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM backtest_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["symbols"] = json.loads(d.pop("symbols_json") or "[]")
    d["params"] = json.loads(d.pop("params_json") or "{}")
    d["summary"] = json.loads(d.pop("summary_json") or "null")
    return d


def list_runs(limit: int = 50, db_path: str = DB_PATH) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, created_at, strategy, start_date, end_date, status, "
            "progress_pct, error, finished_at, summary_json FROM backtest_runs "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["summary"] = json.loads(d.pop("summary_json") or "null")
        out.append(d)
    return out


def insert_trades(run_id: int, trades: list[dict], db_path: str = DB_PATH) -> None:
    if not trades:
        return
    init_db(db_path)
    cols = ["symbol", "side", "strike", "expiry", "entry_ts", "entry_premium",
            "exit_ts", "exit_premium", "exit_reason", "pnl_rupees", "pnl_pct",
            "lot_size", "trigger"]
    rows = [(run_id, *(t.get(c) for c in cols)) for t in trades]
    with _connect(db_path) as conn:
        conn.executemany(
            f"INSERT INTO backtest_trades (run_id, {', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * (len(cols) + 1))})", rows,
        )


def get_trades(run_id: int, db_path: str = DB_PATH) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM backtest_trades WHERE run_id=? ORDER BY entry_ts ASC",
            (run_id,)).fetchall()
    return [dict(r) for r in rows]


def delete_run(run_id: int, db_path: str = DB_PATH) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM backtest_trades WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM backtest_runs WHERE id=?", (run_id,))
