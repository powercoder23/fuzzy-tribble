# Fix Plan — discount.py noise reduction + logging

> Read `CLAUDE.md` first. This plan reduces alert noise and adds structured logging. No refactoring, no scoring changes, no new features.

## Execution rules

- Apply **one step at a time**. After each step:
  1. Run `python -m py_compile discount.py` to verify syntax.
  2. Show the diff.
  3. Stop and wait for "go" before continuing.
- If a step needs a decision (line number ambiguous, naming conflict), ask instead of guessing.
- Do **NOT** unify the two scoring functions, change scoring formulas, refactor `scan_single_strike`, or add features beyond this plan.

---

## Step 1 — Tighten the constants block (~lines 319–346)

Update these values:

| Constant | Old | New |
|---|---|---|
| `MIN_DISCOUNT_SCORE` | 38 | 62 |
| `MAX_SPREAD_RATIO` | 0.15 | 0.06 |
| `IV_PCT_ACTIVE_SCAN_MAX` | 35 | 30 |
| `IV_PCT_STRADDLE_MAX` | 20 | 15 |
| `IV_PCT_NO_TRIGGER_MAX` | 20 | 15 |
| `WATCHLIST_MAX_SYMBOLS` | 40 | 25 |
| `WATCHLIST_MIN_AVG_VOLUME` | 300 | 800 |
| `WATCHLIST_MIN_RANGE_PCT` | 1.5 | 2.0 |
| `VOLUME_SPIKE_MULTIPLIER` | 2.5 | 3.0 |
| `WARMUP_MORNING_TOP_N` | 40 | 20 |

Add these new constants in the same block:

```python
MAX_ALERTS_PER_SCAN      = 8
MAX_ALERTS_PER_SYMBOL    = 1
MIN_TRIGGER_STRENGTH     = 0.35
MIN_DTE                  = 4
MAX_DTE                  = 14
MIN_PREMIUM_NIFTY        = 25
MIN_PREMIUM_STOCK        = 8
MAX_INDIA_VIX_FOR_BUYING = 20
```

---

## Step 2 — Close the flood gate

In `scan_all_fno_stocks`, change:

```python
df = pd.DataFrame(self.select_top_trades(df, limit=500, max_per_direction=260))
```

to:

```python
df = pd.DataFrame(self.select_top_trades(
    df,
    limit=MAX_ALERTS_PER_SCAN * 3,
    max_per_direction=MAX_ALERTS_PER_SCAN,
))
```

In `__main__`, change `min_discount_score=40` to `min_discount_score=MIN_DISCOUNT_SCORE` so the threshold stays in sync with the constant.

---

## Step 3 — Wire in `trader_logger.py`

Read `trader_logger.py` for the method signatures. Five edits:

**3a.** Add import at top of `discount.py`:
```python
from trader_logger import TraderLogger
```

**3b.** In `scan_all_fno_stocks`, right after the `if security_ids is None` block, initialize the logger and emit `scan_start`:
```python
self.trader_log = TraderLogger(scan_type="eod_discount")
self.trader_log.log_scan_start(
    capital=self._capital(),
    universe_size=len(security_ids),
)
```

**3c.** In `scan_underlying`, after IV percentile / HV metrics are computed (before the strike loop), call `self.trader_log.log_symbol_context(...)` with: `symbol`, `spot`, `dte`, `expiry`, `atm_iv`, `iv_rank`, `iv_percentile`, `iv_regime`, `weighted_hv`, `iv_history_samples`, `pcr`, `sentiment_bias`.

**3d.** In `scan_all_fno_stocks` main loop, when iterating `discounted`:
- If score below threshold → `self.trader_log.log_candidate(opt, decision="gated")` then `continue`.
- If a quality gate fails (from Step 4) → already logged via `log_gate_reject`; also call `log_candidate(opt, decision="gated")`.
- If accepted → `self.trader_log.log_candidate(opt, decision="accepted")` then append.

**3e.** At the end of `scan_all_fno_stocks`, just before `return`:
```python
self.trader_log.log_scan_summary(total_alerts=len(all_opportunities))
```

---

## Step 4 — Add `passes_quality_gates` method

Add this method on `DiscountedPremiumScanner` (place it just above `scan_all_fno_stocks`):

