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
GET  /api/iv/{symbol}/skew      → latest per-strike IV skew (+ day-open snapshot)
GET  /api/paper-trades          → today's paper trades
GET  /api/paper-trades/history  → last 30 days P&L summary
GET  /api/health                → DB row counts + last update time
GET  /api/overview              → header KPIs (net P&L, win rate, expectancy,
                                   profit factor, best strategy, open/closed count)
GET  /api/strategy-performance  → per-strategy net P&L / win rate / profit factor
GET  /api/opportunities         → latest Composite Conviction list, enriched with
                                   IV Rank / PCR / smart-money / delivery / block-deal
GET  /api/market-snapshot       → India VIX, F&O-universe breadth, NIFTY/BANKNIFTY
                                   spot+PCR if tracked. FII/DII and Max Pain are
                                   NOT collected anywhere in this system — always null.
GET  /api/activity              → merged recent events across the *_history tables
GET  /api/analytics/ivp/{symbol}      → IV Percentile + buyer verdict (iv-rank scanner data)
GET  /api/analytics/expansion         → 3-4 day IV slope leaderboard (pre-event proxy)
GET  /api/analytics/decay             → intraday IV decay curve, 15-min buckets (?symbol=)
GET  /api/analytics/skew-tilt/{symbol}→ today's put−call IV tilt series + panic flag

Every field that isn't backed by a real collector (FII/DII cash flow, true Max
Pain) is returned as null rather than estimated or faked — the frontend must
render those as an explicit blank/"—" state, never a placeholder number.
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

import iv_analytics

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


# ── Volatility skew for a symbol ─────────────────────────────────────────── #
@app.get("/api/iv/{symbol}/skew")
def iv_skew(symbol: str):
    """Latest per-strike IV skew snapshot (written by iv-collector each pass),
    plus the same day's FIRST snapshot so the frontend can show intraday
    skew drift. 404 until the collector has run with skew support enabled."""
    def _parse(j):
        try:
            return json.loads(j) or []
        except Exception:
            return []

    try:
        rows = _iv("""
            SELECT timestamp, expiry, spot_price, atm_strike, strikes_json
            FROM   skew_snapshots
            WHERE  symbol = ?
            ORDER  BY timestamp DESC LIMIT 1
        """, (symbol.upper(),))
    except sqlite3.OperationalError:
        rows = []  # table appears on first collector run after the upgrade
    if not rows:
        raise HTTPException(
            404, f"No skew data for {symbol} yet — per-strike snapshots are "
                 "collected from the next iv-collector pass onward")

    latest = rows[0]
    day = latest["timestamp"][:10]
    first = _iv("""
        SELECT timestamp, strikes_json
        FROM   skew_snapshots
        WHERE  symbol = ? AND date(timestamp) = ?
        ORDER  BY timestamp ASC LIMIT 1
    """, (symbol.upper(), day))

    out = {
        "symbol":     symbol.upper(),
        "as_of":      latest["timestamp"],
        "expiry":     latest["expiry"],
        "spot":       latest["spot_price"],
        "atm_strike": latest["atm_strike"],
        "strikes":    _parse(latest["strikes_json"]),
    }
    if first and first[0]["timestamp"] != latest["timestamp"]:
        out["open_snapshot"] = {
            "as_of":   first[0]["timestamp"],
            "strikes": _parse(first[0]["strikes_json"]),
        }
    return out


# ── IV History Analytics Engine (module: iv_analytics.py) ────────────────── #
@app.get("/api/analytics/ivp/{symbol}")
def analytics_ivp(symbol: str):
    """IV Percentile + buyer-rule verdict. Source: iv-rank scanner output."""
    return iv_analytics.iv_percentile(symbol)


@app.get("/api/analytics/expansion")
def analytics_expansion(days: int = Query(4, ge=3, le=10),
                        limit: int = Query(15, ge=1, le=50)):
    """3-4 day daily-IV slope leaderboard — the pre-event expansion proxy.
    (No economic-calendar collector exists; this is slope-detected only.)"""
    return iv_analytics.vol_expansion(lookback_days=days, top_n=limit)


