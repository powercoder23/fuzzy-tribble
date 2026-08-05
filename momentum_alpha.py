# -*- coding: utf-8 -*-
"""Momentum alpha engine — signal-quality layer for the ORB/VWAP strategy.

Everything here is derived from data already on disk. No broker calls, no new
market data feeds:

    delivery_daily   (iv_history.db)   daily OHLCV, 211 F&O names
    candles_5m       (iv_history.db)   5-min OHLCV incl. NIFTY (security_id 13)
    sector_mapping.db                  symbol -> industry
    oi_buildup_history / scanner       OI 2x2 classification
    breadth.py                         market + sector breadth

Design rule: SELECTION PRECEDES SIGNAL. The daily ranker decides *which* names
are worth watching; the trigger only decides *when*. Every public method is
fail-open — missing data yields a neutral reading, never an exception and never
a fabricated edge.

Nothing in this module places orders, sizes positions, or writes to any table.
"""

import contextlib
import logging
import sqlite3
from datetime import date

from momentum_config import (
    ATR, BREAKOUT, BREADTH_GATE, CONFIDENCE, IV_HISTORY_DB,
    RS, RVOL, VWAP_Q, WEIGHTS,
)

logger = logging.getLogger(__name__)

# NIFTY 5-min closes live in candles_5m under this security_id. The `symbol`
# column there is mislabeled for the index, so we key on the id (same note as
# engine/config.py INDEX_SECURITY_ID).
INDEX_SECURITY_ID = "13"

SESSION_START = "09:15"


@contextlib.contextmanager
def _connect(db_path):
    """Shared WAL/busy_timeout connection when available, else plain sqlite3.

    Always CLOSES on exit. `with sqlite3.connect(...)` only commits — it leaves
    the handle open, which leaks descriptors in a long-running scan loop that
    queries once per symbol per cycle.
    """
    try:
        from collectors import iv_store
        conn = iv_store.connect(db_path)
    except Exception:
        conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _clip01(x):
    return max(0.0, min(1.0, x))


def _pct_rank(value, population):
    """Percentile rank of `value` within `population`, 0-100."""
    if not population:
        return None
    below = sum(1 for p in population if p < value)
    return below / len(population) * 100.0


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


# ---------------------------------------------------------------------------
# CLASS A1: DailyUniverseRanker  (Relative Strength + ATR expansion)
# ---------------------------------------------------------------------------

