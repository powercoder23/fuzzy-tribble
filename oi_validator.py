# -*- coding: utf-8 -*-
"""
OI Validation Layer for the Break & Bounce strategy.

Runs immediately AFTER ``check_15min_breakout()`` returns BULLISH / BEARISH and
BEFORE the 5-min retest. It decides whether the breakout is supported by
futures positioning:

    15m Breakout  ->  OI Validator  ->  5m Retest  ->  Entry

Design rules honoured here
--------------------------
* Completely isolated: this module imports nothing from the strategy code. It
  only reads from the existing session client (``scanner.dhan``) and the local
  instrument master DB. It never touches entry logic, risk, order placement or
  the Telegram infrastructure.
* Minimal API usage: at most ONE intraday history call per confirmed breakout
  (breakouts are rare), reusing the already-authenticated Upstox session. The
  result is cached per futures contract for ``OI_CACHE_TTL_SEC``.
* Fail-open: any missing/unavailable data yields an ``ALLOW`` decision so the
  core strategy continues unchanged. Never blocks trading on missing OI.

Public surface
--------------
* ``classify(price_change, oi_change)``            -> classification string
* ``FuturesOIProvider``                            -> fetches futures price + OI
* ``OIValidator(scanner).validate(symbol, dir)``   -> OIValidationResult
* ``format_breakout_batch(breakouts)``             -> Telegram message (HTML)
* ``log_line(result)``                             -> compact log string
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import oi_config as cfg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure classification + scoring (no I/O — trivially unit-testable)
# ---------------------------------------------------------------------------

def classify(price_change: float, oi_change: float) -> str:
    """
    Map a (price change, OI change) pair to one of the four OI quadrants.

        price up   + OI up   -> LONG_BUILDUP
        price down + OI up   -> SHORT_BUILDUP
        price up   + OI down -> SHORT_COVERING
        price down + OI down -> LONG_UNWINDING

    "up" means strictly > 0; everything else counts as the "down/flat" side.
    """
    price_up = price_change > 0
    oi_up    = oi_change > 0
    if price_up and oi_up:
        return cfg.LONG_BUILDUP
    if (not price_up) and oi_up:
        return cfg.SHORT_BUILDUP
    if price_up and (not oi_up):
        return cfg.SHORT_COVERING
    return cfg.LONG_UNWINDING


def role_for(direction: str, classification: str) -> str:
    """Return the role (preferred/acceptable/weak/reject) for this direction."""
    return cfg.ROLE_BY_DIRECTION.get(direction, {}).get(classification, "reject")


def score_for(role: str) -> int:
    return cfg.SCORE_BY_ROLE.get(role, 0)


def is_allowed(role: str, strict: bool) -> bool:
    return role in cfg.ALLOWED_ROLES.get(bool(strict), set())


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class OISnapshot:
    """A point-in-time futures price + OI reading and its change vs a baseline."""
    futures_key:      str
    price_now:        float
    price_prev:       float
    oi_now:           float
    oi_prev:          float
    price_change_pct: float
    oi_change_pct:    float
    available:        bool = True
    source:           str = ""   # e.g. "intraday_open"


@dataclass
class OIValidationResult:
    """Everything the strategy needs for state, Telegram and logging."""
    symbol:           str
    direction:        str
    decision:         str            # "ALLOW" | "REJECT"
    classification:   str            # one of cfg.* classifications or NO_DATA
    role:             str            # preferred/acceptable/weak/reject/none
    score:            int
    confidence:       str            # Strong/Moderate/Weak/Rejected/Unknown
    available:        bool           # was real OI data used?
    price_change_pct: float = 0.0
    oi_change_pct:    float = 0.0
    breakout_level:   float = 0.0
    reason:           str = ""

    @property
    def approved(self) -> bool:
        return self.decision == "ALLOW"

    def as_state(self) -> dict:
        """The subset persisted inside the existing per-stock state dict."""
        return {
            "oi_classification": self.classification,
            "oi_score":          self.score,
            "oi_decision":       self.decision,
            "oi_confidence":     self.confidence,
            "oi_available":      self.available,
        }


# ---------------------------------------------------------------------------
# Futures OI provider
# ---------------------------------------------------------------------------

class FuturesOIProvider:
    """
    Resolves a symbol's nearest-expiry futures contract and reads its intraday
    price + OI from the existing Upstox session. All failures degrade to None.
    """

    def __init__(self, scanner=None, dhan=None, complete_db: str | None = None):
        # Accept either the DiscountedPremiumScanner (has .dhan) or a raw client.
        self._dhan = dhan if dhan is not None else getattr(scanner, "dhan", None)
        self._db   = complete_db or cfg.COMPLETE_DB
        self._cache: dict = {}   # futures_key -> (timestamp, OISnapshot)

    # -- instrument resolution ------------------------------------------------

    def resolve_futures_key(self, symbol: str) -> str | None:
        """
        Nearest-expiry NSE futures instrument_key for an underlying symbol.

        FUT rows in complete.db: exchange='NSE', instrument_type='FUT',
        underlying_symbol=<SYMBOL>, expiry stored as epoch-millis,
        instrument_key like 'NSE_FO|66355'. Works for both stock and index
        futures (NIFTY/BANKNIFTY/FINNIFTY resolve too).
        """
        if not symbol:
            return None
        try:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            conn = sqlite3.connect(self._db)
            try:
                cur = conn.cursor()
                cur.execute(
                    """SELECT instrument_key FROM instruments
                       WHERE underlying_symbol = ?
                         AND instrument_type   = 'FUT'
                         AND exchange          = 'NSE'
                         AND expiry            > ?
                       ORDER BY expiry
                       LIMIT 1""",
                    (symbol, now_ms),
                )
                row = cur.fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return row[0]
            logger.debug("OI: no futures contract for %s", symbol)
            return None
        except Exception:
            logger.debug("OI: futures key lookup failed for %s", symbol, exc_info=True)
            return None

    # -- candle fetch ---------------------------------------------------------

    def _fetch_intraday_candles(self, futures_key: str) -> list | None:
        """
        Raw intraday candles for the futures contract: [[ts,o,h,l,c,vol,oi], ...].

        Reaches into the existing Upstox session's history API (read-only). The
        adapter's own intraday helper drops the OI column, so we call the
        history API directly to keep the 7th (OI) field.
        """
        hist = getattr(self._dhan, "_history_api", None)
        if hist is None:
            logger.debug("OI: no history API on session client")
            return None
        try:
            resp = hist.get_intra_day_candle_data(
                instrument_key=futures_key,
                unit="minutes",
                interval=int(cfg.OI_CANDLE_INTERVAL),
            )
            candles = resp.data.candles if (resp and getattr(resp, "data", None)) else []
            return list(candles or [])
        except Exception:
            logger.debug("OI: intraday fetch failed for %s", futures_key, exc_info=True)
            return None

    def _fetch_daily_candles(self, futures_key: str) -> list | None:
        """Raw daily candles for the futures contract (used by prev_day mode)."""
        hist = getattr(self._dhan, "_history_api", None)
        if hist is None:
            return None
        try:
            resp = hist.get_historical_candle_data(
                instrument_key=futures_key,
                unit="days",
                interval=1,
                to_date=datetime.now().strftime("%Y-%m-%d"),
            )
            candles = resp.data.candles if (resp and getattr(resp, "data", None)) else []
            return list(candles or [])
        except Exception:
            logger.debug("OI: daily fetch failed for %s", futures_key, exc_info=True)
            return None

    # -- snapshot assembly ----------------------------------------------------

    @staticmethod
    def _row_ok(row) -> bool:
        return isinstance(row, (list, tuple)) and len(row) >= 7

    @staticmethod
    def _close(row) -> float:
        return float(row[4])

    @staticmethod
    def _oi(row) -> float:
        return float(row[6])

    def fetch_snapshot(self, symbol: str) -> OISnapshot | None:
        """
        Return an OISnapshot for the symbol's nearest futures, or None if OI is
        unavailable. Cached per futures contract for OI_CACHE_TTL_SEC.
        """
        key = self.resolve_futures_key(symbol)
        if not key:
            return None

        cached = self._cache.get(key)
        if cached and (time.monotonic() - cached[0]) < cfg.OI_CACHE_TTL_SEC:
            return cached[1]

        candles = self._fetch_intraday_candles(key)
        if not candles:
            return None

        # Keep only well-formed rows that carry the OI column, sorted oldest→newest.
        rows = [r for r in candles if self._row_ok(r)]
        if len(rows) < 2:
            return None
        rows.sort(key=lambda r: str(r[0]))

        current = rows[-1]
        mode    = cfg.OI_COMPARISON_MODE

        if mode == "prev_candle":
            baseline = rows[-2]
            source   = "prev_candle"
        elif mode == "prev_day":
            daily = self._fetch_daily_candles(key)
            daily = [r for r in (daily or []) if self._row_ok(r)]
            if not daily:
                return None
            daily.sort(key=lambda r: str(r[0]))
            # last row may be today's in-progress daily candle; prefer the
            # most recent row that is strictly before today.
            today = datetime.now().strftime("%Y-%m-%d")
            prior = [r for r in daily if str(r[0])[:10] < today]
            baseline = prior[-1] if prior else daily[-1]
            source   = "prev_day"
        else:  # "intraday_open" (default)
            baseline = rows[0]
            source   = "intraday_open"

        price_now  = self._close(current)
        price_prev = self._close(baseline)
        oi_now     = self._oi(current)
        oi_prev    = self._oi(baseline)

        # OI must be present and trustworthy on both ends.
        if oi_now <= 0 or oi_prev <= 0:
            logger.debug("OI: zero/absent OI for %s (now=%.0f prev=%.0f)",
                         symbol, oi_now, oi_prev)
            return None
        if min(oi_now, oi_prev) < cfg.OI_MIN_ABS_OI:
            return None
        if price_prev <= 0:
            return None

        snap = OISnapshot(
            futures_key      = key,
            price_now        = price_now,
            price_prev       = price_prev,
            oi_now           = oi_now,
            oi_prev          = oi_prev,
            price_change_pct = (price_now - price_prev) / price_prev * 100.0,
            oi_change_pct    = (oi_now - oi_prev) / oi_prev * 100.0,
            available        = True,
            source           = source,
        )
        self._cache[key] = (time.monotonic(), snap)
        return snap


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class OIValidator:
    """
    Orchestrates: snapshot -> classify -> role -> decision. Fail-open at every
    step. Construct once per runner and call ``validate`` per confirmed breakout.
    """

    def __init__(self, scanner=None, provider: FuturesOIProvider | None = None):
        self.provider = provider or FuturesOIProvider(scanner=scanner)

    def _fallback(self, symbol: str, direction: str, reason: str,
                  breakout_level: float = 0.0,
                  price_change: float = 0.0, oi_change: float = 0.0) -> OIValidationResult:
        return OIValidationResult(
            symbol           = symbol,
            direction        = direction,
            decision         = cfg.FALLBACK_DECISION,   # ALLOW
            classification   = cfg.NO_DATA,
            role             = "none",
            score            = cfg.FALLBACK_SCORE,
            confidence       = "Unknown",
            available        = False,
            price_change_pct = price_change,
            oi_change_pct    = oi_change,
            breakout_level   = breakout_level,
            reason           = reason,
        )

    def validate(self, symbol: str, direction: str,
                 breakout_level: float = 0.0) -> OIValidationResult:
        # Disabled → pure no-op pass-through.
        if not cfg.OI_VALIDATION_ENABLED:
            return self._fallback(symbol, direction, "disabled", breakout_level)

        if direction not in ("BULLISH", "BEARISH"):
            return self._fallback(symbol, direction, "bad_direction", breakout_level)

        # Fetch futures OI snapshot (fail-open).
        try:
            snap = self.provider.fetch_snapshot(symbol)
        except Exception:
            logger.debug("OI: snapshot raised for %s", symbol, exc_info=True)
            snap = None

        if snap is None or not snap.available:
            return self._fallback(symbol, direction, "oi_unavailable", breakout_level)

        # Dead-band: if both moves are tiny, don't block on noise.
        if (abs(snap.oi_change_pct) < cfg.OI_MIN_OI_CHANGE_PCT and
                abs(snap.price_change_pct) < cfg.OI_MIN_PRICE_CHANGE_PCT):
            return self._fallback(
                symbol, direction, "inconclusive", breakout_level,
                price_change=snap.price_change_pct, oi_change=snap.oi_change_pct,
            )

        classification = classify(snap.price_change_pct, snap.oi_change_pct)
        role           = role_for(direction, classification)
        score          = score_for(role)
        confidence     = cfg.CONFIDENCE_BY_ROLE.get(role, "Unknown")
        allowed        = is_allowed(role, cfg.OI_STRICT_MODE)

        decision = "ALLOW" if allowed else "REJECT"
        # Annotate-only mode: keep the classification/score but never void.
        if decision == "REJECT" and not cfg.OI_BLOCK_ON_REJECT:
            decision = "ALLOW"

        return OIValidationResult(
            symbol           = symbol,
            direction        = direction,
            decision         = decision,
            classification   = classification,
            role             = role,
            score            = score,
            confidence       = confidence,
            available        = True,
            price_change_pct = snap.price_change_pct,
            oi_change_pct    = snap.oi_change_pct,
            breakout_level   = breakout_level,
            reason           = "strict" if cfg.OI_STRICT_MODE else "normal",
        )


# ---------------------------------------------------------------------------
# Presentation helpers (Telegram + logging) — pure string builders
# ---------------------------------------------------------------------------

def _dir_label(direction: str) -> str:
    return "Bullish" if direction == "BULLISH" else "Bearish"


def log_line(result: OIValidationResult) -> str:
    """Compact, grep-friendly one-liner mirroring the spec's logging example."""
    return (
        f"OI {result.symbol} {_dir_label(result.direction)} | "
        f"Price Change: {result.price_change_pct:+.2f}% | "
        f"OI Change: {result.oi_change_pct:+.2f}% | "
        f"Classification: {result.classification} | "
        f"Decision: {'APPROVED' if result.approved else 'REJECTED'}"
        + ("" if result.available else f" | ({result.reason}, fallback)")
    )