@app.get("/api/analytics/decay")
def analytics_decay(symbol: Optional[str] = None,
                    days: int = Query(10, ge=1, le=60)):
    """Average intraday IV per 15-min bucket over the last N sessions.
    Omit symbol for the universe-wide curve. Surfaces the midday lull."""
    return iv_analytics.intraday_decay_curve(symbol=symbol, days=days)


@app.get("/api/analytics/skew-tilt/{symbol}")
def analytics_skew_tilt(symbol: str, wing: int = Query(3, ge=1, le=7)):
    """Today's put−call IV tilt series at equidistant strikes + panic flag."""
    return iv_analytics.skew_tilt(symbol, wing=wing)


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


# ── Overview KPIs (header strip) ─────────────────────────────────────────── #
@app.get("/api/overview")
def overview(days: int = Query(30, ge=1, le=365)):
    """Net P&L, win rate, expectancy, profit factor, best strategy, open/closed
    counts — everything the header KPI strip needs, over the trailing `days`."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = _pt(
        "SELECT status, realized_rupees, strategy FROM paper_trades WHERE date >= ?",
        (since,),
    )
    closed = [r for r in rows if r["status"] == "closed"]
    open_n = sum(1 for r in rows if r["status"] == "open")
    wins   = [r for r in closed if (r["realized_rupees"] or 0) > 0]
    losses = [r for r in closed if (r["realized_rupees"] or 0) < 0]
    gross_win  = sum((r["realized_rupees"] or 0) for r in wins)
    gross_loss = abs(sum((r["realized_rupees"] or 0) for r in losses))
    net = sum((r["realized_rupees"] or 0) for r in closed)

    by_strat: dict = {}
    for r in closed:
        s = r["strategy"] or "Unknown"
        d = by_strat.setdefault(s, {"n": 0, "net": 0.0, "wins": 0})
        d["n"] += 1
        d["net"] += (r["realized_rupees"] or 0)
        if (r["realized_rupees"] or 0) > 0:
            d["wins"] += 1
    best_strategy = None
    if by_strat:
        name, d = max(by_strat.items(), key=lambda kv: kv[1]["net"])
        best_strategy = {
            "name": name,
            "win_rate": round(d["wins"] / d["n"] * 100, 1) if d["n"] else 0,
            "net_rupees": round(d["net"], 0),
        }

    return {
        "days": days,
        "total_trades": len(rows),
        "open_trades": open_n,
        "closed_trades": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "net_rupees": round(net, 0),
        "expectancy": round(net / len(closed), 0) if closed else 0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "best_strategy": best_strategy,
    }


# ── Per-strategy performance ─────────────────────────────────────────────── #
@app.get("/api/strategy-performance")
def strategy_performance(days: int = Query(30, ge=1, le=365)):
    """Net P&L / win rate / profit factor, grouped by the `strategy` tag on
    each closed paper trade."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = _pt(
        "SELECT strategy, realized_rupees FROM paper_trades "
        "WHERE date >= ? AND status='closed'",
        (since,),
    )
    by_strat: dict = {}
    for r in rows:
        s = r["strategy"] or "Unknown"
        d = by_strat.setdefault(
            s, {"n": 0, "net": 0.0, "wins": 0, "gross_win": 0.0, "gross_loss": 0.0}
        )
        rr = r["realized_rupees"] or 0
        d["n"] += 1
        d["net"] += rr
        if rr > 0:
            d["wins"] += 1
            d["gross_win"] += rr
        elif rr < 0:
            d["gross_loss"] += abs(rr)

    out = []
    for s, d in sorted(by_strat.items(), key=lambda kv: kv[1]["net"], reverse=True):
        out.append({
            "strategy": s,
            "trades": d["n"],
            "net_rupees": round(d["net"], 0),
            "win_rate": round(d["wins"] / d["n"] * 100, 1) if d["n"] else 0,
            "profit_factor": round(d["gross_win"] / d["gross_loss"], 2) if d["gross_loss"] else None,
        })
    return {"days": days, "strategies": out}


