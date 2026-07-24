# -*- coding: utf-8 -*-
"""Outcome labeler (E1-1) — the engine's truth layer.

For every EMITTED decision the engine journaled, look forward in the underlying's
own 5-min candles and record what actually happened: spot at +30/+60/+90 min,
the raw % move, the *direction-adjusted edge* (a CE wants spot up, a PE wants it
down), and a hit flag. Writes engine_outcomes (engine is its sole writer, 1:1
with engine_decisions by id). This is what lets the journal show hit-rate and
expectancy PER GRADE — i.e. whether the conviction ladder actually holds or is
inverted (the Week-1 blocker) — instead of ranking on score alone.

Zero broker calls: forward spot comes from candles_5m, keyed by the same
security_id the engine decided on. A horizon with no forward candle (end of
session) is left NULL and can be re-labeled later.

Run:
    python -m engine.labeler            # label all not-yet-labeled EMITTED rows
    python -m engine.labeler --rewrite  # re-label everything (e.g. after a fix)
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta

from engine import config as cfg
from engine.store import _connect

logger = logging.getLogger(__name__)

HORIZONS = (30, 60, 90)          # minutes forward
CANDLE_TABLE = "candles_5m"
OUTCOMES_TABLE = "engine_outcomes"
_TS_FMT = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------------- #
def ensure_table(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {OUTCOMES_TABLE} (
                decision_id INTEGER PRIMARY KEY,   -- 1:1 with engine_decisions.id
                ts TEXT NOT NULL, day TEXT NOT NULL,
                symbol TEXT, security_id TEXT,
                direction TEXT, grade TEXT, score REAL, formula_ver TEXT,
                entry_spot REAL,
                spot_30 REAL, spot_60 REAL, spot_90 REAL,
                move_30 REAL, move_60 REAL, move_90 REAL,   -- raw % (fwd-entry)/entry
                edge_30 REAL, edge_60 REAL, edge_90 REAL,   -- direction-adjusted %
                hit_30 INTEGER, hit_60 INTEGER, hit_90 INTEGER,
                labeled_at TEXT
            )""")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_out_day ON {OUTCOMES_TABLE}(day)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_out_grade ON {OUTCOMES_TABLE}(grade)")
        conn.commit()


def _candle_series(conn, security_id: str, day: str) -> list[tuple[datetime, float]]:
    """Ascending [(ts, close)] for one name on one day."""
    rows = conn.execute(
        f"SELECT ts, close FROM {CANDLE_TABLE} "
        f"WHERE security_id=? AND substr(ts,1,10)=? AND close IS NOT NULL "
        f"ORDER BY ts", (str(security_id), day)).fetchall()
    out = []
    for ts_s, close in rows:
        try:
            out.append((datetime.strptime(str(ts_s)[:19], _TS_FMT), float(close)))
        except (ValueError, TypeError):
            continue
    return out


def _price_at_or_before(series, t: datetime) -> float | None:
    """Close of the last candle at/ before t (the spot the engine saw)."""
    px = None
    for cts, close in series:
        if cts <= t:
            px = close
        else:
            break
    return px


def _price_at_or_after(series, t: datetime) -> float | None:
    """Close of the first candle at/after t (the forward spot)."""
    for cts, close in series:
        if cts >= t:
            return close
    return None


def _label_one(dec_ts: datetime, direction: str, series) -> dict | None:
    entry = _price_at_or_before(series, dec_ts)
    if not entry:
        # decision before the first candle — use the first candle as entry
        entry = series[0][1] if series else None
    if not entry:
        return None
    sign = 1.0 if direction == "CE" else -1.0
    out = {"entry_spot": round(entry, 2)}
    for h in HORIZONS:
        fwd = _price_at_or_after(series, dec_ts + timedelta(minutes=h))
        if fwd is None:
            out[f"spot_{h}"] = out[f"move_{h}"] = out[f"edge_{h}"] = out[f"hit_{h}"] = None
            continue
        move = (fwd - entry) / entry * 100.0
        edge = sign * move
        out[f"spot_{h}"] = round(fwd, 2)
        out[f"move_{h}"] = round(move, 4)
        out[f"edge_{h}"] = round(edge, 4)
        out[f"hit_{h}"] = 1 if edge > 0 else 0
    return out


