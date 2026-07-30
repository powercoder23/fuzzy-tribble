"""hedge.py — Pure function to find the OTM hedge (short) leg for a buy signal.

Given already-fetched option chain data and a primary buy strike, returns the
signal dict for the hedge leg (direction="short") that forms the short side of
a debit spread. No broker API calls — uses the chain data the caller already
holds.

The hedge is:
  CE primary: sell the CE N strikes above the primary (bull-call spread)
  PE primary: sell the PE N strikes below the primary (bear-put spread)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def find_hedge_leg(
    chain: dict,
    spot: float,
    primary_strike: float,
    primary_side: str,
    n_strikes: int,
    signal_template: dict,
) -> tuple[dict | None, str]:
    """Find the OTM hedge leg from already-fetched option chain data.

    Parameters
    ----------
    chain         : option chain dict {strike_str: {"ce": {...}, "pe": {...}}}
    spot          : current underlying spot price
    primary_strike: the buy (long) leg strike price
    primary_side  : "CE" / "CALL" or "PE" / "PUT"
    n_strikes     : how many strikes more OTM for the short leg
    signal_template: base fields to copy (symbol, security_id, expiry, etc.)

    Returns (hedge_signal_dict, "ok") on success, or (None, reason) when the
    hedge is unavailable, too thin, or invalid — 2026-07-30: this used to
    return bare None with only a logger.debug() call, which is invisible in
    practice (this system's containers don't reliably persist stdout to a
    mounted log file — see [[paper-trading]] memory). Callers now persist
    `reason` into the primary trade's factors_json so a silent, systemic
    hedge-booking failure (as happened for ~half of discount's first batch of
    hedged trades) is diagnosable from the DB instead of requiring guesswork.
    """
    import hedge_config as cfg

    if not chain or not spot or not primary_strike:
        return None, "missing chain/spot/primary_strike input"

    side_norm = "CE" if str(primary_side).upper() in ("CE", "CALL") else "PE"
    opt_key = "ce" if side_norm == "CE" else "pe"

    # Parse strike prices from the chain keys
    try:
        strikes = sorted(float(k) for k in chain.keys())
    except (TypeError, ValueError):
        logger.debug("hedge: could not parse strike keys")
        return None, "could not parse chain strike keys"

    if len(strikes) < 2:
        return None, "chain has fewer than 2 strikes"

    # Find the primary strike in the chain (nearest match)
    closest_primary = min(strikes, key=lambda s: abs(s - primary_strike))
    strike_interval = strikes[1] - strikes[0]
    if abs(closest_primary - primary_strike) > strike_interval * 0.6:
        logger.debug("hedge: primary strike %.0f not found in chain (nearest %.0f)",
                     primary_strike, closest_primary)
        return None, f"primary strike {primary_strike:.0f} not found in chain (nearest {closest_primary:.0f})"

    primary_idx = strikes.index(closest_primary)

    # Hedge is further OTM
    if side_norm == "CE":
        hedge_idx = min(primary_idx + n_strikes, len(strikes) - 1)
    else:
        hedge_idx = max(primary_idx - n_strikes, 0)

    hedge_strike = strikes[hedge_idx]
    if abs(hedge_strike - closest_primary) < strike_interval * 0.5:
        logger.debug("hedge: hedge strike same as primary — skipping")
        return None, "hedge strike collapsed onto primary (chain too short past primary)"

    # Look up the hedge strike data in the chain
    strike_data = None
    for k in chain:
        try:
            if abs(float(k) - hedge_strike) < 0.5:
                strike_data = chain[k]
                break
        except (TypeError, ValueError):
            continue

    if not strike_data:
        logger.debug("hedge: strike %.0f data not found in chain", hedge_strike)
        return None, f"strike {hedge_strike:.0f} data not found in chain"

    opt = strike_data.get(opt_key, {})
    if not opt:
        return None, f"no {opt_key.upper()} data at strike {hedge_strike:.0f}"

    bid = float(opt.get("top_bid_price") or opt.get("last_price") or 0)
    ask = float(opt.get("top_ask_price") or opt.get("last_price") or 0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else float(opt.get("last_price") or 0)

    if mid < cfg.HEDGE_MIN_CREDIT:
        logger.debug("hedge: credit ₹%.2f < min ₹%.2f — skipping for %s K%.0f",
                     mid, cfg.HEDGE_MIN_CREDIT, signal_template.get("symbol"), hedge_strike)
        return None, f"credit ₹{mid:.2f} < min ₹{cfg.HEDGE_MIN_CREDIT:.2f}"

    # Don't hedge when the short credit is too close to the primary premium
    # (the spread would be so narrow it captures almost no move)
    primary_entry = float(signal_template.get("entry") or 0)
    if primary_entry > 0 and mid / primary_entry > cfg.HEDGE_MAX_CREDIT_RATIO:
        ratio_pct = mid / primary_entry * 100
        logger.debug("hedge: credit ₹%.2f is %.0f%% of primary ₹%.2f (max %.0f%%) — spread too narrow",
                     mid, ratio_pct, primary_entry, cfg.HEDGE_MAX_CREDIT_RATIO * 100)
        return None, f"credit ₹{mid:.2f} is {ratio_pct:.0f}% of primary (max {cfg.HEDGE_MAX_CREDIT_RATIO*100:.0f}%) — spread too narrow"

    hedge_sl = round(mid * cfg.HEDGE_SL_MULT, 2)
    hedge_t1 = max(round(mid * cfg.HEDGE_T1_MULT, 2), 0.05)

    # Build the hedge signal from the primary's template
    exclude_primary = {
        "direction", "strike", "entry", "sl", "t1", "t2",
        "bid", "ask", "oi", "volume", "skip_risk_cap",
        "combo_id", "min_premium", "score", "moneyness",
    }
    hedge = {k: v for k, v in signal_template.items() if k not in exclude_primary}
    hedge.update({
        "side":               side_norm,
        "strike":             hedge_strike,
        "entry":              round(mid, 2),
        "sl":                 hedge_sl,
        "t1":                 hedge_t1,
        "t2":                 hedge_t1,
        "t1_book_fraction":   1.0,
        "direction":          "short",
        "skip_risk_cap":      True,
        "skip_pre_market_gate": True,
        "min_premium":        0.0,
        "bid":                round(bid, 2),
        "ask":                round(ask, 2),
        "oi":                 int(opt.get("oi") or 0),
        "volume":             int(opt.get("volume") or 0),
        "strategy":           str(signal_template.get("strategy", "")) + " [hedge]",
    })
    logger.info("hedge: %s %s K%.0f short @ ₹%.2f (primary K%.0f @ ₹%.2f, %d strikes OTM)",
                signal_template.get("symbol"), side_norm, hedge_strike, mid,
                closest_primary, primary_entry, n_strikes)
    return hedge, "ok"


# --------------------------------------------------------------------------- #
# Margin estimation — see hedge_config.py NAKED_SPAN_PCT_OF_NOTIONAL for the
# "why this formula, and why it's an estimate" explanation.
# --------------------------------------------------------------------------- #
def estimate_naked_short_margin(spot: float, lot_size: int, premium: float) -> float:
    """Rough SPAN+exposure margin a NAKED short option would require (~15% of
    notional + the premium itself). ESTIMATE ONLY — ballpark for comparison,
    not a substitute for the broker's live margin calculator."""
    import hedge_config as cfg
    if not spot or not lot_size:
        return 0.0
    return round(spot * lot_size * cfg.NAKED_SPAN_PCT_OF_NOTIONAL + (premium or 0.0) * lot_size, 2)


def estimate_spread_margin(long_entry: float, short_entry: float, lot_size: int) -> float:
    """Margin for a HEDGED debit spread (both legs held together) ≈ its max
    loss — the net debit paid. This is what a broker's spread margining
    recognizes once a long+short combo of the same underlying/expiry is held
    simultaneously (rather than the short leg's full naked SPAN). ESTIMATE."""
    return round(max((long_entry or 0.0) - (short_entry or 0.0), 0.0) * (lot_size or 1), 2)
