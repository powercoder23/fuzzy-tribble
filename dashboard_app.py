# -*- coding: utf-8 -*-
"""
dashboard_app.py — Trading dashboard backend.

Serves the single-page dashboard HTML and JSON APIs that read directly
from iv_history.db and paper_trades.db. Zero broker calls — read-only.

Endpoints
─────────
GET  /                          → dashboard HTML
GET  /api/symbols               → [{symbol, security_id, last_iv, iv_rank}]
GET  /api/iv/{symbol}           → IV history + PCR + spot for chart
GET  /api/iv/{symbol}/intraday  → today's intraday IV ticks
GET  /api/paper-trades          → today's paper trades
GET  /api/paper-trades/history  → last 30 days P&L summary
GET  /api/health                → DB row counts + last update time
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

# ── Config ────────────────────────────────────────────────────────────────── #
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
IV_DB    = DATA_DIR / "iv_history.db"
PT_DB    = DATA_DIR / "paper_trades.db"

app = FastAPI(title="Fuzzy Tribble Dashboard", version="1.0")


# ── DB helpers ───────────────────────────────────────────────────────────────#
def _iv(sql: str, params=()) -> list[dict]:
    if not IV_DB.exists():
        return []
    with sqlite3.connect(IV_DB) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _pt(sql: str, params=()) -> list[dict]:
    if not PT_DB.exists():
        return []
    with sqlite3.connect(PT_DB) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _scalar(db: Path, sql: str, params=(), default=None):
    if not db.exists():
        return default
    with sqlite3.connect(db) as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else default


# ── Symbol list ───────────────────────────────────────────────────────────── #
@app.get("/api/symbols")
def symbols():
    """All symbols with current IV and computed IV Rank (52-week)."""
    rows = _iv("""
        SELECT DISTINCT h.security_id, h.symbol,
               h.atm_iv   AS last_iv,
               h.spot_price,
               h.timestamp AS last_ts
        FROM   iv_history h
        INNER JOIN (
            SELECT security_id, MAX(timestamp) AS mt
            FROM   iv_history
            WHERE  data_type = 'intraday'
            GROUP  BY security_id
        ) latest ON latest.security_id = h.security_id
                AND latest.mt          = h.timestamp
        WHERE  h.data_type = 'intraday'
          AND  h.atm_iv    > 0
        ORDER  BY h.symbol
    """)

    result = []
    for r in rows:
        # Compute IVR from 252-day daily history
        hist = _iv("""
            SELECT atm_iv FROM iv_history
            WHERE  security_id = ? AND data_type = 'daily'
              AND  atm_iv BETWEEN 1 AND 200
            ORDER  BY timestamp DESC LIMIT 252
        """, (r["security_id"],))
        iv_vals = [x["atm_iv"] for x in hist]
        if len(iv_vals) >= 5:
            iv_min = min(iv_vals)
            iv_max = max(iv_vals)
            iv_rank = round(
                (r["last_iv"] - iv_min) / (iv_max - iv_min) * 100
                if iv_max > iv_min else 0, 1
            )
        else:
            iv_rank = None
        result.append({
            "security_id": r["security_id"],
            "symbol":      r["symbol"],
            "last_iv":     round(r["last_iv"], 2) if r["last_iv"] else None,
            "spot":        round(r["spot_price"], 2) if r["spot_price"] else None,
            "iv_rank":     iv_rank,
            "last_ts":     r["last_ts"],
        })
    return result


# ── IV history for a symbol ───────────────────────────────────────────────── #
@app.get("/api/iv/{symbol}")
def iv_history(symbol: str, days: int = Query(30, ge=1, le=365)):
    """Daily IV history + PCR + spot for the main chart."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = _iv("""
        SELECT timestamp, atm_iv, atm_call_iv, atm_put_iv,
               spot_price, total_call_oi, total_put_oi,
               total_call_volume, total_put_volume
        FROM   iv_history
        WHERE  symbol   = ?
          AND  data_type = 'daily'
          AND  date(timestamp) >= ?
          AND  atm_iv   BETWEEN 1 AND 200
        ORDER  BY timestamp ASC
    """, (symbol.upper(), since))

    if not rows:
        raise HTTPException(404, f"No daily IV data for {symbol}")

    # Compute IVR range from 252-day history
    hist_all = _iv("""
        SELECT atm_iv FROM iv_history
        WHERE  symbol = ? AND data_type = 'daily'
          AND  atm_iv BETWEEN 1 AND 200
        ORDER  BY timestamp DESC LIMIT 252
    """, (symbol.upper(),))
    iv_vals = [x["atm_iv"] for x in hist_all]
    iv_min  = round(min(iv_vals), 2) if iv_vals else None
    iv_max  = round(max(iv_vals), 2) if iv_vals else None
    current = round(rows[-1]["atm_iv"], 2) if rows else None
    iv_rank = round(
        (current - iv_min) / (iv_max - iv_min) * 100
        if (iv_min and iv_max and iv_max > iv_min) else 0, 1
    )

    timestamps = [r["timestamp"][:10] for r in rows]
    return {
        "symbol":      symbol.upper(),
        "days":        days,
        "timestamps":  timestamps,
        "atm_iv":      [round(r["atm_iv"], 2)         for r in rows],
        "call_iv":     [round(r["atm_call_iv"] or 0, 2) for r in rows],
        "put_iv":      [round(r["atm_put_iv"]  or 0, 2) for r in rows],
        "spot":        [round(r["spot_price"]   or 0, 2) for r in rows],
        "pcr":         [
            round(r["total_put_oi"] / r["total_call_oi"], 3)
            if (r["total_call_oi"] or 0) > 0 else None
            for r in rows
        ],
        "iv_rank":     iv_rank,
        "iv_min_52w":  iv_min,
        "iv_max_52w":  iv_max,
        "current_iv":  current,
        "n_samples":   len(iv_vals),
    }


# ── Intraday IV for today ─────────────────────────────────────────────────── #
@app.get("/api/iv/{symbol}/intraday")
def iv_intraday(symbol: str):
    """Today's intraday IV ticks for the live view."""
    today = datetime.now().strftime("%Y-%m-%d")
    rows  = _iv("""
        SELECT timestamp, atm_iv, spot_price,
               total_call_oi, total_put_oi
        FROM   iv_history
        WHERE  symbol    = ?
          AND  data_type = 'intraday'
          AND  date(timestamp) = ?
          AND  atm_iv    BETWEEN 1 AND 200
        ORDER  BY timestamp ASC
    """, (symbol.upper(), today))

    return {
        "symbol":     symbol.upper(),
        "date":       today,
        "timestamps": [r["timestamp"][11:16] for r in rows],  # HH:MM
        "atm_iv":     [round(r["atm_iv"], 2) for r in rows],
        "spot":       [round(r["spot_price"] or 0, 2) for r in rows],
        "pcr":        [
            round(r["total_put_oi"] / r["total_call_oi"], 3)
            if (r["total_call_oi"] or 0) > 0 else None
            for r in rows
        ],
    }