def format_breakout_batch(breakouts: list) -> str:
    """
    Build a single HTML Telegram message for a batch of confirmed breakouts,
    enriched with OI analysis. Each item is a dict that carries the existing
    breakout fields plus the optional OI annotations attached by the runner:

        symbol, direction, level, candle_close,
        oi_classification, oi_confidence, oi_decision, oi_available,
        oi_price_change_pct, oi_oi_change_pct

    Sent via the existing notifier's generic ``send()`` primitive, so the core
    Telegram infrastructure is untouched.
    """
    count = len(breakouts)
    header = f"⚡ <b>BREAKOUT{'S' if count != 1 else ''} CONFIRMED ({count})</b>"
    blocks = []
    for b in breakouts:
        arrow = "🟢" if b.get("direction") == "BULLISH" else "🔴"
        label = "HIGH" if b.get("direction") == "BULLISH" else "LOW"
        lines = [
            f"{arrow} <b>{b.get('symbol', '')}</b> — close ₹{b.get('candle_close', 0):.2f} "
            f"vs {label} ₹{b.get('level', 0):.2f}"
        ]
        if b.get("oi_available"):
            cls   = b.get("oi_classification", cfg.NO_DATA)
            decis = b.get("oi_decision", "ALLOW")
            mark  = "✅" if decis == "ALLOW" else "🚫"
            lines.append(
                f"   OI: {cfg.CLASSIFICATION_LABEL.get(cls, cls)} "
                f"(ΔP {b.get('oi_price_change_pct', 0):+.1f}% / "
                f"ΔOI {b.get('oi_oi_change_pct', 0):+.1f}%)"
            )
            lines.append(
                f"   {mark} Confidence: {b.get('oi_confidence', 'Unknown')} → {decis}"
            )
        else:
            lines.append("   OI: unavailable (validation skipped)")
        blocks.append("\n".join(lines))

    body = "\n──────────────────────────────\n".join(blocks)
    return header + "\n──────────────────────────────\n" + body