```python
def passes_quality_gates(self, candidate):
    """Hard quality gates. Every rejection logs via trader_log."""
    sym = candidate.get("symbol")
    strike = candidate.get("strike")
    opt_type = candidate.get("type")

    dte = candidate.get("dte")
    if dte is None or dte < MIN_DTE or dte > MAX_DTE:
        self.trader_log.log_gate_reject(sym, "dte", dte, f"[{MIN_DTE},{MAX_DTE}]", strike, opt_type)
        return False

    entry = candidate.get("entry") or 0
    floor = MIN_PREMIUM_NIFTY if sym in ("NIFTY", "BANKNIFTY", "FINNIFTY") else MIN_PREMIUM_STOCK
    if entry < floor:
        self.trader_log.log_gate_reject(sym, "premium_floor", entry, floor, strike, opt_type)
        return False

    spread = candidate.get("spread")
    if spread is not None and spread > MAX_SPREAD_RATIO:
        self.trader_log.log_gate_reject(sym, "spread", spread, MAX_SPREAD_RATIO, strike, opt_type)
        return False

    abs_delta = abs(candidate.get("delta") or 0)
    if abs_delta < 0.20 or abs_delta > 0.55:
        self.trader_log.log_gate_reject(sym, "delta_band", abs_delta, "[0.20, 0.55]", strike, opt_type)
        return False

    moneyness = abs(candidate.get("moneyness") or 100)
    if moneyness > 3.0:
        self.trader_log.log_gate_reject(sym, "moneyness", moneyness, "<3.0%", strike, opt_type)
        return False

    conviction = str(candidate.get("conviction", "LOW")).upper()
    if conviction == "LOW":
        self.trader_log.log_gate_reject(sym, "conviction", conviction, "MED|HIGH", strike, opt_type)
        return False

    rr = candidate.get("risk_reward")
    if rr is None or rr < 1.8:
        self.trader_log.log_gate_reject(sym, "rr", rr, ">=1.8", strike, opt_type)
        return False

    if candidate.get("trade_type") == "directional":
        trig_strength = (candidate.get("triggers") or {}).get("strength_score", 0)
        if trig_strength < MIN_TRIGGER_STRENGTH:
            self.trader_log.log_gate_reject(sym, "trigger_strength", trig_strength, MIN_TRIGGER_STRENGTH, strike, opt_type)
            return False

    return True
```

In `scan_all_fno_stocks`, apply the gate before appending each candidate:

```python
for opt in discounted:
    opt["symbol"] = sec_name
    opt["security_id"] = sec_id
    opt["expiry"] = opt.get("expiry") or current_expiry
    if opt["score"] < min_discount_score:
        self.trader_log.log_candidate(opt, decision="gated")
        continue
    if not self.passes_quality_gates(opt):
        self.trader_log.log_candidate(opt, decision="gated")
        continue
    self.trader_log.log_candidate(opt, decision="accepted")
    all_opportunities.append(opt)
```

---

## Step 5 — India VIX scan-level abort

Add this method on `DiscountedPremiumScanner`:

```python
def fetch_india_vix(self):
    """Fetch current India VIX. Returns None on failure (fail-open)."""
    try:
        response = self.dhan.quote_data({"IDX_I": [21]})
        data = unwrap_dhan_payload(response.get("data") or {})
        vix = data.get("IDX_I", {}).get("21", {}).get("last_price")
        return float(vix) if vix is not None else None
    except Exception:
        logger.exception("Failed to fetch India VIX")
        return None
```

In `scan_all_fno_stocks`, right after initializing `trader_log` in Step 3b, add:

```python
vix = self.fetch_india_vix()
self.trader_log.log_scan_start(  # update the earlier call with vix
    vix=vix,
    capital=self._capital(),
    universe_size=len(security_ids),
)
if vix is not None and vix > MAX_INDIA_VIX_FOR_BUYING:
    self.trader_log.log_scan_abort(
        "vix_too_high", vix=vix, threshold=MAX_INDIA_VIX_FOR_BUYING
    )
    return pd.DataFrame()
```

(Replace the `log_scan_start` from Step 3b with this fuller version — only one `log_scan_start` per scan.)

---

## Step 6 — Final per-symbol cap in `__main__`

After `reduce_to_one_per_symbol_expiry(all_opportunities)`, add:

```python
if not all_opportunities.empty:
    all_opportunities = (
        all_opportunities
        .sort_values("score", ascending=False)
        .groupby("symbol", group_keys=False)
        .head(MAX_ALERTS_PER_SYMBOL)
        .head(MAX_ALERTS_PER_SCAN)
        .reset_index(drop=True)
    )
```

---

## Verification (final step)

After all six steps:

1. `python -m py_compile discount.py` — must pass.
2. Run a dry scan (no live orders). Confirm `logs/scan_<date>.jsonl` and `_summary.txt` are created.
3. Open the summary file. The gate rejection histogram tells us whether the filters are correctly calibrated. If one gate kills 90%+ of candidates, that gate is miscalibrated and we'll tune it.

Report: total alerts before vs after, plus the gate rejection histogram.