class DailyUniverseRanker:
    """Cross-sectional ranking of the F&O universe from delivery_daily.

    Momentum is a *relative* phenomenon: what matters is not that a stock rose,
    but that it rose more than its peers. This produces, per symbol:

        ret_pct        N-day % return
        rs_pct         percentile rank of that return across the universe
        atr_pct        Wilder ATR(14) as % of price — movement capacity
        atr_expansion  mean TR% (fast window) / mean TR% (slow window)

    Zero broker calls; one SQL read per premarket.
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or IV_HISTORY_DB
        self._cache = None
        self._cache_day = None

    # -- data ------------------------------------------------------------
    def _load_bars(self, sessions):
        """{symbol: [(date, high, low, close), ...]} ascending, last N sessions."""
        out = {}
        try:
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    """SELECT symbol, date, high, low, close FROM delivery_daily
                       WHERE date >= (SELECT MIN(date) FROM (
                                SELECT DISTINCT date FROM delivery_daily
                                ORDER BY date DESC LIMIT ?))
                         AND close > 0
                       ORDER BY symbol, date""",
                    (int(sessions),),
                ).fetchall()
        except sqlite3.OperationalError:
            logger.warning("DailyUniverseRanker: delivery_daily unavailable")
            return out
        except Exception:
            logger.exception("DailyUniverseRanker: bar load failed")
            return out

        for sym, d, h, l, c in rows:
            if sym is None or c is None:
                continue
            out.setdefault(str(sym).strip().upper(), []).append(
                (d, float(h or 0), float(l or 0), float(c)))
        return out

    # -- indicators ------------------------------------------------------
    @staticmethod
    def _true_ranges(bars):
        """[(TR, close)] using the previous close, oldest first."""
        trs = []
        for i in range(1, len(bars)):
            _, h, l, c = bars[i]
            pc = bars[i - 1][3]
            trs.append((max(h - l, abs(h - pc), abs(l - pc)), c))
        return trs

    @classmethod
    def _atr_pct(cls, bars, period):
        """Wilder ATR as % of the latest close. None when history is short."""
        trs = cls._true_ranges(bars)
        if len(trs) < period:
            return None
        atr = sum(t for t, _ in trs[:period]) / period       # seed
        for t, _ in trs[period:]:                            # Wilder smoothing
            atr = (atr * (period - 1) + t) / period
        close = trs[-1][1]
        return (atr / close * 100.0) if close > 0 else None

    @classmethod
    def _atr_expansion(cls, bars, fast, slow):
        """mean TR% over `fast` days / mean TR% over `slow` days."""
        trs = cls._true_ranges(bars)
        if len(trs) < slow:
            return None
        def mean_tr_pct(window):
            vals = [(t / c * 100.0) for t, c in trs[-window:] if c > 0]
            return (sum(vals) / len(vals)) if vals else None
        f, s = mean_tr_pct(fast), mean_tr_pct(slow)
        if not f or not s:
            return None
        return f / s

    # -- public ----------------------------------------------------------
    def rank(self, force=False):
        """{SYMBOL: {ret_pct, rs_pct, atr_pct, atr_expansion}}. Cached per day."""
        today = date.today().isoformat()
        if not force and self._cache is not None and self._cache_day == today:
            return self._cache

        lookback = int(RS["lookback_days"])
        need = max(lookback + 2, int(ATR["expansion_slow"]) + 2,
                   int(ATR["period"]) + 2)
        bars = self._load_bars(need + 5)

        metrics = {}
        for sym, series in bars.items():
            if len(series) < lookback + 1:
                continue
            close_now = series[-1][3]
            close_then = series[-1 - lookback][3]
            if close_then <= 0:
                continue
            metrics[sym] = {
                "ret_pct": (close_now / close_then - 1.0) * 100.0,
                "atr_pct": self._atr_pct(series, int(ATR["period"])),
                "atr_expansion": self._atr_expansion(
                    series, int(ATR["expansion_fast"]), int(ATR["expansion_slow"])),
            }

        population = [m["ret_pct"] for m in metrics.values()]
        for m in metrics.values():
            m["rs_pct"] = _pct_rank(m["ret_pct"], population)

        logger.info("DailyUniverseRanker: ranked %d symbols over %dd lookback",
                    len(metrics), lookback)
        self._cache, self._cache_day = metrics, today
        return metrics

    def passes_universe_filter(self, symbol, side, ranked=None):
        """(ok, reason) — the premarket cross-sectional gate.

        Fail-open: an unranked symbol or an undersized universe never blocks.
        """
        ranked = self.rank() if ranked is None else ranked
        if len(ranked) < int(RS["min_universe"]):
            return True, "rs_universe_too_small"
        m = ranked.get(str(symbol).strip().upper())
        if not m or m.get("rs_pct") is None:
            return True, "rs_unranked"

        rs_pct = m["rs_pct"]
        floor = float(RS["min_percentile"])
        if side == "CE" and rs_pct < floor:
            return False, f"rs_weak({rs_pct:.0f}<{floor:.0f})"
        if side == "PE" and rs_pct > (100.0 - floor):
            return False, f"rs_strong({rs_pct:.0f}>{100.0 - floor:.0f})"

        atr_pct = m.get("atr_pct")
        if atr_pct is not None and atr_pct < float(ATR["min_atr_pct"]):
            return False, f"atr_dead({atr_pct:.2f}%)"

        expansion = m.get("atr_expansion")
        if expansion is not None and expansion < float(ATR["min_expansion"]):
            return False, f"atr_contracting({expansion:.2f})"

        return True, "ok"


# ---------------------------------------------------------------------------
# CLASS A2: RelativeVolume  (time-of-day RVOL)
# ---------------------------------------------------------------------------

class RelativeVolume:
    """Cumulative volume so far today vs the same clock time on prior sessions.

    This replaces the "mean of the previous 5 candles" baseline, which is
    time-of-day biased: at 09:45 the trailing window contains the opening
    surge (ratio suppressed), at 13:00 it contains lunchtime lull (ratio
    inflated), so one threshold means different things at different times.
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or IV_HISTORY_DB

    def rvol(self, security_id, now_hhmm, sessions=None, day=None):
        """Today's cum volume / median prior-session cum volume at the same time.

        Returns None when the baseline is too thin to trust (never 0.0, so the
        caller can tell "no data" apart from "no participation").
        """
        sessions = int(sessions or RVOL["sessions"])
        day = day or date.today().isoformat()
        try:
            with _connect(self.db_path) as conn:
                rows = conn.execute(
                    """SELECT substr(ts,1,10) AS d, SUM(volume) AS v
                       FROM candles_5m
                       WHERE security_id = ?
                         AND substr(ts,1,10) <= ?
                         AND substr(ts,12,5) <= ?
                       GROUP BY d ORDER BY d DESC LIMIT ?""",
                    (str(security_id), day, str(now_hhmm), sessions + 1),
                ).fetchall()
        except sqlite3.OperationalError:
            return None
        except Exception:
            logger.debug("rvol: query failed for %s", security_id, exc_info=True)
            return None

        if not rows or rows[0][0] != day:
            return None                      # no data for today yet
        today_vol = float(rows[0][1] or 0.0)
        prior = [float(v or 0.0) for d, v in rows[1:] if v]
        if len(prior) < int(RVOL["min_sessions"]) or today_vol <= 0:
            return None
        base = _median(prior)
        if not base or base <= 0:
            return None
        return today_vol / base


