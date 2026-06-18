# -*- coding: utf-8 -*-
"""
Extreme Opening (Gap + Range) Scanner  (service: gap-scan)

Option-buyer screener for open-drive setups. A name qualifies as EXTREME when:

    |gap%|  >= GAP_PCT                         (open vs yesterday's close)
    AND open prints beyond yesterday's range   (open > prev_high  for gap-up
                                                open < prev_low   for gap-down)

Gap-up + breaks prior high  -> CE bias (gap-and-go long)
Gap-down + breaks prior low -> PE bias (gap-and-go short)

Design rules (same isolation contract as the other scanners)
------------------------------------------------------------
* Reads ONLY iv_history.db. ZERO broker calls.
* Prior-day OHLC source is adaptive:
    - if a populated `delivery_daily` table exists (bhav collector) -> TRUE OHLC
    - else -> a proxy built from the prior day's intraday IV snapshots
      (open=first, high=max(spot), low=min(spot), close=last). Labelled in alerts.
* Today's "open" = first intraday snapshot at/after OPEN_CUTOFF.
* Fail-open: any name missing a prior day or today's open is skipped.
* Touches no existing module's code or behaviour.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date as _date, datetime

import pandas as pd

from collectors import iv_store
import notifications
import gap_scanner_config as cfg

logger = logging.getLogger(__name__)


def gap_pct(open_px: float, prev_close: float) -> float | None:
    if not prev_close or prev_close <= 0 or open_px is None:
        return None
    return (open_px - prev_close) / prev_close * 100.0


def classify_gap(open_px, prev_close, prev_high, prev_low, require_range_break) -> tuple[str, bool]:
    """Return (direction, is_extreme).

    direction in {GAP_UP, GAP_DOWN, NONE}. is_extreme applies the gap% + range
    rule from config.
    """
    g = gap_pct(open_px, prev_close)
    if g is None:
        return "NONE", False
    if g >= cfg.GAP_PCT:
        direction = "GAP_UP"
        range_ok = (prev_high is None) or (open_px > prev_high)
    elif g <= -cfg.GAP_PCT:
        direction = "GAP_DOWN"
        range_ok = (prev_low is None) or (open_px < prev_low)
    else:
        return "NONE", False
    extreme = range_ok if require_range_break else True
    return direction, extreme


class GapScanner:
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
            cnt = conn.execute("SELECT COUNT(*) FROM delivery_daily").fetchone()[0]
        return cnt > 0

    def _intraday_dates(self) -> list[str]:
        with self._connect() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT DATE(timestamp) FROM iv_history "
                    "WHERE data_type='intraday' ORDER BY DATE(timestamp)"
                )
            ]

    # ---- today's open ------------------------------------------------------ #
    def _today_opens(self, day: str) -> pd.DataFrame:
        """First intraday spot at/after OPEN_CUTOFF per security on `day`."""
        with self._connect() as conn:
            df = pd.read_sql(
                """
                SELECT security_id, symbol, timestamp, spot_price
                FROM   iv_history
                WHERE  data_type='intraday' AND DATE(timestamp)=?
                  AND  TIME(timestamp) >= ? AND spot_price > 0
                ORDER  BY security_id, timestamp
                """,
                conn,
                params=(day, cfg.OPEN_CUTOFF + ":00"),
            )
        if df.empty:
            return df
        return df.groupby("security_id", as_index=False).first()

    # ---- prior-day OHLC ---------------------------------------------------- #
    def _prev_ohlc_from_delivery(self, prev_day: str) -> dict:
        with self._connect() as conn:
            df = pd.read_sql(
                "SELECT symbol, open, high, low, close FROM delivery_daily WHERE date=?",
                conn,
                params=(prev_day,),
            )
        return {str(r["symbol"]).upper(): r for _, r in df.iterrows()}

    def _prev_ohlc_from_intraday(self, prev_day: str) -> dict:
        """Proxy: open=first, high=max, low=min, close=last intraday spot."""
        with self._connect() as conn:
            df = pd.read_sql(
                """
                SELECT security_id, symbol, spot_price, timestamp
                FROM   iv_history
                WHERE  data_type='intraday' AND DATE(timestamp)=? AND spot_price > 0
                ORDER  BY security_id, timestamp
                """,
                conn,
                params=(prev_day,),
            )
        out = {}
        for sid, g in df.groupby("security_id"):
            out[str(sid)] = {
                "symbol": g.iloc[-1]["symbol"],
                "open": float(g.iloc[0]["spot_price"]),
                "high": float(g["spot_price"].max()),
                "low": float(g["spot_price"].min()),
                "close": float(g.iloc[-1]["spot_price"]),
            }
        return out

    def scan(self) -> pd.DataFrame:
        self._ensure_table()
        dates = self._intraday_dates()
        if len(dates) < 2:
            logger.warning("gap-scan: need >= 2 trading days of intraday data")
            return pd.DataFrame()
        today, prev_day = dates[-1], dates[-2]

        use_true_ohlc = self._has_delivery_daily()
        source = "delivery_daily" if use_true_ohlc else "intraday-proxy"
        # Staleness guard: if the proxy's "previous day" is not an adjacent
        # trading session (data gap), the computed "gap" is really a multi-day
        # move. Flag it loudly so the signal isn't mistaken for an overnight gap.
        if not use_true_ohlc:
            gap_days = (_date.fromisoformat(today) - _date.fromisoformat(prev_day)).days
            if gap_days > 4:
                source = "intraday-proxy(STALE)"
                logger.warning(
                    "gap-scan: prev session %s is %d days before %s — gaps reflect a "
                    "multi-day move, not an overnight gap. delivery_daily fills day by "
                    "day; gaps become accurate once it has the adjacent session.",
                    prev_day, gap_days, today,
                )

        opens = self._today_opens(today)
        if opens.empty:
            logger.warning("gap-scan: no opens for %s", today)
            return pd.DataFrame()

        # delivery_daily is keyed by SYMBOL; intraday proxy by security_id.
        prev_by_symbol = self._prev_ohlc_from_delivery(prev_day) if use_true_ohlc else {}
        prev_by_sid = {} if use_true_ohlc else self._prev_ohlc_from_intraday(prev_day)

        rows = []
        for _, o in opens.iterrows():
            sid = str(o["security_id"])
            symbol = str(o["symbol"])
            open_px = float(o["spot_price"])
            prev = prev_by_symbol.get(symbol.upper()) if use_true_ohlc else prev_by_sid.get(sid)
            if prev is None:
                continue
            prev_close = float(prev["close"])
            prev_high = float(prev["high"])
            prev_low = float(prev["low"])

            direction, extreme = classify_gap(
                open_px, prev_close, prev_high, prev_low, cfg.REQUIRE_RANGE_BREAK
            )
            if direction == "NONE":
                continue

            g = gap_pct(open_px, prev_close)
            rows.append(
                {
                    "security_id": sid,
                    "symbol": symbol,
                    "direction": direction,
                    "bias": "CE" if direction == "GAP_UP" else "PE",
                    "extreme": bool(extreme),
                    "gap_pct": round(g, 2),
                    "open": round(open_px, 2),
                    "prev_close": round(prev_close, 2),
                    "prev_high": round(prev_high, 2),
                    "prev_low": round(prev_low, 2),
                    "broke_range": (open_px > prev_high) if direction == "GAP_UP" else (open_px < prev_low),
                    "ohlc_source": source,
                }
            )

        if not rows:
            logger.info("gap-scan: no gaps >= %.2f%% on %s", cfg.GAP_PCT, today)
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["_abs"] = df["gap_pct"].abs()
        # Extreme (gap+range) first, then biggest gaps.
        df = df.sort_values(["extreme", "_abs"], ascending=[False, False]).drop(
            columns="_abs"
        ).reset_index(drop=True)
        logger.info(
            "gap-scan: %d gappers on %s (%d extreme) | OHLC=%s",
            len(df), today, int(df["extreme"].sum()), source,
        )
        return df

    # ---- persistence ------------------------------------------------------- #
    def _ensure_table(self) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {cfg.PERSIST_TABLE} (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    security_id TEXT NOT NULL,
                    symbol      TEXT,
                    timestamp   DATETIME NOT NULL,
                    direction   TEXT,
                    bias        TEXT,
                    extreme     INTEGER,
                    gap_pct     REAL,
                    open        REAL,
                    prev_close  REAL,
                    ohlc_source TEXT,
                    UNIQUE(security_id, timestamp)
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
                        (security_id, symbol, timestamp, direction, bias,
                         extreme, gap_pct, open, prev_close, ohlc_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(security_id, timestamp) DO NOTHING
                    """,
                    (
                        r["security_id"], r["symbol"], ts, r["direction"], r["bias"],
                        int(r["extreme"]), r["gap_pct"], r["open"], r["prev_close"],
                        r["ohlc_source"],
                    ),
                )
                n += cur.rowcount
            conn.commit()
        logger.info("gap-scan: persisted %d rows", n)
        return n

    # ---- alerting ---------------------------------------------------------- #
    def send_telegram(self, df: pd.DataFrame) -> None:
        src = df["ohlc_source"].iloc[0] if not df.empty else "n/a"
        lines = [
            "Extreme Opening Scanner (gap + range)",
            f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')} | prior OHLC: {src}",
        ]
        if "STALE" in str(src):
            lines.append("(prior session not adjacent — gaps may be multi-day; delivery_daily still filling)")
        if df.empty:
            lines.append(f"No gaps >= {cfg.GAP_PCT:.1f}% beyond prior range today.")
        else:
            extreme = df[df["extreme"]]
            shown = extreme if not extreme.empty else df
            if extreme.empty:
                lines.append("No gap+range setups; biggest gaps (range not broken):")
            else:
                lines.append(f"{len(extreme)} extreme open-drive setups:")
            for _, r in shown.head(cfg.TOP_N_ALERT).iterrows():
                lines.append(self._fmt(r))
        lines.append("\nGap-and-go is momentum; beware open-rejection. Confirm with a 15-min hold.")
        text = "\n".join(lines)

        if notifications.notify(text, parse_mode=None):
            logger.info("gap-scan: alert sent")
        else:
            logger.info("gap-scan: alert skipped; no channel configured")

    @staticmethod
    def _fmt(r) -> str:
        arrow = "UP" if r["direction"] == "GAP_UP" else "DN"
        brk = "broke range" if r["broke_range"] else "within range"
        return (
            f"[{arrow}] {r['symbol']:<12} gap {r['gap_pct']:+.2f}% -> {r['bias']} | "
            f"open {r['open']:.2f} vs PC {r['prev_close']:.2f} ({brk})"
        )


def get_latest_gap(security_id: str, db_path: str | None = None) -> dict:
    path = db_path or iv_store.DB_PATH
    try:
        with sqlite3.connect(path) as conn:
            cur = conn.execute(
                f"""
                SELECT direction, bias, extreme, gap_pct, open, prev_close, timestamp
                FROM   {cfg.PERSIST_TABLE}
                WHERE  security_id=? ORDER BY timestamp DESC LIMIT 1
                """,
                (str(security_id),),
            )
            row = cur.fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    keys = ["direction", "bias", "extreme", "gap_pct", "open", "prev_close", "timestamp"]
    return dict(zip(keys, row))


# build-verified: gap+range extreme opening scanner
