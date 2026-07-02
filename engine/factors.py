# -*- coding: utf-8 -*-
"""Factor adapters — normalize V1 scanner outputs into FactorReading objects.

P0 strangler layer: reads the *_history tables through each V1 scanner's
get_latest_* helper (defensive import, fail-open — identical philosophy to
composite_scanner, which this module supersedes). In P2 the computations move
in-engine and this file shrinks to the normalizers.

Factor names are stable API for the conviction scorer and the journal:
    oi_flow · inst_flow · delivery · gap · trend · sector_rs · premium_value
"""

from __future__ import annotations

import logging

from engine.contracts import FactorReading, CE, PE

logger = logging.getLogger(__name__)


def _opt_import(name):
    try:
        return __import__(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("factors: '%s' unavailable (%s)", name, exc)
        return None


_oi = _opt_import("oi_buildup_scanner")
_gap = _opt_import("gap_scanner")
_smart = _opt_import("smart_money_scanner")
_deliv = _opt_import("delivery_surge_scanner")
_ivr = _opt_import("iv_rank_scanner")
_sonar = _opt_import("sonar_laplace_scanner")


def _safe(module, fn, key):
    if module is None:
        return {}
    try:
        return getattr(module, fn)(key) or {}
    except Exception:  # noqa: BLE001
        return {}


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


# --------------------------------------------------------------------------- #
# Normalizers (pure; unit-tested with plain dicts)
# --------------------------------------------------------------------------- #
def norm_oi_flow(d: dict) -> FactorReading:
    """OI 2x2 quadrant: fresh build = 1.0, covering/unwinding = 0.5 (doctrine)."""
    f = FactorReading("oi_flow")
    if d and d.get("bias") in (CE, PE):
        fresh = str(d.get("strength", "")).upper() == "STRONG"
        f.bias = d["bias"]
        f.strength = 1.0 if fresh else 0.5
        f.detail = {"fresh": fresh, "pattern": d.get("pattern")}
    return f


def norm_inst_flow(d: dict) -> FactorReading:
    """Block/bulk deals net value -> strength (₹20cr saturates)."""
    f = FactorReading("inst_flow")
    if d and d.get("bias") in (CE, PE):
        net = abs(float(d.get("net_value_cr") or 0.0))
        f.bias = d["bias"]
        f.strength = _clip01(max(0.5, net / 20.0))
        f.detail = {"net_value_cr": net}
    return f


def norm_delivery(d: dict) -> FactorReading:
    f = FactorReading("delivery")
    if d and d.get("bias") in (CE, PE):
        surge = float(d.get("surge_x") or 1.0)
        f.bias = d["bias"]
        f.strength = _clip01(max(0.5, (surge - 1.0) / 2.0 + 0.5))
        f.detail = {"surge_x": surge}
    return f


def norm_gap(d: dict) -> FactorReading:
    f = FactorReading("gap")
    if d and d.get("bias") in (CE, PE):
        f.bias = d["bias"]
        f.strength = 1.0 if d.get("extreme") else 0.7
        f.detail = {"extreme": bool(d.get("extreme"))}
    return f


def norm_trend(d: dict) -> FactorReading:
    """Sonar SuperSmoother slope -> trend vote. 0.5% slope saturates."""
    f = FactorReading("trend")
    if d and d.get("trend") in ("UP", "DOWN"):
        slope = abs(float(d.get("slope_pct") or 0.0))
        f.bias = CE if d["trend"] == "UP" else PE
        f.strength = _clip01(0.4 + slope / 0.5 * 0.6)
        f.detail = {"slope_pct": d.get("slope_pct"),
                    "support": d.get("support"), "resistance": d.get("resistance")}
    return f


def norm_sector_rs(sector_pct: float | None, market_pct: float | None) -> FactorReading:
    """Sector breadth vs market breadth -> relative-strength vote.
    A sector 15pp above/below market saturates."""
    f = FactorReading("sector_rs")
    if sector_pct is None or market_pct is None:
        return f
    edge = sector_pct - market_pct
    if abs(edge) < 5.0:      # deadband — no vote
        return f
    f.bias = CE if edge > 0 else PE
    f.strength = _clip01(abs(edge) / 15.0)
    f.detail = {"sector_pct": sector_pct, "market_pct": market_pct}
    return f


def norm_premium_value(zone: str | None) -> FactorReading:
    """IV-rank zone. CHEAP votes WITH any direction (handled by scorer as a
    direction-neutral bonus); EXPENSIVE is a hard gate upstream, never scored."""
    f = FactorReading("premium_value")
    if zone in ("CHEAP", "FAIR", "EXPENSIVE"):
        f.detail = {"zone": zone}
        f.strength = 1.0 if zone == "CHEAP" else 0.0
    return f


# --------------------------------------------------------------------------- #
# Loader — one FactorSet per symbol (fail-open per factor)
# --------------------------------------------------------------------------- #
def load_factors(security_id: str, symbol: str,
                 breadth_snap=None) -> dict[str, FactorReading]:
    """Read all factors for one symbol from V1 tables. Missing factor = silent vote."""
    sid = str(security_id)
    sector_pct = market_pct = None
    if breadth_snap is not None:
        try:
            market_pct = breadth_snap.market_pct
            _, sec = breadth_snap.sector_for(symbol)
            sector_pct = sec.get("pct") if sec else None
        except Exception:  # noqa: BLE001
            pass

    return {
        "oi_flow": norm_oi_flow(_safe(_oi, "get_latest_buildup", sid)),
        "inst_flow": norm_inst_flow(_safe(_smart, "get_latest_smart_money", symbol)),
        "delivery": norm_delivery(_safe(_deliv, "get_latest_surge", symbol)),
        "gap": norm_gap(_safe(_gap, "get_latest_gap", sid)),
        "trend": norm_trend(_safe(_sonar, "get_latest_sonar", sid)),
        "sector_rs": norm_sector_rs(sector_pct, market_pct),
        "premium_value": norm_premium_value(
            (_safe(_ivr, "get_latest_zone", sid) or {}).get("zone")),
    }


# --------------------------------------------------------------------------- #
# P0 trigger adapter — sonar band breakouts are the only in-DB trigger source
# until ORB / VWAP / break-retest are ported in-engine (P2).
# --------------------------------------------------------------------------- #
def load_trigger(security_id: str):
    from engine.contracts import TriggerEvent
    d = _safe(_sonar, "get_latest_sonar", str(security_id))
    sig = (d or {}).get("signal")
    if sig in ("BREAKOUT_UP", "BREAKDOWN"):
        return TriggerEvent("SONAR_BAND", CE if sig == "BREAKOUT_UP" else PE,
                            quality=0.7, detail={"signal": sig, "last": d.get("last")})
    if sig in ("REVERSAL_UP", "REVERSAL_DOWN"):
        return TriggerEvent("SONAR_BAND", CE if sig == "REVERSAL_UP" else PE,
                            quality=0.5, detail={"signal": sig, "last": d.get("last")})
    return None