# ── Latest-per-symbol helper (shared by opportunities + snapshot) ────────── #
def _latest_per_symbol(table: str, cols: str) -> dict:
    """{security_id: row} for the most recent row per security_id in `table`.
    Returns {} if the table doesn't exist yet (fresh install) instead of raising."""
    try:
        rows = _iv(f"""
            SELECT t.security_id, {cols}, t.timestamp AS _ts
            FROM {table} t
            INNER JOIN (
                SELECT security_id, MAX(timestamp) AS mt FROM {table} GROUP BY security_id
            ) latest ON latest.security_id = t.security_id AND latest.mt = t.timestamp
        """)
    except Exception:
        return {}
    return {r["security_id"]: r for r in rows}


# ── Today's Top Opportunities (Composite Conviction, enriched) ──────────── #
@app.get("/api/opportunities")
def opportunities(limit: int = Query(12, ge=1, le=50)):
    """Latest Composite Conviction score per symbol, enriched with IV Rank,
    PCR/OI classification, and smart-money/delivery/block-deal flags.

    Composite only refreshes on its own EOD cadence (20:15/22:45) — `as_of`
    on every row is the real DB timestamp, so the frontend can show exactly
    how stale it is instead of implying a live intraday trigger.
    """
    comp = _iv(f"""
        SELECT c.security_id, c.symbol, c.direction, c.score, c.grade,
               c.n_factors, c.contributing, c.iv_zone, c.vix_regime,
               c.timestamp AS as_of
        FROM composite_history c
        INNER JOIN (
            SELECT security_id, MAX(timestamp) AS mt FROM composite_history GROUP BY security_id
        ) latest ON latest.security_id = c.security_id AND latest.mt = c.timestamp
        ORDER BY c.score DESC
        LIMIT ?
    """, (limit,))

    ivr_map = _latest_per_symbol("iv_rank_history", "iv_rank, iv_percentile, zone")
    oib_map = _latest_per_symbol("oi_buildup_history", "pcr, classification")
    sm_map  = _latest_per_symbol("smart_money_history", "bias")
    ds_map  = _latest_per_symbol("delivery_surge_history", "bias, surge_x")

    # Block deals: deals table has no security_id, so match by symbol text
    # over the last 2 calendar days.
    since = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        deal_syms = {
            (r["symbol"] or "").strip().upper()
            for r in _iv("SELECT DISTINCT symbol FROM deals WHERE date >= ?", (since,))
        }
    except Exception:
        deal_syms = set()

    out = []
    for r in comp:
        sid = r["security_id"]
        ivr, oib, sm, ds = ivr_map.get(sid), oib_map.get(sid), sm_map.get(sid), ds_map.get(sid)
        out.append({
            "symbol": r["symbol"],
            "direction": r["direction"],
            "score": round(r["score"], 1) if r["score"] is not None else None,
            "grade": r["grade"],
            "contributing": r["contributing"],
            "iv_zone": r["iv_zone"],
            "vix_regime": r["vix_regime"],
            "iv_rank": round(ivr["iv_rank"], 1) if ivr and ivr["iv_rank"] is not None else None,
            "iv_percentile": round(ivr["iv_percentile"], 1) if ivr and ivr.get("iv_percentile") is not None else None,
            "pcr": round(oib["pcr"], 2) if oib and oib["pcr"] is not None else None,
            "oi_classification": oib["classification"] if oib else None,
            "smart_money_bias": sm["bias"] if sm else None,
            "delivery_bias": ds["bias"] if ds else None,
            "block_deal": (r["symbol"] or "").strip().upper() in deal_syms,
            "composite_as_of": r["as_of"],
        })
    return {"count": len(out), "opportunities": out}