# ---------------------------------------------------------------------------
# CLASS A3: IntradayRelativeStrength  (stock vs NIFTY, live)
# ---------------------------------------------------------------------------

class IntradayRelativeStrength:
    """Today's stock return minus NIFTY's, both measured from the open.

    The daily ranker says which names led *yesterday*; this says which are
    leading *right now*. Reads candles_5m only.
    """

    def __init__(self, db_path=None, index_security_id=INDEX_SECURITY_ID):
        self.db_path = db_path or IV_HISTORY_DB
        self.index_security_id = str(index_security_id)
        self._index_cache = None
        self._index_cache_key = None

    def _day_return(self, conn, security_id, day):
        row = conn.execute(
            """SELECT (SELECT open  FROM candles_5m
                        WHERE security_id=? AND substr(ts,1,10)=?
                        ORDER BY ts ASC  LIMIT 1),
                      (SELECT close FROM candles_5m
                        WHERE security_id=? AND substr(ts,1,10)=?
                        ORDER BY ts DESC LIMIT 1)""",
            (str(security_id), day, str(security_id), day),
        ).fetchone()
        if not row or not row[0] or not row[1]:
            return None
        o, c = float(row[0]), float(row[1])
        return ((c / o) - 1.0) * 100.0 if o > 0 else None

    def rs(self, security_id, day=None):
        """Stock %move - NIFTY %move since the open, in percentage points."""
        day = day or date.today().isoformat()
        try:
            with _connect(self.db_path) as conn:
                if self._index_cache_key != day:
                    self._index_cache = self._day_return(
                        conn, self.index_security_id, day)
                    self._index_cache_key = day
                idx = self._index_cache
                if idx is None:
                    return None
                stock = self._day_return(conn, security_id, day)
        except sqlite3.OperationalError:
            return None
        except Exception:
            logger.debug("intraday RS failed for %s", security_id, exc_info=True)
            return None
        return None if stock is None else (stock - idx)


# ---------------------------------------------------------------------------
# Trigger quality — pure functions over completed candles
# ---------------------------------------------------------------------------

def _bar(df_row):
    """Normalise a pandas row or dict into a plain OHLCV dict."""
    get = df_row.get if hasattr(df_row, "get") else (lambda k, d=None: df_row[k])
    return {
        "open": float(get("open", 0.0) or 0.0),
        "high": float(get("high", 0.0) or 0.0),
        "low": float(get("low", 0.0) or 0.0),
        "close": float(get("close", 0.0) or 0.0),
        "volume": float(get("volume", 0.0) or 0.0),
    }


