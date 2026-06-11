# -*- coding: utf-8 -*-
"""
IV Rank / IV Percentile Scanner  (service: iv-rank)

Standalone screener for OPTION BUYERS. Finds F&O names whose current ATM IV is
*cheap* relative to its own recent history — the "buy zone" where long options
are least exposed to IV crush.

Design rules (mirrors oi_validator.py isolation philosophy)
-----------------------------------------------------------
* Reads ONLY from the shared iv_history.db (written by iv-collector). Makes ZERO
  broker / option-chain API calls, so it adds no API load and can run anytime.
* Pure, side-effect-free math (iv_rank / iv_percentile / classify_zone) that is
  trivially unit-testable with plain Python lists.
* Fail-open: a symbol with insufficient history is skipped, never crashes a scan.
* Adaptive lookback: with only ~40 days of history today it reports an IV
  *percentile* over the available window and labels the baseline depth, then
  upgrades smoothly toward a true 52-week IV Rank as history accumulates.

Public surface
--------------
* iv_rank(current, hist) / iv_percentile(current, hist) / classify_zone(metric)
* IVRankScanner().scan()            -> ranked pandas DataFrame (cheapest first)
* IVRankScanner().persist(df)       -> writes iv_rank_history rows
* IVRankScanner().send_telegram(df) -> buy-zone alert (alert-only; no orders)
* get_latest_zone(security_id)      -> dict for directional_iv to consume
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime

import pandas as pd

from collectors import iv_store
import iv_rank_config as cfg

logger = logging.getLogger(__name__)

ZONE_CHEAP     = "CHEAP"
ZONE_FAIR      = "FAIR"
ZONE_EXPENSIVE = "EXPENSIVE"


# --------------------------------------------------------------------------- #
# Pure math (no I/O — unit-testable)
# --------------------------------------------------------------------------- #
def iv_rank(current_iv: float, historical_ivs: list) -> float | None:
    """IV Rank = (current - min) / (max - min) * 100, clipped to [0, 100].

    Mirrors discount.DiscountedPremiumScanner.calculate_iv_rank but standalone.
    Returns None when history is empty (caller decides what to do).
    """
    if current_iv is None or not historical_ivs:
        return None
    lo, hi = min(historical_ivs), max(historical_ivs)
    if hi == lo:
        return 50.0
    rank = (current_iv - lo) / (hi - lo) * 100.0
    return max(0.0, min(100.0, rank))


def iv_percentile(current_iv: float, historical_ivs: list) -> float | None:
    """Share of historical IV observations strictly below current IV (0-100).

    Mirrors discount.DiscountedPremiumScanner.calculate_iv_percentile.
    """
    if current_iv is None or not historical_ivs:
        return None
    below = sum(1 for v in historical_ivs if v < current_iv)
    return below / len(historical_ivs) * 100.0


def classify_zone(metric_value: float | None) -> str:
    """Map a rank/percentile value to a buyer zone using config thresholds."""
    if metric_value is None:
        return ZONE_FAIR
    if metric_value <= cfg.BUY_ZONE_MAX:
        return ZONE_CHEAP
    if metric_value <= cfg.SELECTIVE_MAX:
        return ZONE_FAIR
    return ZONE_EXPENSIVE


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #
class IVRankScanner:
    """Computes IV rank/percentile for every F&O symbol from iv_history.db."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or iv_store.DB_PATH

    # ---- DB helpers -------------------------------------------------------- #
    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _symbol_universe(self) -> pd.DataFrame:
        """Distinct (security_id, symbol) that have any daily IV history."""
        with self._connect() as conn:
            return pd.read_sql(
                """
                SELECT security_id,
                       MAX(symbol) AS symbol
                FROM   iv_history
                WHERE  data_type = 'daily'
                  AND  atm_iv BETWEEN 1.0 AND 200.0
                GROUP  BY security_id
                """,
                conn,
            )

    def _latest_iv_map(self) -> dict:
        """{security_id(str): (current_iv, timestamp)} — most recent IV of ANY
        data_type. Intraday is fresher than daily, so the latest row wins."""
        with self._connect() as conn:
            df = pd.read_sql(
                """
                SELECT h.security_id, h.atm_iv, h.timestamp
                FROM   iv_history h
                JOIN  (SELECT security_id, MAX(timestamp) AS mx
                       FROM   iv_history
                       WHERE  atm_iv BETWEEN 1.0 AND 200.0
                       GROUP  BY security_id) m
                  ON   h.security_id = m.security_id
                 AND   h.timestamp   = m.mx
                """,
                conn,
            )
        out = {}
        for _, r in df.iterrows():
            try:
                out[str(r["security_id"])] = (float(r["atm_iv"]), r["timestamp"])
            except (TypeError, ValueError):
                continue
        return out

    # ---- main scan --------------------------------------------------------- #
    def scan(self) -> pd.DataFrame:
        """Return a DataFrame ranked from cheapest IV to richest."""
        self._ensure_table()
        universe = self._symbol_universe()
        latest = self._latest_iv_map()

        rows = []
        for _, u in universe.iterrows():
            sid = str(u["security_id"])
            symbol = u["symbol"]
            hist = iv_store.get_iv_history(sid, days=cfg.LOOKBACK_DAYS)
            if len(hist) < cfg.MIN_HISTORY_DAYS:
                continue  # fail-open: not enough baseline to rank

            cur = latest.get(sid, (None, None))[0]
            if cur is None:
                cur = hist[-1]  # fallback to last daily point

            rank = iv_rank(cur, hist)
            pct = iv_percentile(cur, hist)
            primary = pct if cfg.PRIMARY_METRIC == "percentile" else rank
            zone = classify_zone(primary)

            rows.append(
                {
                    "security_id": sid,
                    "symbol": symbol,
                    "current_iv": round(cur, 2),
                    "iv_rank": round(rank, 1) if rank is not None else None,
                    "iv_percentile": round(pct, 1) if pct is not None else None,
                    "primary_metric": round(primary, 1) if primary is not None else None,
                    "zone": zone,
                    "hist_days": len(hist),
                    "iv_min": round(min(hist), 2),
                    "iv_max": round(max(hist), 2),
                }
            )

        if not rows:
            logger.warning("iv-rank: no symbols had >= %d daily IV points", cfg.MIN_HISTORY_DAYS)
            return pd.DataFrame()

        df = pd.DataFrame(rows).sort_values("primary_metric", na_position="last").reset_index(drop=True)
        cheap = (df["zone"] == ZONE_CHEAP).sum()
        logger.info("iv-rank: scanned %d symbols | %d in CHEAP buy-zone", len(df), cheap)
        return df

    # ---- persistence ------------------------------------------------------- #
    def _ensure_table(self) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {cfg.PERSIST_TABLE} (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    security_id   TEXT NOT NULL,
                    symbol        TEXT,
                    timestamp     DATETIME NOT NULL,
                    current_iv    REAL,
                    iv_rank       REAL,
                    iv_percentile REAL,
                    zone          TEXT,
                    hist_days     INTEGER,
                    UNIQUE(security_id, timestamp)
                )
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_ivrank_sid_time "
                f"ON {cfg.PERSIST_TABLE}(security_id, timestamp)"
            )
            conn.commit()

    def persist(self, df: pd.DataFrame) -> int:
        """Write one snapshot row per symbol. Returns rows inserted."""
        if df.empty:
            return 0
        self._ensure_table()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        inserted = 0
        with self._connect() as conn:
            for _, r in df.iterrows():
                cur = conn.execute(
                    f"""
                    INSERT INTO {cfg.PERSIST_TABLE}
                        (security_id, symbol, timestamp, current_iv,
                         iv_rank, iv_percentile, zone, hist_days)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(security_id, timestamp) DO NOTHING
                    """,
                    (
                        r["security_id"], r["symbol"], ts, r["current_iv"],
                        r["iv_rank"], r["iv_percentile"], r["zone"], int(r["hist_days"]),
                    ),
                )
                inserted += cur.rowcount
            conn.commit()
        logger.info("iv-rank: persisted %d rows to %s", inserted, cfg.PERSIST_TABLE)
        return inserted

    # ---- alerting ---------------------------------------------------------- #
    def _baseline_label(self, df: pd.DataFrame) -> str:
        """'IV Rank' once a full year exists, else an honest adaptive label."""
        if df.empty:
            return "IV %ile"
        depth = int(df["hist_days"].max())
        if depth >= cfg.FULL_BASELINE_DAYS:
            return "IV Rank (52w)"
        return f"IV %ile ({depth}d baseline)"

    def send_telegram(self, df: pd.DataFrame) -> None:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            logger.info("iv-rank: telegram skipped; bot token or chat id missing")
            return

        label = self._baseline_label(df)
        cheap = df[df["zone"] == ZONE_CHEAP] if not df.empty else df

        lines = [
            "📉 IV Rank Scanner — cheap-option buy zone",
            f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Metric: {label}",
        ]
        if df.empty:
            lines.append("No symbols had enough IV history to rank yet.")
        elif cheap.empty:
            lines.append(f"No names in CHEAP zone (<= {cfg.BUY_ZONE_MAX:.0f}). Cheapest available:")
            for _, r in df.head(cfg.TOP_N_ALERT).iterrows():
                lines.append(self._fmt_row(r))
        else:
            lines.append(f"⚡ {len(cheap)} names in CHEAP zone (<= {cfg.BUY_ZONE_MAX:.0f}):")
            for _, r in cheap.head(cfg.TOP_N_ALERT).iterrows():
                lines.append(self._fmt_row(r))
            extra = len(cheap) - min(len(cheap), cfg.TOP_N_ALERT)
            if extra > 0:
                lines.append(f"(+{extra} more cheap names)")

        lines.append("\nℹ️ Cheap IV favours buyers — still needs a catalyst + direction.")
        text = "\n".join(lines)

        try:
            import requests
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("iv-rank: telegram alert sent")
        except Exception:
            logger.exception("iv-rank: failed to send telegram alert")

    @staticmethod
    def _fmt_row(r) -> str:
        return (
            f"{r['symbol']:<12} IV {r['current_iv']:.1f}% | "
            f"pct {r['iv_percentile']:.0f} | rank {r['iv_rank']:.0f} "
            f"(range {r['iv_min']:.0f}-{r['iv_max']:.0f}, {int(r['hist_days'])}d)"
        )


# --------------------------------------------------------------------------- #
# Consumer helper for directional_iv (and any other strategy)
# --------------------------------------------------------------------------- #
def get_latest_zone(security_id: str, db_path: str | None = None) -> dict:
    """Most recent persisted IV-zone snapshot for a security. {} if none.

    Lets directional_iv (or any strategy) gate trades on cheap IV without
    recomputing — e.g. skip names whose zone != 'CHEAP'.
    """
    path = db_path or iv_store.DB_PATH
    try:
        with sqlite3.connect(path) as conn:
            cur = conn.execute(
                f"""
                SELECT current_iv, iv_rank, iv_percentile, zone, hist_days, timestamp
                FROM   {cfg.PERSIST_TABLE}
                WHERE  security_id = ?
                ORDER  BY timestamp DESC
                LIMIT  1
                """,
                (str(security_id),),
            )
            row = cur.fetchone()
    except sqlite3.OperationalError:
        return {}  # table not created yet — fail open
    if not row:
        return {}
    keys = ["current_iv", "iv_rank", "iv_percentile", "zone", "hist_days", "timestamp"]
    return dict(zip(keys, row))