# ── Market snapshot ───────────────────────────────────────────────────────── #
@app.get("/api/market-snapshot")
def market_snapshot():
    """India VIX, F&O-universe market/sector breadth, and NIFTY/BANKNIFTY
    spot+PCR if those symbols happen to be in the tracked universe.

    FII/DII cash-flow figures and true Max Pain are NOT computed anywhere in
    this system (no collector for either) — both are always null here. Do
    not estimate or backfill these; render an explicit blank state instead.
    """
    vix_rows = _iv("SELECT date, close, pct_change FROM vix_daily ORDER BY date DESC LIMIT 1")
    vix = vix_rows[0] if vix_rows else None

    snap = None
    try:
        import breadth
        snap = breadth.compute(
            db_path=str(IV_DB),
            sector_db_path=str(DATA_DIR / "sector_mapping.db"),
        )
    except Exception:
        snap = None

    def _index_snapshot(sym: str):
        rows = _iv("""
            SELECT spot_price, total_call_oi, total_put_oi, timestamp
            FROM iv_history WHERE symbol=? AND data_type='intraday'
            ORDER BY timestamp DESC LIMIT 1
        """, (sym,))
        if not rows:
            return None
        r = rows[0]
        pcr = (round(r["total_put_oi"] / r["total_call_oi"], 2)
               if (r["total_call_oi"] or 0) > 0 else None)
        return {"spot": r["spot_price"], "pcr": pcr, "as_of": r["timestamp"]}

    return {
        "india_vix": ({
            "value": vix["close"], "pct_change": vix["pct_change"], "as_of": vix["date"],
        } if vix else None),
        "nifty": _index_snapshot("NIFTY"),
        "banknifty": _index_snapshot("BANKNIFTY"),
        "breadth": ({
            "market_pct": snap.market_pct,
            "advancers": snap.adv,
            "decliners": snap.dec,
            "as_of": snap.day,
            "universe_note": "F&O scan universe only — not the full exchange",
        } if snap and snap.total else None),
        "fii_dii": None,   # not collected anywhere — deliberately blank
        "max_pain": None,  # not computed anywhere — deliberately blank
    }


# ── Merged activity / alerts feed ─────────────────────────────────────────── #
@app.get("/api/activity")
def activity(limit: int = Query(20, ge=1, le=100)):
    """Recent events merged across every *_history scanner table, sorted by
    real timestamp. This is a log of what each scanner actually wrote, not a
    synthetic live feed."""
    events = []

    def _add(table, cols, build):
        try:
            rows = _iv(f"SELECT {cols}, timestamp AS ts FROM {table} ORDER BY timestamp DESC LIMIT 30")
        except Exception:
            rows = []
        for r in rows:
            try:
                events.append(build(r))
            except Exception:
                continue

    _add("composite_history", "symbol, score, grade",
         lambda r: {"ts": r["ts"], "type": "composite", "label": "Composite Trigger",
                    "symbol": r["symbol"],
                    "detail": f"Score {(r['score'] or 0):.0f} · {r['grade'] or ''}"})
    _add("smart_money_history", "symbol, bias, net_value_cr",
         lambda r: {"ts": r["ts"], "type": "smart_money",
                    "label": f"Smart Money {r['bias'] or ''}".strip(),
                    "symbol": r["symbol"],
                    "detail": f"Net Rs.{(r['net_value_cr'] or 0):.1f}Cr"})
    _add("oi_buildup_history", "symbol, classification, oi_chg_pct, price_chg_pct",
         lambda r: {"ts": r["ts"], "type": "oi_buildup",
                    "label": r["classification"] or "OI Buildup",
                    "symbol": r["symbol"],
                    "detail": f"OI {(r['oi_chg_pct'] or 0):+.1f}% · Px {(r['price_chg_pct'] or 0):+.1f}%"})
    _add("gap_history", "symbol, direction, gap_pct",
         lambda r: {"ts": r["ts"], "type": "gap",
                    "label": f"Gap {r['direction'] or ''}".strip(),
                    "symbol": r["symbol"],
                    "detail": f"{(r['gap_pct'] or 0):+.1f}% gap"})
    _add("sonar_history", "symbol, signal, bias, slope_pct",
         lambda r: {"ts": r["ts"], "type": "sonar",
                    "label": r["signal"] or "Sonar",
                    "symbol": r["symbol"],
                    "detail": f"{r['bias'] or ''} · slope {(r['slope_pct'] or 0):+.2f}%"})
    _add("delivery_surge_history", "symbol, bias, surge_x, deliv_pct",
         lambda r: {"ts": r["ts"], "type": "delivery",
                    "label": f"Delivery Surge {r['bias'] or ''}".strip(),
                    "symbol": r["symbol"],
                    "detail": f"{(r['surge_x'] or 0):.1f}x avg · {(r['deliv_pct'] or 0):.0f}% deliv"})

    events.sort(key=lambda e: e["ts"] or "", reverse=True)
    return {"events": events[:limit]}


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