def breakout_quality(bars, breakout_idx, level, direction):
    """Score an ORB breakout 0-1 on structure, not just "price crossed".

    Four independent components, equally weighted:

      coil        the bars before the break were contracting (a real base)
      volume      the breakout bar carried expansion vs its own recent norm
      close_loc   the bar closed at its extreme, not mid-range (no long wick)
      follow      the break cleared the level by a meaningful fraction of range

    A wide-range drifting bar that merely tags the level scores near 0; a tight
    coil resolving on 2x volume with a strong close scores near 1.
    """
    try:
        bars = [_bar(b) for b in bars]
        n = len(bars)
        if n < 3 or breakout_idx <= 0 or breakout_idx >= n:
            return 0.0
        brk = bars[breakout_idx]
        coil_n = int(BREAKOUT["coil_bars"])

        # -- coil: range of the bars immediately before the break vs the ones
        #    before those. Contraction into a level is what makes it a base.
        pre = bars[max(0, breakout_idx - coil_n):breakout_idx]
        older = bars[max(0, breakout_idx - 2 * coil_n):max(0, breakout_idx - coil_n)]
        coil_score = 0.0
        if len(pre) >= 2 and len(older) >= 2:
            r_pre = max(b["high"] for b in pre) - min(b["low"] for b in pre)
            r_old = max(b["high"] for b in older) - min(b["low"] for b in older)
            if r_old > 0:
                ratio = r_pre / r_old
                # ratio <= nr_contraction is a full pass; 1.0+ scores nothing.
                span = max(1e-9, 1.0 - float(BREAKOUT["nr_contraction"]))
                coil_score = _clip01((1.0 - ratio) / span)

        # -- volume expansion on the breakout bar vs the coil
        vol_score = 0.0
        prior_vols = [b["volume"] for b in pre if b["volume"] > 0]
        if prior_vols:
            vr = brk["volume"] / (sum(prior_vols) / len(prior_vols))
            need = float(BREAKOUT["min_vol_expansion"])
            vol_score = _clip01((vr - 1.0) / max(1e-9, need - 1.0))

        # -- close location within the breakout bar's own range
        rng = brk["high"] - brk["low"]
        if rng > 0:
            loc = (brk["close"] - brk["low"]) / rng
            if direction == "PE":
                loc = 1.0 - loc
            floor = float(BREAKOUT["min_close_loc"])
            close_score = _clip01((loc - floor) / max(1e-9, 1.0 - floor))
        else:
            close_score = 0.0

        # -- follow-through beyond the level, scaled by the bar's own range
        follow_score = 0.0
        if rng > 0 and level:
            beyond = (brk["close"] - level) if direction == "CE" else (level - brk["close"])
            follow_score = _clip01(beyond / rng)

        return round((coil_score + vol_score + close_score + follow_score) / 4.0, 3)
    except Exception:
        logger.debug("breakout_quality failed", exc_info=True)
        return 0.0


def vwap_quality(bars, vwaps, direction):
    """Score a VWAP signal 0-1 on slope, acceptance and separation.

    A bare crossover is close to a coin flip in chop. This demands the VWAP
    itself be sloping the right way, that price has *stayed* on the signal side
    for more than one bar, and that it is not sitting on top of the line.
    """
    try:
        bars = [_bar(b) for b in bars]
        vwaps = [float(v) for v in vwaps]
        n = min(len(bars), len(vwaps))
        if n < max(int(VWAP_Q["slope_bars"]), int(VWAP_Q["acceptance_bars"])) + 1:
            return 0.0
        bars, vwaps = bars[-n:], vwaps[-n:]
        last, last_vwap = bars[-1], vwaps[-1]
        if last["close"] <= 0:
            return 0.0

        # -- slope of the VWAP line itself, % of price
        sb = int(VWAP_Q["slope_bars"])
        slope = (vwaps[-1] - vwaps[-1 - sb]) / last["close"] * 100.0
        if direction == "PE":
            slope = -slope
        slope_score = _clip01(slope / max(1e-9, float(VWAP_Q["min_slope_pct"]) * 2.0))

        # -- acceptance: how many of the last N closes HELD the signal side.
        #    A close hovering on top of the line is chop, not acceptance, so a
        #    bar only counts if it also separated by a real fraction of its own
        #    range. Without this, flat price + flat VWAP scores full marks.
        ab = int(VWAP_Q["acceptance_bars"])
        held = 0
        for i in range(-ab, 0):
            gap = bars[i]["close"] - vwaps[i]
            if (gap > 0) != (direction == "CE"):
                continue
            bar_range = bars[i]["high"] - bars[i]["low"]
            if bar_range > 0 and abs(gap) < 0.1 * bar_range:
                continue                      # sitting on the line
            held += 1
        acc_score = _clip01(held / max(1, ab))

        # -- separation from the line, scaled by recent bar range. SIGNED: being
        #    far from VWAP on the wrong side is the opposite of confirmation,
        #    so only distance in the trade's favour earns anything.
        ranges = [b["high"] - b["low"] for b in bars[-ab:] if b["high"] > b["low"]]
        avg_range = (sum(ranges) / len(ranges)) if ranges else 0.0
        gap = last["close"] - last_vwap
        if direction == "PE":
            gap = -gap
        dist_score = _clip01(gap / avg_range) if avg_range > 0 else 0.0

        return round((slope_score + acc_score + dist_score) / 3.0, 3)
    except Exception:
        logger.debug("vwap_quality failed", exc_info=True)
        return 0.0


