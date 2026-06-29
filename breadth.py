# -*- coding: utf-8 -*-
"""
breadth.py — market & sector breadth from intraday spot snapshots.

Computes, for the current session, how broad the up/down participation is — for
the whole F&O universe and per sector — straight from iv_history.db (the spot
snapshots iv-collector writes every cycle). No broker calls, no dependency on
the oi-buildup service schedule, so a breadth read is available as soon as there
are two intraday snapshots (≈09:30).

Used by:
  • order_manager breadth gate — block CE into a broadly-red tape / sector, and
    PE into a broadly-green one (config: breadth_config).
  • a morning sector-heatmap alert.

Breadth % = advancers / (advancers + decliners) * 100, counting only names whose
spot moved more than MIN_MOVE_PCT from day-open. 50 = balanced.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

import breadth_config as cfg

logger = logging.getLogger(__name__)


def _iv_db_path() -> str:
    try:
        from collectors import iv_store
        return iv_store.DB_PATH
    except Exception:
        return "data/iv_history.db"


# --------------------------------------------------------------------------- #
# Sector map (symbol -> industry), loaded from sector_mapping.db
# --------------------------------------------------------------------------- #
def load_sector_map(db_path: str | None = None) -> dict:
    """{SYMBOL: industry}. Empty dict if the DB is missing/unreadable."""
    path = db_path or cfg.SECTOR_DB_PATH
    out: dict = {}
    try:
        with sqlite3.connect(path) as conn:
            for sym, ind in conn.execute(
                'SELECT Symbol, Industry FROM symbol_sector_map'
            ):
                if sym and ind:
                    out[str(sym).strip().upper()] = str(ind).strip()
    except Exception:
        logger.debug("sector map unavailable at %s", path)
    return out


# --------------------------------------------------------------------------- #
# Breadth snapshot
# --------------------------------------------------------------------------- #
@dataclass
class BreadthSnapshot:
    market_pct: float | None = None     # None when too few names to trust
    adv: int = 0
    dec: int = 0
    total: int = 0                      # adv + dec (moving names only)
    sectors: dict = field(default_factory=dict)   # industry -> {adv,dec,pct,avg,n}
    symbol_sector: dict = field(default_factory=dict)
    day: str | None = None

    def sector_for(self, symbol: str):
        """(industry, sector_dict) for a symbol, or (None, None)."""
        ind = self.symbol_sector.get(str(symbol).strip().upper())
        if ind is None:
            return None, None
        return ind, self.sectors.get(ind)


def _classify(pct: float):
    """+1 advancer / -1 decliner / 0 flat, by the move deadband."""
    if pct >= cfg.MIN_MOVE_PCT:
        return 1
    if pct <= -cfg.MIN_MOVE_PCT:
        return -1
    return 0


def _pct(adv: int, dec: int):
    tot = adv + dec
    return (adv / tot * 100.0) if tot else None


def compute(day: str | None = None, db_path: str | None = None,
            sector_db_path: str | None = None) -> BreadthSnapshot:
    """Build a BreadthSnapshot from today's intraday spot snapshots.

    For each security: first vs latest spot of the day → % move. Counts
    advancers/decliners (past the deadband) market-wide and per sector.
    Fail-open: any error returns an empty snapshot (market_pct=None).
    """
    snap = BreadthSnapshot()
    path = db_path or _iv_db_path()
    try:
        with sqlite3.connect(path) as conn:
            if day is None:
                row = conn.execute(
                    "SELECT MAX(DATE(timestamp)) FROM iv_history WHERE data_type='intraday'"
                ).fetchone()
                day = row[0] if row and row[0] else None
            if not day:
                return snap
            snap.day = day
            rows = conn.execute(
                """SELECT security_id, symbol, spot_price
                   FROM iv_history
                   WHERE data_type='intraday' AND DATE(timestamp)=? AND spot_price>0
                   ORDER BY security_id, timestamp""",
                (day,),
            ).fetchall()
    except Exception:
        logger.debug("breadth: iv_history read failed", exc_info=True)
        return snap

    if not rows:
        return snap

    # first & last spot per security
    first: dict = {}
    last: dict = {}
    sym_of: dict = {}
    for sid, symbol, spot in rows:
        if sid not in first:
            first[sid] = float(spot)
        last[sid] = float(spot)
        sym_of[sid] = symbol

    smap = load_sector_map(sector_db_path)
    snap.symbol_sector = {}
    sec_acc: dict = {}   # industry -> [adv, dec, sum_pct, n]

    adv = dec = 0
    for sid, f0 in first.items():
        if f0 <= 0:
            continue
        pct = (last[sid] - f0) / f0 * 100.0
        cls = _classify(pct)
        symbol = str(sym_of.get(sid) or "").strip()
        ind = smap.get(symbol.upper())
        if ind:
            snap.symbol_sector[symbol.upper()] = ind
            a = sec_acc.setdefault(ind, [0, 0, 0.0, 0])
            a[2] += pct
            a[3] += 1
            if cls > 0:
                a[0] += 1
            elif cls < 0:
                a[1] += 1
        if cls > 0:
            adv += 1
        elif cls < 0:
            dec += 1

    snap.adv, snap.dec, snap.total = adv, dec, adv + dec
    if snap.total >= cfg.MIN_TOTAL_NAMES:
        snap.market_pct = _pct(adv, dec)

    for ind, (a, d, spct, n) in sec_acc.items():
        snap.sectors[ind] = {
            "adv": a, "dec": d, "n": n,
            "pct": _pct(a, d),
            "avg": round(spct / n, 2) if n else 0.0,
        }
    return snap


# --------------------------------------------------------------------------- #
# Pure decision
# --------------------------------------------------------------------------- #
def breadth_blocks(side, symbol, snap: BreadthSnapshot, c=cfg):
    """(block: bool, reason: str). Blocks a counter-trend entry by market and
    (optionally) the candidate's own sector breadth. Fail-open: unknown breadth
    never blocks."""
    side = "CE" if str(side).upper() in ("CE", "CALL") else "PE"
    if snap is None or snap.market_pct is None:
        return False, "no breadth data"

    m = snap.market_pct
    if side == "CE" and m < c.MIN_BREADTH_FOR_CE:
        return True, f"market breadth {m:.0f}% < {c.MIN_BREADTH_FOR_CE:.0f}% (red tape) vs CE"
    if side == "PE" and m > c.MAX_BREADTH_FOR_PE:
        return True, f"market breadth {m:.0f}% > {c.MAX_BREADTH_FOR_PE:.0f}% (green tape) vs PE"

    if c.SECTOR_ENABLED:
        ind, sec = snap.sector_for(symbol)
        if sec and sec.get("pct") is not None and sec["n"] >= c.SECTOR_MIN_NAMES:
            sp = sec["pct"]
            if side == "CE" and sp < c.MIN_SECTOR_BREADTH_FOR_CE:
                return True, f"{ind} breadth {sp:.0f}% < {c.MIN_SECTOR_BREADTH_FOR_CE:.0f}% vs CE"
            if side == "PE" and sp > c.MAX_SECTOR_BREADTH_FOR_PE:
                return True, f"{ind} breadth {sp:.0f}% > {c.MAX_SECTOR_BREADTH_FOR_PE:.0f}% vs PE"

    return False, "breadth ok"


# --------------------------------------------------------------------------- #
# Morning heatmap
# --------------------------------------------------------------------------- #
def format_sector_heatmap(snap: BreadthSnapshot | None = None, top: int = 0) -> str:
    """HTML sector heatmap sorted strongest→weakest by average move. `top`=0
    shows all sectors meeting the min-names bar."""
    snap = snap if snap is not None else compute()
    if not snap.sectors:
        return "🗺 <b>Sector heatmap</b>\nNo intraday breadth yet."

    items = [
        (ind, s) for ind, s in snap.sectors.items()
        if s["n"] >= cfg.SECTOR_MIN_NAMES
    ]
    items.sort(key=lambda kv: kv[1]["avg"], reverse=True)
    if top:
        items = items[:top]

    m = snap.market_pct
    head = (f"🗺 <b>Sector heatmap</b> — {snap.day or ''}\n"
            f"Market breadth {('%.0f%%' % m) if m is not None else 'n/a'} "
            f"({snap.adv}↑ / {snap.dec}↓)")
    lines = [head, "─────────────"]
    for ind, s in items:
        dot = "🟢" if s["avg"] > 0.15 else "🔴" if s["avg"] < -0.15 else "⚪"
        lines.append(
            f"{dot} {ind:<26} {s['avg']:+.2f}% avg | "
            f"{s['adv']}↑/{s['dec']}↓ ({s['pct']:.0f}%)" if s["pct"] is not None
            else f"{dot} {ind:<26} {s['avg']:+.2f}% avg | {s['n']} names"
        )
    return "\n".join(lines)


def send_sector_heatmap(bot_token: str | None = None, chat_id: str | None = None) -> bool:
    """Compute and push the sector heatmap via the shared notifier."""
    try:
        import notifications
        snap = compute()
        return notifications.notify(
            format_sector_heatmap(snap), bot_token=bot_token, chat_id=chat_id
        )
    except Exception:
        logger.exception("send_sector_heatmap failed (non-fatal)")
        return False