# ── Convex cockpit (V2) — everything an option buyer needs in one call ────── #
def _iv_safe(sql: str, params=()) -> list[dict]:
    """_iv() that tolerates missing tables (engine tables appear on first run)."""
    try:
        return _iv(sql, params)
    except sqlite3.OperationalError:
        return []


@app.get("/api/cockpit")
def cockpit():
    """Regime + today's engine decisions + cheap-IV names. Read-only, fail-open."""
    regime = _iv_safe(
        "SELECT ts, posture, lean, vix, breadth_pct, size_mult, reasons "
        "FROM engine_regime ORDER BY ts DESC LIMIT 1")
    emitted = _iv_safe(
        "SELECT ts, symbol, direction, grade, score, trigger_kind, why "
        "FROM engine_decisions WHERE status='EMITTED' "
        "AND date(ts)=date('now','localtime') ORDER BY score DESC LIMIT 10")
    watch = _iv_safe(
        "SELECT symbol, MAX(score) AS score FROM engine_decisions "
        "WHERE status='WATCH' AND date(ts)=date('now','localtime') "
        "GROUP BY symbol ORDER BY score DESC LIMIT 8")
    rejects = _iv_safe(
        "SELECT reject_reason AS reason, COUNT(*) AS n FROM engine_decisions "
        "WHERE status='REJECTED' AND date(ts)=date('now','localtime') "
        "GROUP BY reject_reason ORDER BY n DESC LIMIT 5")
    n_rejected = _iv_safe(
        "SELECT COUNT(*) AS n FROM engine_decisions "
        "WHERE status='REJECTED' AND date(ts)=date('now','localtime')")
    cheap_iv = _iv_safe(
        "SELECT symbol, iv_rank, current_iv FROM iv_rank_history r "
        "WHERE zone='CHEAP' AND timestamp=(SELECT MAX(timestamp) "
        "  FROM iv_rank_history r2 WHERE r2.security_id=r.security_id) "
        "ORDER BY iv_rank ASC LIMIT 10")
    candles_today = _iv_safe(
        "SELECT COUNT(*) AS n FROM candles_5m WHERE date(ts)=date('now','localtime')")
    return {
        "regime": regime[0] if regime else None,
        "emitted": emitted,
        "watch": watch,
        "rejects": rejects,
        "n_rejected": (n_rejected[0]["n"] if n_rejected else 0),
        "cheap_iv": cheap_iv,
        "candles_today": (candles_today[0]["n"] if candles_today else 0),
        "server_time": datetime.now().isoformat(),
    }


# ── Serve frontend ────────────────────────────────────────────────────────── #
@app.get("/", response_class=HTMLResponse)
def index():
    html_path = Path(__file__).parent / "dashboard.html"
    if html_path.exists():
        return html_path.read_text()
    return HTMLResponse("<h1>Dashboard HTML not found</h1>", status_code=500)