# ---------------------------------------------------------------------------
# Context adapters — reuse the modules that already exist
# ---------------------------------------------------------------------------

def breadth_context(symbol):
    """{market_pct, sector_pct, sector} from breadth.py. Empty dict on failure."""
    try:
        import breadth
        snap = breadth.compute()
        _, sector = snap.sector_for(symbol)
        return {
            "market_pct": snap.market_pct,
            "sector_pct": (sector or {}).get("pct"),
            "sector_n": (sector or {}).get("n", 0),
            "sector": (sector or {}).get("name"),
        }
    except Exception:
        logger.debug("breadth unavailable", exc_info=True)
        return {}


def breadth_blocks(side, ctx):
    """(block, reason) using the momentum-local thresholds. Fail-open."""
    if not BREADTH_GATE["enabled"] or not ctx:
        return False, "breadth_gate_off"
    m = ctx.get("market_pct")
    if m is None:
        return False, "no_breadth"
    if side == "CE" and m < float(BREADTH_GATE["min_for_ce"]):
        return True, f"market_breadth({m:.0f}%)_vs_CE"
    if side == "PE" and m > float(BREADTH_GATE["max_for_pe"]):
        return True, f"market_breadth({m:.0f}%)_vs_PE"

    if BREADTH_GATE["sector_enabled"]:
        sp, sn = ctx.get("sector_pct"), ctx.get("sector_n", 0)
        if sp is not None and sn >= int(BREADTH_GATE["sector_min_names"]):
            if side == "CE" and sp < float(BREADTH_GATE["sector_min_ce"]):
                return True, f"sector_breadth({sp:.0f}%)_vs_CE"
            if side == "PE" and sp > float(BREADTH_GATE["sector_max_pe"]):
                return True, f"sector_breadth({sp:.0f}%)_vs_PE"
    return False, "ok"


def oi_context(security_id):
    """OI 2x2 reading from the existing scanner. Empty dict when unavailable."""
    try:
        import oi_buildup_scanner
        d = oi_buildup_scanner.get_latest_buildup(str(security_id)) or {}
        return {"bias": d.get("bias"),
                "classification": d.get("classification"),
                "strength": d.get("strength")}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# CLASS A4: MomentumConvictionScorer
# ---------------------------------------------------------------------------

