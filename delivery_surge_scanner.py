# -*- coding: utf-8 -*-
"""
Delivery-% Surge Scanner  (service: delivery-surge)

Option-buyer screener for conviction-backed moves. A name qualifies when today's
delivery % is a SURGE over its trailing average AND the price moved meaningfully:

    deliv_pct(today) >= SURGE_MULT * avg(deliv_pct, prior days)
    AND deliv_pct(today) >= MIN_DELIV_PCT
    AND |price change today| >= MIN_PRICE_CHANGE_PCT

    surge + price up   -> accumulation -> CE bias
    surge + price down -> distribution -> PE bias

Why it matters for a buyer: high delivery % = real money positioning, so the move
tends to trend over days instead of chopping you out on theta. This is a BTST /
swing signal (daily data), pair with cheap IV (iv-rank) before buying.

Design rules (same isolation contract as the other scanners)
------------------------------------------------------------
* Reads ONLY iv_history.db (the `delivery_daily` table from the bhav collector,
  plus iv_history for the F&O symbol->security_id map). ZERO broker / NSE calls.
* Adaptive baseline: runs on whatever history exists, labels the depth, and
  sharpens as more bhav days accumulate.
* Fail-open: missing table / too little history -> empty result, never crashes.
* Touches no existing module's code or behaviour.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

import pandas as pd

from collectors import iv_store
import notifications
import delivery_surge_config as cfg

logger = logging.getLogger(__name__)


def surge_ratio(today_pct: float, avg_pct: float) -> float | None:
    if avg_pct is None or avg_pct <= 0 or today_pct is None:
        return None
    return today_pct / avg_pct


def qualifies(today_pct, avg_pct, price_chg) -> bool:
    r = surge_ratio(today_pct, avg_pct)
    if r is None:
        return False
    return (
        r >= cfg.SURGE_MULT
        and today_pct >= cfg.MIN_DELIV_PCT
        and abs(price_chg) >= cfg.MIN_PRICE_CHANGE_PCT
    )


class DeliverySurgeScanner:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or iv_store.DB_PATH

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _has_delivery_daily(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='delivery_daily'"
            ).fetchone()
            if not row:
                return False
            return conn.execute("SELECT COUNT(*) FROM delivery_daily").fetchone()[0] > 0

    def _fno_symbols(self) -> set:
        """Upper-cased symbols present in iv_history = the F&O universe."""
        with self._connect() as conn:
            return {
                str(r[0]).upper()
                for r in conn.execute("SELECT DISTINCT symbol FROM iv_history")
                if r[0]
            }

    def _symbol_to_sid(self) -> dict:
        with self._connect() as conn:
            out = {}
            for sym, sid in conn.execute(
                "SELECT symbol, MAX(security_id) FROM iv_history GROUP BY symbol"
            ):
                if sym:
                    out[str(sym).upper()] = str(sid)
            return out

    def scan(self) -> pd.DataFrame:
        self._ensure_table()
        if not self._has_delivery_daily():
            logger.warning(
                "delivery-surge: delivery_daily is empty/missing — run the bhav "
                "collector (or scripts/backfill_bhav.py) to populate it."
            )
            return pd.DataFrame()

        with self._connect() as conn:
            df = pd.read_sql(
                """
                SELECT date, symbol, close, deliv_pct
                FROM   delivery_daily
                WHERE  deliv_pct IS NOT NULL AND close IS NOT NULL
                ORDER  BY symbol, date
                """,
                conn,
            )
        if df.empty:
            return pd.DataFrame()

        fno = self._fno_symbols() if cfg.FNO_ONLY else None
        sid_map = self._symbol_to_sid()

        rows = []
        max_hist = 0
        for symbol, g in df.groupby("symbol"):
            sym_u = str(symbol).upper()
            if fno is not None and sym_u not in fno:
                continue
            g = g.tail(cfg.LOOKBACK_DAYS + 1)
            if len(g) < cfg.MIN_HISTORY_DAYS + 1:  # need latest + >=1 prior
                continue
            latest = g.iloc[-1]
            prior = g.iloc[:-1]
            avg_pct = float(prior["deliv_pct"].mean())
            today_pct = float(latest["deliv_pct"])
            prev_close = float(prior.iloc[-1]["close"])
            today_close = float(latest["close"])
            price_chg = (today_close - prev_close) / prev_close * 100.0 if prev_close else 0.0
            max_hist = max(max_hist, len(prior))

            if not qualifies(today_pct, avg_pct, price_chg):
                continue

            rows.append(
                {
                    "security_id": sid_map.get(sym_u),
                    "symbol": sym_u,
                    "bias": "CE" if price_chg > 0 else "PE",
                    "deliv_pct": round(today_pct, 1),
                    "avg_deliv_pct": round(avg_pct, 1),
                    "surge_x": round(surge_ratio(today_pct, avg_pct), 2),
                    "price_chg_pct": round(price_chg, 2),
                    "close": round(today_close, 2),
                    "hist_days": len(prior),
                    "date": latest["date"],
                }
            )

        if not rows:
            logger.info("delivery-surge: no delivery surges met the criteria")
            return pd.DataFrame()

        out = pd.DataFrame(rows).sort_values("surge_x", ascending=False).reset_index(drop=True)
        logger.info(
            "delivery-surge: %d names (baseline up to %d prior days)", len(out), max_hist
        )
        return out

    # ---- persistence ------------------------------------------------------- #
    def _ensure_table(self) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {cfg.PERSIST_TABLE} (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    security_id   TEXT,
                    symbol        TEXT,
                    timestamp     DATETIME NOT NULL,
                    bias          TEXT,
                    deliv_pct     REAL,
                    avg_deliv_pct REAL,
                    surge_x       REAL,
                    price_chg_pct REAL,
                    hist_days     INTEGER,
                    UNIQUE(symbol, timestamp)
                )
                """
            )
            conn.commit()

    def persist(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        self._ensure_table()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        n = 0
        with self._connect() as conn:
            for _, r in df.iterrows():
                cur = conn.execute(
                    f"""
                    INSERT INTO {cfg.PERSIST_TABLE}
                        (security_id, symbol, timestamp, bias, deliv_pct,
                         avg_deliv_pct, surge_x, price_chg_pct, hist_days)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, timestamp) DO NOTHING
                    """,
                    (
                        r["security_id"], r["symbol"], ts, r["bias"], r["deliv_pct"],
                        r["avg_deliv_pct"], r["surge_x"], r["price_chg_pct"], int(r["hist_days"]),
                    ),
                )
                n += cur.rowcount
            conn.commit()
        logger.info("delivery-surge: persisted %d rows", n)
        return n

    # ---- alerting ---------------------------------------------------------- #
    def send_telegram(self, df: pd.DataFrame) -> None:
        depth = int(df["hist_days"].max()) if not df.empty else 0
        label = "reliable" if depth >= cfg.RELIABLE_HISTORY_DAYS else f"low-confidence ({depth}d baseline)"
        lines = [
            "📦 Delivery-% Surge Scanner",
            f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')} | baseline: {label}",
        ]
        if df.empty:
            lines.append("No delivery surges today (or delivery_daily not populated).")
        else:
            ce = df[df["bias"] == "CE"].head(cfg.TOP_N_ALERT)
            pe = df[df["bias"] == "PE"].head(cfg.TOP_N_ALERT)
            if not ce.empty:
                lines.append(f"\n🟢 Accumulation → CE ({len(ce)}):")
                lines += [self._fmt(r) for _, r in ce.iterrows()]
            if not pe.empty:
                lines.append(f"\n🔴 Distribution → PE ({len(pe)}):")
                lines += [self._fmt(r) for _, r in pe.iterrows()]
        lines.append("\nℹ️ BTST/swing signal — pair with cheap IV (iv-rank) and 4+ DTE.")
        text = "\n".join(lines)

        if notifications.notify(text, parse_mode=None):
            logger.info("delivery-surge: alert sent")
        else:
            logger.info("delivery-surge: alert skipped; no channel configured")

    @staticmethod
    def _fmt(r) -> str:
        return (
            f"{r['symbol']:<12} deliv {r['deliv_pct']:.0f}% vs avg {r['avg_deliv_pct']:.0f}% "
            f"({r['surge_x']:.1f}x) | px {r['price_chg_pct']:+.2f}%"
        )


def get_latest_surge(symbol: str, db_path: str | None = None) -> dict:
    path = db_path or iv_store.DB_PATH
    try:
        with sqlite3.connect(path) as conn:
            cur = conn.execute(
                f"""
                SELECT symbol, bias, deliv_pct, avg_deliv_pct, surge_x,
                       price_chg_pct, hist_days, timestamp
                FROM   {cfg.PERSIST_TABLE}
                WHERE  symbol = ? ORDER BY timestamp DESC LIMIT 1
                """,
                (str(symbol).upper(),),
            )
            row = cur.fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    keys = ["symbol", "bias", "deliv_pct", "avg_deliv_pct", "surge_x",
            "price_chg_pct", "hist_days", "timestamp"]
    return dict(zip(keys, row))
