# -*- coding: utf-8 -*-
"""
Composite Conviction Scanner  (service: composite)

Fuses the five zero-API scanners into ONE direction-aware conviction score per
F&O stock. The edge is *confluence*: a name where fresh OI buildup, institutional
deals, a delivery-% surge, and a gap all point the same way — while IV is cheap —
is a far higher-conviction option-buyer setup than any single factor alone.

Design rules (same isolation philosophy as iv_rank_scanner.py)
--------------------------------------------------------------
* Reads ONLY the persisted *_history tables the other scanners already write to
  iv_history.db. ZERO broker / option-chain calls. Safe to run anytime.
* Pure, side-effect-free scoring (score_symbol) — unit-testable with plain dicts.
* Fail-open: a missing factor table or symbol is treated as "no signal", never
  crashes a scan. The composite degrades gracefully as factors come online.
* Direction-aware: every directional factor votes CE/PE; IV-rank and VIX are
  conviction MODIFIERS, not votes.

Factor sources (all already in iv_history.db)
    oi_buildup     get_latest_buildup(security_id)   -> bias CE/PE   (direction)
    smart_money    get_latest_smart_money(symbol)    -> bias CE/PE   (catalyst)
    delivery_surge get_latest_surge(symbol)          -> bias CE/PE   (conviction)
    gap            get_latest_gap(security_id)        -> bias CE/PE   (trigger)
    iv_rank        get_latest_zone(security_id)       -> CHEAP/FAIR/EXPENSIVE (cost gate)
    vix_daily      latest close                       -> market regime (modifier)

Public surface
    score_symbol(factors, vix_regime) -> dict   (pure)
    CompositeScanner().scan()         -> ranked DataFrame (highest conviction first)
    CompositeScanner().persist(df)    -> writes composite_history rows
    CompositeScanner().send_telegram(df)
    get_latest_composite(security_id) -> dict   (for any strategy to gate on)
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

import pandas as pd

from collectors import iv_store
import notifications
import composite_config as cfg

# The five factor providers — each already zero-API. Imported defensively so a
# single unavailable/broken factor module degrades gracefully (fail-open) instead
# of taking down the whole composite scan.
def _opt_import(name):
    try:
        return __import__(name)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("composite: factor '%s' unavailable (%s)", name, exc)
        return None

oi_buildup_scanner    = _opt_import("oi_buildup_scanner")
smart_money_scanner   = _opt_import("smart_money_scanner")
delivery_surge_scanner = _opt_import("delivery_surge_scanner")
gap_scanner           = _opt_import("gap_scanner")
iv_rank_scanner       = _opt_import("iv_rank_scanner")

logger = logging.getLogger(__name__)

CE, PE, NONE = "CE", "PE", "NONE"
STRONG, MODERATE, WEAK = "STRONG", "MODERATE", "WEAK"


# --------------------------------------------------------------------------- #
# Pure scoring (no I/O — unit-testable)
# --------------------------------------------------------------------------- #
def _weights() -> dict:
    return {
        "oi":    cfg.W_OI_BUILDUP,
        "smart": cfg.W_SMART_MONEY,
        "deliv": cfg.W_DELIVERY_SURGE,
        "gap":   cfg.W_GAP,
    }


def score_symbol(factors: dict, vix_regime: str = "NORMAL") -> dict:
    """Fuse one symbol's factors into a conviction score.

    `factors` keys (any may be missing / None):
        oi, smart, deliv, gap : {"bias": "CE"|"PE", "strength": float 0..1} | None
        iv_zone               : "CHEAP" | "FAIR" | "EXPENSIVE" | None
    `vix_regime` : "ELEVATED" | "CALM" | "NORMAL"

    Returns a dict with direction, score (0-100), grade, and a breakdown.
    Returns score 0 / direction NONE when fewer than MIN_FACTORS vote.
    """
    w = _weights()
    ce_sum = pe_sum = 0.0
    votes_ce = votes_pe = 0
    contributing = []

    for key in ("oi", "smart", "deliv", "gap"):
        f = factors.get(key)
        if not f or f.get("bias") not in (CE, PE):
            continue
        strength = float(f.get("strength", 1.0) or 1.0)
        strength = max(0.0, min(1.0, strength))
        contrib = w[key] * strength
        contributing.append(key)
        if f["bias"] == CE:
            ce_sum += contrib
            votes_ce += 1
        else:
            pe_sum += contrib
            votes_pe += 1

    n_votes = votes_ce + votes_pe
    if n_votes < cfg.MIN_FACTORS:
        return {
            "direction": NONE, "score": 0.0, "grade": WEAK,
            "n_factors": n_votes, "contributing": contributing,
            "ce_sum": round(ce_sum, 3), "pe_sum": round(pe_sum, 3),
            "iv_zone": factors.get("iv_zone"), "vix_regime": vix_regime,
        }

    net = ce_sum - pe_sum
    direction = CE if net > 0 else PE if net < 0 else NONE
    denom = sum(w.values()) or 1.0
    base = abs(net) / denom * 100.0  # 0..100 before modifiers

    # Confluence bonus — reward factors agreeing on the WINNING side.
    agree = votes_ce if direction == CE else votes_pe
    if agree >= 4:
        base *= (1.0 + cfg.AGREE_BONUS_4)
    elif agree >= 3:
        base *= (1.0 + cfg.AGREE_BONUS_3)

    # IV-rank modifier (cost gate for buyers).
    iv_zone = factors.get("iv_zone")
    if iv_zone == "CHEAP":
        base *= (1.0 + cfg.IV_CHEAP_BOOST)
    elif iv_zone == "EXPENSIVE":
        base *= (1.0 - cfg.IV_EXPENSIVE_PEN)

    # VIX regime modifier (market-wide).
    if vix_regime == "ELEVATED":
        base *= (1.0 - cfg.VIX_HIGH_PENALTY)
    elif vix_regime == "CALM":
        base *= (1.0 + cfg.VIX_LOW_BOOST)

    score = max(0.0, min(100.0, base))
    grade = STRONG if score >= cfg.STRONG_MIN else MODERATE if score >= cfg.MODERATE_MIN else WEAK

    return {
        "direction": direction,
        "score": round(score, 1),
        "grade": grade,
        "n_factors": n_votes,
        "agree": agree,
        "contributing": contributing,
        "ce_sum": round(ce_sum, 3),
        "pe_sum": round(pe_sum, 3),
        "iv_zone": iv_zone,
        "vix_regime": vix_regime,
    }


# --------------------------------------------------------------------------- #
# Factor-normalisation helpers (map each scanner's dict -> {bias, strength})
# --------------------------------------------------------------------------- #
def _norm_oi(d: dict) -> dict | None:
    if not d or d.get("bias") not in (CE, PE):
        return None
    strength = 1.0 if str(d.get("strength", "")).upper() == "STRONG" else 0.6
    return {"bias": d["bias"], "strength": strength}


def _norm_gap(d: dict) -> dict | None:
    if not d or d.get("bias") not in (CE, PE):
        return None
    return {"bias": d["bias"], "strength": 1.0 if d.get("extreme") else 0.7}


def _norm_deliv(d: dict) -> dict | None:
    if not d or d.get("bias") not in (CE, PE):
        return None
    surge = float(d.get("surge_x") or 1.0)
    return {"bias": d["bias"], "strength": max(0.5, min(1.0, (surge - 1.0) / 2.0 + 0.5))}


def _norm_smart(d: dict) -> dict | None:
    if not d or d.get("bias") not in (CE, PE):
        return None
    net = abs(float(d.get("net_value_cr") or 0.0))
    return {"bias": d["bias"], "strength": max(0.5, min(1.0, net / 20.0))}


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #
class CompositeScanner:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or iv_store.DB_PATH

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _universe(self) -> pd.DataFrame:
        """Distinct (security_id, symbol) seen by the collector."""
        with self._connect() as conn:
            return pd.read_sql(
                "SELECT security_id, MAX(symbol) AS symbol "
                "FROM iv_history GROUP BY security_id",
                conn,
            )

    def _vix_regime(self) -> tuple[str, float | None]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT close FROM vix_daily ORDER BY date DESC LIMIT 1"
                ).fetchone()
        except sqlite3.OperationalError:
            return "NORMAL", None
        if not row or row[0] is None:
            return "NORMAL", None
        vix = float(row[0])
        if vix >= cfg.VIX_HIGH:
            return "ELEVATED", vix
        if vix <= cfg.VIX_LOW:
            return "CALM", vix
        return "NORMAL", vix

    def scan(self) -> pd.DataFrame:
        self._ensure_table()
        universe = self._universe()
        vix_regime, vix_val = self._vix_regime()
        logger.info("composite: VIX regime=%s (%.1f)", vix_regime, vix_val or -1)

        rows = []
        for _, u in universe.iterrows():
            sid = str(u["security_id"])
            symbol = u["symbol"]
            if not symbol:
                continue

            # Pull each factor fail-open (helpers return {} when table absent or
            # the factor module failed to import).
            factors = {
                "oi":      _norm_oi(_safe(oi_buildup_scanner, "get_latest_buildup", sid)),
                "gap":     _norm_gap(_safe(gap_scanner, "get_latest_gap", sid)),
                "smart":   _norm_smart(_safe(smart_money_scanner, "get_latest_smart_money", symbol)),
                "deliv":   _norm_deliv(_safe(delivery_surge_scanner, "get_latest_surge", symbol)),
                "iv_zone": (_safe(iv_rank_scanner, "get_latest_zone", sid) or {}).get("zone"),
            }

            res = score_symbol(factors, vix_regime)
            if res["direction"] == NONE or res["grade"] == WEAK:
                continue

            rows.append({
                "security_id": sid,
                "symbol": symbol,
                "direction": res["direction"],
                "score": res["score"],
                "grade": res["grade"],
                "n_factors": res["n_factors"],
                "agree": res.get("agree", 0),
                "contributing": ",".join(res["contributing"]),
                "iv_zone": res["iv_zone"] or "-",
                "vix_regime": vix_regime,
            })

        if not rows:
            logger.warning("composite: no symbol cleared MIN_FACTORS=%d / WEAK floor", cfg.MIN_FACTORS)
            return pd.DataFrame()

        df = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
        logger.info("composite: %d ranked | %d STRONG | %d CE / %d PE",
                    len(df), (df["grade"] == STRONG).sum(),
                    (df["direction"] == CE).sum(), (df["direction"] == PE).sum())
        return df

    # ---- persistence ------------------------------------------------------- #
    def _ensure_table(self) -> None:
        with self._connect() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {cfg.PERSIST_TABLE} (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    security_id  TEXT NOT NULL,
                    symbol       TEXT,
                    timestamp    DATETIME NOT NULL,
                    direction    TEXT,
                    score        REAL,
                    grade        TEXT,
                    n_factors    INTEGER,
                    contributing TEXT,
                    iv_zone      TEXT,
                    vix_regime   TEXT,
                    UNIQUE(security_id, timestamp)
                )
            """)
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_cmp_sid_time "
                         f"ON {cfg.PERSIST_TABLE}(security_id, timestamp)")
            conn.commit()

    def persist(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        self._ensure_table()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        inserted = 0
        with self._connect() as conn:
            for _, r in df.iterrows():
                cur = conn.execute(
                    f"""INSERT INTO {cfg.PERSIST_TABLE}
                        (security_id, symbol, timestamp, direction, score, grade,
                         n_factors, contributing, iv_zone, vix_regime)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(security_id, timestamp) DO NOTHING""",
                    (r["security_id"], r["symbol"], ts, r["direction"], r["score"],
                     r["grade"], int(r["n_factors"]), r["contributing"],
                     r["iv_zone"], r["vix_regime"]),
                )
                inserted += cur.rowcount
            conn.commit()
        logger.info("composite: persisted %d rows to %s", inserted, cfg.PERSIST_TABLE)
        return inserted

    # ---- alerting ---------------------------------------------------------- #
    def send_telegram(self, df: pd.DataFrame) -> None:
        lines = [
            "🎯 Composite Conviction Scanner",
            f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ]
        if df.empty:
            lines.append("No multi-factor confluence today (need "
                         f">= {cfg.MIN_FACTORS} aligned factors).")
        else:
            ce = df[df["direction"] == CE].head(cfg.TOP_N_ALERT)
            pe = df[df["direction"] == PE].head(cfg.TOP_N_ALERT)
            if not ce.empty:
                lines.append(f"\n🟢 CE conviction ({len(df[df.direction==CE])}):")
                lines += [self._fmt_row(r) for _, r in ce.iterrows()]
            if not pe.empty:
                lines.append(f"\n🔴 PE conviction ({len(df[df.direction==PE])}):")
                lines += [self._fmt_row(r) for _, r in pe.iterrows()]
        lines.append("\nℹ️ Confluence of OI + deals + delivery + gap, IV/VIX adjusted. "
                     "Still confirm a clean entry + 4+ DTE.")
        text = "\n".join(lines)
        if notifications.notify(text, parse_mode=None):
            logger.info("composite: alert sent")
        else:
            logger.info("composite: alert skipped; no channel configured")

    @staticmethod
    def _fmt_row(r) -> str:
        return (f"{r['symbol']:<12} {r['score']:>5.1f} {r['grade'][:4]:<4} "
                f"[{r['n_factors']}f: {r['contributing']}] IV {r['iv_zone']}")


def _safe(module, fnname, key):
    """Call module.fnname(key), swallowing any error / missing module -> {} (fail-open)."""
    if module is None:
        return {}
    try:
        return getattr(module, fnname)(key) or {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Consumer helper — any strategy can gate on the latest composite conviction.
# --------------------------------------------------------------------------- #
def get_latest_composite(security_id: str, db_path: str | None = None) -> dict:
    path = db_path or iv_store.DB_PATH
    try:
        with sqlite3.connect(path) as conn:
            cur = conn.execute(
                f"""SELECT direction, score, grade, n_factors, contributing,
                           iv_zone, vix_regime, timestamp
                    FROM {cfg.PERSIST_TABLE}
                    WHERE security_id = ?
                    ORDER BY timestamp DESC LIMIT 1""",
                (str(security_id),),
            )
            row = cur.fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    keys = ["direction", "score", "grade", "n_factors", "contributing",
            "iv_zone", "vix_regime", "timestamp"]
    return dict(zip(keys, row))