class MomentumConvictionScorer:
    """Weighted 0-100 confidence from continuous factor readings.

    Replaces the old fixed ladder (40 regime + 30 alignment + 10 ORB + 5 volume),
    whose "+30 alignment" was constant for every surviving signal and whose total
    range was effectively three bits. Each factor here contributes a *continuous*
    aligned strength in [0,1] scaled by its configured weight, and the total is
    normalised by the weights actually available — so a missing factor dilutes
    confidence rather than silently scoring zero.
    """

    def __init__(self, weights=None):
        self.weights = dict(weights or WEIGHTS)

    @staticmethod
    def _directional(value_0_100, direction):
        """Map a 0-100 'bullishness' onto aligned strength, neutral at 50."""
        if value_0_100 is None:
            return None
        aligned = value_0_100 / 100.0
        if direction == "PE":
            aligned = 1.0 - aligned
        return _clip01((aligned - 0.5) * 2.0)

    def factor_strengths(self, ctx, direction):
        """{factor: strength in [0,1] or None if the input was unavailable}."""
        f = {}
        f["relative_strength"] = self._directional(ctx.get("rs_pct"), direction)
        bq = ctx.get("breakout_quality")
        f["breakout_quality"] = None if bq is None else _clip01(bq)

        rvol = ctx.get("rvol")
        if rvol is None:
            f["rvol"] = None
        else:
            lo, hi = float(RVOL["min_rvol"]), float(RVOL["saturate_at"])
            f["rvol"] = _clip01((rvol - lo) / max(1e-9, hi - lo))

        exp = ctx.get("atr_expansion")
        if exp is None:
            f["atr_expansion"] = None
        else:
            lo = float(ATR["min_expansion"])
            f["atr_expansion"] = _clip01((exp - lo) / 0.4)

        f["sector_breadth"] = self._directional(ctx.get("sector_pct"), direction)
        f["market_breadth"] = self._directional(ctx.get("market_pct"), direction)

        strength = str(ctx.get("regime_strength") or "").upper()
        f["regime"] = 1.0 if strength == "STRONG" else 0.5 if strength == "WEAK" else None

        oi_bias = ctx.get("oi_bias")
        if oi_bias in ("CE", "PE"):
            fresh = str(ctx.get("oi_strength") or "").upper() == "STRONG"
            f["oi_buildup"] = (1.0 if fresh else 0.5) if oi_bias == direction else 0.0
        else:
            f["oi_buildup"] = None
        return f

    def score(self, ctx, direction):
        """{confidence, breakdown, n_agree, n_missing} — confidence is 0-100."""
        strengths = self.factor_strengths(ctx, direction)
        total = 0.0
        denom = 0.0
        breakdown = {}
        n_agree = 0
        n_missing = 0

        for name, weight in self.weights.items():
            s = strengths.get(name)
            if s is None:
                n_missing += 1
                breakdown[name] = None
                continue
            if weight <= 0:
                breakdown[name] = 0.0      # journalled, deliberately unweighted
                continue
            contrib = weight * s
            total += contrib
            denom += weight
            breakdown[name] = round(contrib, 2)
            if s > 0.5:
                n_agree += 1

        confidence = round(total / denom * 100.0, 1) if denom > 0 else 0.0
        return {"confidence": confidence, "breakdown": breakdown,
                "n_agree": n_agree, "n_missing": n_missing,
                "strengths": {k: (round(v, 3) if v is not None else None)
                              for k, v in strengths.items()}}

    @staticmethod
    def passes(result):
        """(ok, reason) against CONFIDENCE['min_score']. observe_only never blocks."""
        floor = float(CONFIDENCE["min_score"])
        conf = result.get("confidence", 0.0)
        if CONFIDENCE.get("observe_only"):
            return True, f"observe_only(conf={conf:.0f})"
        if conf < floor:
            return False, f"low_confidence({conf:.0f}<{floor:.0f})"
        return True, "ok"


def format_factor_note(result, ctx):
    """Compact one-line factor trace for the trade journal's `notes` column.

    Packing the breakdown into the existing free-text column keeps the journal
    schema (and every downstream reader of momentum_trades.csv) unchanged.
    """
    try:
        bits = [f"conf={result.get('confidence', 0):.0f}"]
        for key, fmt in (("rs_pct", "rs=%.0f"), ("rvol", "rvol=%.2f"),
                         ("atr_pct", "atr=%.2f"), ("atr_expansion", "atrx=%.2f"),
                         ("breakout_quality", "bq=%.2f"),
                         ("market_pct", "mkt=%.0f"), ("sector_pct", "sec=%.0f"),
                         ("intraday_rs", "irs=%+.2f")):
            v = ctx.get(key)
            if v is not None:
                bits.append(fmt % v)
        if ctx.get("oi_bias"):
            bits.append(f"oi={ctx['oi_bias']}")
        bits.append(f"agree={result.get('n_agree', 0)}")
        return " ".join(bits)
    except Exception:
        return ""
