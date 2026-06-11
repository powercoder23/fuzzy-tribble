# -*- coding: utf-8 -*-
"""
Smart-Money (Bulk/Block) Scanner  (service: smart-money)

Turns NSE bulk & block deals into a next-day (BTST) directional bias for option
buyers. For each F&O symbol it nets institutional flow over the last few sessions:

    net_value_cr = sum(BUY value) - sum(SELL value)   across BULK (+BLOCK) deals
    net > 0  ->  accumulation -> CE bias
    net < 0  ->  distribution -> PE bias

Only names with |net| >= MIN_NET_VALUE_CR signal. Block crosses (matched buy+sell)
net to ~0 and self-filter. This is a catalyst + conviction signal — pair with
cheap IV (iv-rank) and buy with 4+ DTE per BTST rules.

Design rules (same isolation contract as the other scanners)
------------------------------------------------------------
* Reads ONLY iv_history.db (`deals` table from the deals collector, plus
  iv_history for the F&O symbol->security_id map). ZERO broker / NSE calls.
* SHORT deal_type rows (short-sell quantities, no buy/sell side) are ignored
  here — they belong to a future squeeze scanner.
* Fail-open: missing table / no qualifying deals -> empty result, never crashes.
* Touches no existing module's code or behaviour.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime

import pandas as pd

from collectors import iv_store
import smart_money_config as cfg

logger = logging.getLogger(__name__)


def _parse_deal_date(s: str):
    """deals.date is 'DD-Mon-YYYY' (e.g. '01-Jun-2026'). Return a date or None."""
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def net_bias(net_value_cr: float) -> str:
    return "CE" if net_value_cr > 0 else "PE"


class SmartMoneyScanner:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or iv_store.DB_PATH

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _has_deals(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='deals'"
            ).fetchone()
            if not row:
                return False
            return conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] > 0

    def _fno_symbols(self) -> set:
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
        if not self._has_deals():
            logger.warning("smart-money: deals table empty/missing — run the deals collector.")
            return pd.DataFrame()

        types = ("BULK", "BLOCK") if cfg.INCLUDE_BLOCK else ("BULK",)
        placeholders = ",".join("?" * len(types))
        with self._connect() as conn:
            df = pd.read_sql(
                f"""
                SELECT date, symbol, deal_type, client, trade_type, value_cr
                FROM   deals
                WHERE  deal_type IN ({placeholders})
                  AND  trade_type IN ('BUY','SELL')
                  AND  value_cr IS NOT NULL
                """,
                conn,
                params=types,
            )
        if df.empty:
            logger.info("smart-money: no BULK/BLOCK buy/sell deals with values")
            return pd.DataFrame()

        df["d"] = df["date"].map(_parse_deal_date)
        df = df.dropna(subset=["d"])
        if df.empty:
            return pd.DataFrame()

        # Keep only the last N distinct deal-dates.
        recent_dates = sorted(df["d"].unique())[-cfg.LOOKBACK_DAYS:]
        df = df[df["d"].isin(recent_dates)]

        fno = self._fno_symbols() if cfg.FNO_ONLY else None
        sid_map = self._symbol_to_sid()

        rows = []
        for symbol, g in df.groupby("symbol"):
            sym_u = str(symbol).upper()
            if fno is not None and sym_u not in fno:
                continue
            buy_val = float(g.loc[g["trade_type"] == "BUY", "value_cr"].sum())
            sell_val = float(g.loc[g["trade_type"] == "SELL", "value_cr"].sum())
            net = buy_val - sell_val
            if abs(net) < cfg.MIN_NET_VALUE_CR:
                continue
            clients = sorted({str(c) for c in g["client"] if c})
            rows.append(
                {
                    "security_id": sid_map.get(sym_u),
                    "symbol": sym_u,
                    "bias": net_bias(net),
                    "net_value_cr": round(net, 2),
                    "buy_value_cr": round(buy_val, 2),
                    "sell_value_cr": round(sell_val, 2),
                    "deals": int(len(g)),
                    "dates": ",".join(d.isoformat() for d in sorted(g["d"].unique())),
                    "top_clients": "; ".join(clients[:3]),
                }
            )

        if not rows:
            logger.info("smart-money: no F&O names with net flow >= %.1f cr", cfg.MIN_NET_VALUE_CR)
            return pd.DataFrame()

        out = pd.DataFrame(rows)
        out["_abs"] = out["net_value_cr"].abs()
        out = out.sort_values("_abs", ascending=False).drop(columns="_abs").reset_index(drop=True)
        logger.info(
            "smart-money: %d names | %d CE / %d PE",
            len(out), (out["bias"] == "CE").sum(), (out["bias"] == "PE").sum(),
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
                    net_value_cr  REAL,
                    buy_value_cr  REAL,
                    sell_value_cr REAL,
                    deals         INTEGER,
                    top_clients   TEXT,
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
                        (security_id, symbol, timestamp, bias, net_value_cr,
                         buy_value_cr, sell_value_cr, deals, top_clients)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, timestamp) DO NOTHING
                    """,
                    (
                        r["security_id"], r["symbol"], ts, r["bias"], r["net_value_cr"],
                        r["buy_value_cr"], r["sell_value_cr"], int(r["deals"]), r["top_clients"],
                    ),
                )
                n += cur.rowcount
            conn.commit()
        logger.info("smart-money: persisted %d rows", n)
        return n

    # ---- alerting ---------------------------------------------------------- #
    def send_telegram(self, df: pd.DataFrame) -> None:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            logger.info("smart-money: telegram skipped; creds missing")
            return

        lines = [
            "🏦 Smart-Money Scanner (bulk/block, BTST bias)",
            f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')} | last {cfg.LOOKBACK_DAYS} session(s)",
        ]
        if df.empty:
            lines.append("No F&O names with significant net institutional flow.")
        else:
            ce = df[df["bias"] == "CE"].head(cfg.TOP_N_ALERT)
            pe = df[df["bias"] == "PE"].head(cfg.TOP_N_ALERT)
            if not ce.empty:
                lines.append(f"\n🟢 Net buying → CE ({len(ce)}):")
                lines += [self._fmt(r) for _, r in ce.iterrows()]
            if not pe.empty:
                lines.append(f"\n🔴 Net selling → PE ({len(pe)}):")
                lines += [self._fmt(r) for _, r in pe.iterrows()]
        lines.append("\nℹ️ Disclosed post-close = next-day signal. Pair with cheap IV + 4+ DTE.")
        text = "\n".join(lines)

        try:
            import requests
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("smart-money: telegram alert sent")
        except Exception:
            logger.exception("smart-money: failed to send telegram alert")

    @staticmethod
    def _fmt(r) -> str:
        who = f" [{r['top_clients']}]" if r["top_clients"] else ""
        return (
            f"{r['symbol']:<12} net {r['net_value_cr']:+.1f}cr "
            f"(B {r['buy_value_cr']:.1f}/S {r['sell_value_cr']:.1f}, {r['deals']} deals){who}"
        )


def get_latest_smart_money(symbol: str, db_path: str | None = None) -> dict:
    path = db_path or iv_store.DB_PATH
    try:
        with sqlite3.connect(path) as conn:
            cur = conn.execute(
                f"""
                SELECT symbol, bias, net_value_cr, buy_value_cr, sell_value_cr,
                       deals, top_clients, timestamp
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
    keys = ["symbol", "bias", "net_value_cr", "buy_value_cr", "sell_value_cr",
            "deals", "top_clients", "timestamp"]
    return dict(zip(keys, row))
