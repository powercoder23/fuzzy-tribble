# -*- coding: utf-8 -*-
"""
OI Buildup Scanner  (service: oi-buildup)

Universe-wide option-buyer screener that classifies every F&O name into the four
OI quadrants — Long Buildup / Short Buildup / Short Covering / Long Unwinding —
using the SAME pure classifier that the Break & Bounce strategy already trusts
(oi_validator.classify). Promotes that per-breakout check into a standalone scan.

Design rules (same isolation contract as iv_rank_scanner / oi_validator)
------------------------------------------------------------------------
* Reads ONLY iv_history.db (spot_price + total_call_oi/total_put_oi snapshots
  written by iv-collector). ZERO broker / option-chain calls.
* Imports only the PURE function oi_validator.classify and the label constants
  from oi_config. Does not touch break-bounce, discount, momentum or order code.
* Fail-open: a name with missing/!two snapshots today is skipped, never crashes.

Note on the OI source
---------------------
This uses AGGREGATE OPTION OI (call+put) as a proxy for positioning, vs the
break-bounce validator which uses single-contract FUTURES OI. The quadrant
vocabulary is identical; the call/put split + PCR are surfaced as extra context
so the read isn't blind to option-writer nuance.

Buyer bias mapping
------------------
    LONG_BUILDUP   -> CE bias (fresh longs, price up + OI up)
    SHORT_BUILDUP  -> PE bias (fresh shorts, price down + OI up)
    SHORT_COVERING -> CE (weak / fading) — squeeze, not fresh demand
    LONG_UNWINDING -> PE (weak / fading)
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

import pandas as pd

from collectors import iv_store
from oi_validator import classify
import notifications
import oi_config
import oi_buildup_config as cfg

logger = logging.getLogger(__name__)

# Buyer-facing direction per classification.
BIAS = {
    oi_config.LONG_BUILDUP:   ("CE", "strong"),
    oi_config.SHORT_BUILDUP:  ("PE", "strong"),
    oi_config.SHORT_COVERING: ("CE", "weak"),
    oi_config.LONG_UNWINDING: ("PE", "weak"),
}


def buyer_bias(classification: str) -> tuple[str, str]:
    """(option_side, strength) for a classification. ('-', 'flat') if unknown."""
    return BIAS.get(classification, ("-", "flat"))


def is_flat(price_chg: float, oi_chg: float) -> bool:
    """True when both moves sit inside the dead-band (noise)."""
    return abs(price_chg) < cfg.MIN_PRICE_CHANGE_PCT and abs(oi_chg) < cfg.MIN_OI_CHANGE_PCT


class OIBuildupScanner:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or iv_store.DB_PATH

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _today(self) -> str:
        """Latest date that actually has intraday rows (handles weekends/holidays
        and stale test DBs gracefully)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(DATE(timestamp)) FROM iv_history WHERE data_type='intraday'"
            ).fetchone()
        return row[0] if row and row[0] else datetime.now().date().isoformat()

    def _snapshots_for_day(self, day: str) -> pd.DataFrame:
        """First and last intraday snapshot of `day` per security, with OI."""
        with self._connect() as conn:
            df = pd.read_sql(
                """
                SELECT security_id, symbol, timestamp, spot_price,
                       COALESCE(total_call_oi, 0) AS call_oi,
                       COALESCE(total_put_oi, 0)  AS put_oi
                FROM   iv_history
                WHERE  data_type = 'intraday' AND DATE(timestamp) = ?
                  AND  spot_price > 0
                ORDER  BY security_id, timestamp
                """,
                conn,
                params=(day,),
            )
        return df

    def scan(self) -> pd.DataFrame:
        self._ensure_table()
        day = self._today()
        snaps = self._snapshots_for_day(day)
        if snaps.empty:
            logger.warning("oi-buildup: no intraday snapshots for %s", day)
            return pd.DataFrame()

        rows = []
        for sid, g in snaps.groupby("security_id"):
            if len(g) < 2:
                continue  # need an open and a later reading
            first, last = g.iloc[0], g.iloc[-1]
            open_px, now_px = float(first["spot_price"]), float(last["spot_price"])
            oi_open = float(first["call_oi"]) + float(first["put_oi"])
            oi_now = float(last["call_oi"]) + float(last["put_oi"])
            if open_px <= 0 or oi_open <= 0 or oi_now < cfg.MIN_ABS_OI:
                continue

            price_chg = (now_px - open_px) / open_px * 100.0
            oi_chg = (oi_now - oi_open) / oi_open * 100.0
            call_chg = _pct(first["call_oi"], last["call_oi"])
            put_chg = _pct(first["put_oi"], last["put_oi"])
            pcr_now = (float(last["put_oi"]) / float(last["call_oi"])) if last["call_oi"] else None

            if is_flat(price_chg, oi_chg):
                classification = "FLAT"
                side, strength = "-", "flat"
            else:
                classification = classify(price_chg, oi_chg)
                side, strength = buyer_bias(classification)

            rows.append(
                {
                    "security_id": str(sid),
                    "symbol": last["symbol"],
                    "classification": classification,
                    "bias": side,
                    "strength": strength,
                    "price_chg_pct": round(price_chg, 2),
                    "oi_chg_pct": round(oi_chg, 2),
                    "call_oi_chg_pct": round(call_chg, 2) if call_chg is not None else None,
                    "put_oi_chg_pct": round(put_chg, 2) if put_chg is not None else None,
                    "pcr": round(pcr_now, 2) if pcr_now is not None else None,
                    "spot": round(now_px, 2),
                }
            )

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        # Rank: strong buildups first, then by absolute price move.
        order = {"strong": 0, "weak": 1, "flat": 2}
        df["_rank"] = df["strength"].map(order).fillna(3)
        df["_absmove"] = df["price_chg_pct"].abs()
        df = df.sort_values(["_rank", "_absmove"], ascending=[True, False]).drop(
            columns=["_rank", "_absmove"]
        ).reset_index(drop=True)

        counts = dict(df["classification"].value_counts())
        logger.info("oi-buildup: scanned %d names | %s", len(df), counts)
        return df

    # ---- persistence ------------------------------------------------------- #
    def _ensure_table(self) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {cfg.PERSIST_TABLE} (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    security_id    TEXT NOT NULL,
                    symbol         TEXT,
                    timestamp      DATETIME NOT NULL,
                    classification TEXT,
                    bias           TEXT,
                    strength       TEXT,
                    price_chg_pct  REAL,
                    oi_chg_pct     REAL,
                    pcr            REAL,
                    UNIQUE(security_id, timestamp)
                )
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_oib_sid_time "
                f"ON {cfg.PERSIST_TABLE}(security_id, timestamp)"
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
                        (security_id, symbol, timestamp, classification,
                         bias, strength, price_chg_pct, oi_chg_pct, pcr)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(security_id, timestamp) DO NOTHING
                    """,
                    (
                        r["security_id"], r["symbol"], ts, r["classification"],
                        r["bias"], r["strength"], r["price_chg_pct"],
                        r["oi_chg_pct"], r["pcr"],
                    ),
                )
                n += cur.rowcount
            conn.commit()
        logger.info("oi-buildup: persisted %d rows", n)
        return n

    # ---- alerting ---------------------------------------------------------- #
    def send_telegram(self, df: pd.DataFrame) -> None:
        lines = [
            "🔭 OI Buildup Scanner",
            f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')} (latest vs day-open, aggregate option OI)",
        ]
        if df.empty:
            lines.append("No names had two intraday snapshots to compare yet.")
        else:
            longs = df[df["classification"] == oi_config.LONG_BUILDUP].head(cfg.TOP_N_ALERT)
            shorts = df[df["classification"] == oi_config.SHORT_BUILDUP].head(cfg.TOP_N_ALERT)
            if not longs.empty:
                lines.append(f"\n🟢 Long buildup → CE bias ({len(longs)}):")
                lines += [self._fmt(r) for _, r in longs.iterrows()]
            if not shorts.empty:
                lines.append(f"\n🔴 Short buildup → PE bias ({len(shorts)}):")
                lines += [self._fmt(r) for _, r in shorts.iterrows()]
            if longs.empty and shorts.empty:
                lines.append("No fresh buildup today (mostly covering / unwinding / flat).")
        lines.append("\nℹ️ Buildup = direction; still confirm cheap IV + a catalyst before buying.")
        text = "\n".join(lines)

        if notifications.notify(text, parse_mode=None):
            logger.info("oi-buildup: alert sent")
        else:
            logger.info("oi-buildup: alert skipped; no channel configured")

    @staticmethod
    def _fmt(r) -> str:
        pcr = f"{r['pcr']:.2f}" if r["pcr"] is not None else "n/a"
        return (
            f"{r['symbol']:<12} px {r['price_chg_pct']:+.2f}% | OI {r['oi_chg_pct']:+.1f}% "
            f"(C {r['call_oi_chg_pct']:+.0f} / P {r['put_oi_chg_pct']:+.0f}) | PCR {pcr}"
        )


def _pct(old, new) -> float | None:
    try:
        old, new = float(old), float(new)
    except (TypeError, ValueError):
        return None
    if old <= 0:
        return None
    return (new - old) / old * 100.0


def get_latest_buildup(security_id: str, db_path: str | None = None) -> dict:
    """Most recent persisted buildup row for a security. {} if none."""
    path = db_path or iv_store.DB_PATH
    try:
        with sqlite3.connect(path) as conn:
            cur = conn.execute(
                f"""
                SELECT classification, bias, strength, price_chg_pct,
                       oi_chg_pct, pcr, timestamp
                FROM   {cfg.PERSIST_TABLE}
                WHERE  security_id = ?
                ORDER  BY timestamp DESC LIMIT 1
                """,
                (str(security_id),),
            )
            row = cur.fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    keys = ["classification", "bias", "strength", "price_chg_pct", "oi_chg_pct", "pcr", "timestamp"]
    return dict(zip(keys, row))