# --------------------------------------------------------------------------- #
def run(db_path: str | None = None, *, rewrite: bool = False,
        statuses: tuple[str, ...] = ("EMITTED",)) -> dict:
    """Label decisions that have candle coverage. Returns a summary dict."""
    if db_path is None:
        from collectors import iv_store
        db_path = iv_store.DB_PATH
    ensure_table(db_path)

    status_ph = ",".join("?" for _ in statuses)
    where_new = "" if rewrite else (
        f" AND d.id NOT IN (SELECT decision_id FROM {OUTCOMES_TABLE})")

    with _connect(db_path) as conn:
        decisions = conn.execute(
            f"""SELECT d.id, d.ts, d.symbol, d.security_id, d.direction,
                       d.grade, d.score, d.formula_ver
                FROM engine_decisions d
                WHERE d.status IN ({status_ph}) AND d.direction IS NOT NULL
                {where_new}
                ORDER BY d.security_id, d.ts""", tuple(statuses)).fetchall()

        labeled = skipped = 0
        series_cache: dict[tuple[str, str], list] = {}
        now_iso = datetime.now().isoformat(timespec="seconds")
        batch = []
        for did, ts_s, symbol, sid, direction, grade, score, fver in decisions:
            try:
                dec_ts = datetime.strptime(str(ts_s)[:19], _TS_FMT)
            except (ValueError, TypeError):
                skipped += 1
                continue
            day = str(ts_s)[:10]
            key = (str(sid), day)
            if key not in series_cache:
                series_cache[key] = _candle_series(conn, sid, day)
            series = series_cache[key]
            if not series:
                skipped += 1
                continue
            o = _label_one(dec_ts, direction, series)
            if o is None:
                skipped += 1
                continue
            batch.append((
                did, ts_s, day, symbol, str(sid), direction, grade, score, fver,
                o["entry_spot"],
                o["spot_30"], o["spot_60"], o["spot_90"],
                o["move_30"], o["move_60"], o["move_90"],
                o["edge_30"], o["edge_60"], o["edge_90"],
                o["hit_30"], o["hit_60"], o["hit_90"],
                now_iso))
            labeled += 1

        conn.executemany(
            f"""INSERT INTO {OUTCOMES_TABLE}
                (decision_id, ts, day, symbol, security_id, direction, grade,
                 score, formula_ver, entry_spot, spot_30, spot_60, spot_90,
                 move_30, move_60, move_90, edge_30, edge_60, edge_90,
                 hit_30, hit_60, hit_90, labeled_at)
                VALUES ({",".join("?" for _ in range(23))})
                ON CONFLICT(decision_id) DO UPDATE SET
                 entry_spot=excluded.entry_spot,
                 spot_30=excluded.spot_30, spot_60=excluded.spot_60, spot_90=excluded.spot_90,
                 move_30=excluded.move_30, move_60=excluded.move_60, move_90=excluded.move_90,
                 edge_30=excluded.edge_30, edge_60=excluded.edge_60, edge_90=excluded.edge_90,
                 hit_30=excluded.hit_30, hit_60=excluded.hit_60, hit_90=excluded.hit_90,
                 labeled_at=excluded.labeled_at""", batch)
        conn.commit()

    logger.info("labeler: labeled %d, skipped %d (rewrite=%s)", labeled, skipped, rewrite)
    return {"labeled": labeled, "skipped": skipped, "rewrite": rewrite}


def main():
    ap = argparse.ArgumentParser(description="Convex engine outcome labeler (E1-1)")
    ap.add_argument("--rewrite", action="store_true", help="re-label all rows")
    ap.add_argument("--db", default=None, help="iv_history.db path override")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(run(args.db, rewrite=args.rewrite))


if __name__ == "__main__":
    main()