# ── Paper trades ─────────────────────────────────────────────────────────── #
@app.get("/api/paper-trades")
def paper_trades(date: Optional[str] = None):
    """Today's (or a specific date's) paper trades."""
    d = date or datetime.now().strftime("%Y-%m-%d")
    rows = _pt("""
        SELECT symbol, side, strike, expiry, entry, last_price,
               sl, t1, t2, lot_size, score, iv, hv, iv_rank,
               realized_pct, realized_rupees, status,
               opened_at, closed_at, exit_reason
        FROM   paper_trades
        WHERE  date = ?
        ORDER  BY opened_at ASC
    """, (d,))
    return {"date": d, "trades": rows, "count": len(rows)}


@app.get("/api/paper-trades/history")
def paper_trades_history(days: int = Query(30, ge=1, le=90)):
    """Rolling P&L summary — one row per day."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows  = _pt("""
        SELECT date,
               COUNT(*)                                    AS total,
               SUM(CASE WHEN realized_rupees > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN realized_rupees < 0 THEN 1 ELSE 0 END) AS losses,
               ROUND(SUM(realized_rupees), 0)             AS net_rupees,
               ROUND(AVG(realized_pct), 2)                AS avg_pct,
               status
        FROM   paper_trades
        WHERE  date >= ? AND status = 'closed'
        GROUP  BY date
        ORDER  BY date ASC
    """, (since,))

    # Rolling cumulative P&L
    cumulative = 0.0
    for r in rows:
        cumulative += r["net_rupees"] or 0
        r["cumulative"] = round(cumulative, 0)

    total_trades = sum(r["total"]  for r in rows)
    total_wins   = sum(r["wins"]   for r in rows)
    total_pnl    = sum((r["net_rupees"] or 0) for r in rows)

    return {
        "days":       days,
        "daily":      rows,
        "summary": {
            "total_trades": total_trades,
            "win_rate":     round(total_wins / total_trades * 100, 1) if total_trades else 0,
            "net_rupees":   round(total_pnl, 0),
            "expectancy":   round(total_pnl / total_trades, 0) if total_trades else 0,
        },
    }


# ── Health ────────────────────────────────────────────────────────────────── #
@app.get("/api/health")
def health():
    iv_rows   = _scalar(IV_DB, "SELECT COUNT(*) FROM iv_history", default=0)
    iv_today  = _scalar(IV_DB,
        "SELECT COUNT(*) FROM iv_history WHERE date(timestamp,'localtime')=date('now','localtime')",
        default=0)
    last_ts   = _scalar(IV_DB, "SELECT MAX(timestamp) FROM iv_history WHERE data_type='intraday'")
    pt_today  = _scalar(PT_DB,
        "SELECT COUNT(*) FROM paper_trades WHERE date=date('now','localtime')",
        default=0)
    return {
        "iv_total_rows":  iv_rows,
        "iv_today_rows":  iv_today,
        "last_iv_update": last_ts,
        "paper_trades_today": pt_today,
        "db_iv":  str(IV_DB),
        "db_pt":  str(PT_DB),
        "server_time": datetime.now().isoformat(),
    }


# ── Serve frontend ────────────────────────────────────────────────────────── #
@app.get("/", response_class=HTMLResponse)
def index():
    html_path = Path(__file__).parent / "dashboard.html"
    if html_path.exists():
        return html_path.read_text()
    return HTMLResponse("<h1>Dashboard HTML not found</h1>", status_code=500)
