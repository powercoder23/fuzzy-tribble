# P1 Strategy-Quality Review — Scanner Validation, Quantitative Validation, Backtesting

**Date:** 2026-07-02 · **Scope:** P1 items 4-6 · **Prerequisite:** ARCHITECTURE_REVIEW_P0.md (all CRITICAL/HIGH fixed same day)
**Method:** full source read of every scanner + config. No empirical validation was possible — the shared DB copy here is unreadable and, more fundamentally, **no backtest harness exists** (§6). Every "edge" assessment below is therefore analytical, not measured. That itself is the headline: *nothing in this system has a measured edge.*

**Mapping your list to the code.** "Sonar" and "Laplace" are one module (`sonar_laplace_scanner.py`). "Long Build-up / Short Build-up / Short Covering / Long Unwinding" are not scanners — they are the four output labels of `oi_validator.classify`, surfaced by the OI Build-up scanner. "Accumulation Detection" and "Distribution Detection" **do not exist as modules**; their nearest proxies are Delivery-Surge (surge + price up = accumulation) and Smart-Money (net institutional buy = accumulation). If you intended standalone accumulation/distribution scanners (e.g. Wyckoff/A-D line/OBV based), they are not in the tree.

Verdicts: **KEEP** (edge plausible, fix noted flaws) · **DEMOTE** (context only — never a vote or veto) · **REBUILD** (thesis fine, implementation invalidates it) · **KILL** (no defensible edge as built).

---

## 4. Scanner-by-scanner validation

### 4.1 Option Discount ("Volatility Expansion Play") — `discount.py` — **REBUILD scoring, KEEP pipeline**

**Purpose.** Find single-leg options trading "cheap" for an intraday volatility-expansion buy.

**Logic.** Per strike: IV z-score vs the same-side chain mean ("skew discount"), IV vs 252-day weighted HV, delta band (0.15-0.40 ideal), log-liquidity, expected-move-ratio relevance → weighted base score → then **~12 layered ad-hoc adjustments** (premarket IV trend ±8/10, volume/OI steps ±3-8, spread penalty to −12, EM-ratio penalties, OI-wall proximity +10, per-option buildup +8, market-signal agreement +10-20 / disagreement −10, volume spike +5, market strength +6) → hardcoded `score < 40` reject → fixed premium plan: SL −15%, T1 +25% (book 70%), T2 +45%.

**Statistical problems, in order of severity:**

1. **"Skew discount" mostly measures the smile, not mispricing.** The z-score compares every strike's IV to the flat chain mean. The volatility smile guarantees strikes on the low side of the smile sit persistently below the mean — they will screen as "discounted" forever without being mispriced. Proper cheapness needs a moneyness-conditional reference (fit IV by delta bucket, then z-score the residual).
2. **IV-vs-HV term mismatch.** Near-expiry implied vs 252-day realized: IV < weighted-HV is usually the market correctly pricing a low-vol regime, not free premium — and the variance risk premium means IV > HV is the normal state, so the `hv_score` centering at 50 systematically punishes every candidate in normal regimes.
3. **The premium plan contradicts the thesis.** Fixed −15%/+25%/+45% on the premium ignores each option's delta/gamma/theta. On a 0.2-delta contract, −15% premium is a ~0.3-0.5% adverse spot wiggle — noise-level stopouts; on a 0.6-delta contract the same plan is far too loose. Levels should derive from the expected move / underlying structure translated through delta, not from the premium itself.
4. **Adjustment stack is unfalsifiable.** Base weights (0.30/0.40/0.10/0.10/0.20) plus ~12 bonuses/penalties = ~25 free parameters, none fit to data. No individual factor's marginal contribution is measurable because everything is summed before persistence (only `score_breakdown` partially helps).
5. **Threshold drift after the P0 fix.** The internal `score < 40` reject (`scan_single_strike` tail) and main.py's `min_discount_score=45` were calibrated to the old floor-40 compressed scale. On the new raw 0-100 scale they cut harder. **Re-derive both from the first week of new-scale score distributions.**

**False positives:** smile-structural "cheap" wings; quiet names whose IV is low because nothing is happening (cheap ≠ about to move); wide-spread names — the hard spread gate is 60% (!), and 20-60% spreads cost only up to −12 points. **False negatives:** genuinely mispriced ATM strikes (z ≈ 0 vs chain mean by construction). **Best conditions:** event/trend days with breadth expansion where realized vol overshoots implied. **Worst:** range-bound sessions (theta bleeds every position), expiry day, post-event IV crush.

### 4.2 Sonar / Laplace — `sonar_laplace_scanner.py` — **DEMOTE (soft bias only), one scanner not two**

**Purpose.** Ehlers SuperSmoother midline + ±1.6σ residual bands on 5-min closes → trend, dynamic S/R, breakout/reversal signals. Feeds: paper-trader entry veto, position risk warnings, composite.

**Logic flaws:**

1. **Internally contradictory signal set.** The same band emits momentum signals (`BREAKOUT_UP` = close beyond band → CE) and mean-reversion signals (`REVERSAL_UP` = re-entry from below → CE). A falling name triggers BREAKDOWN (PE bias), then minutes later REVERSAL_UP (CE bias) on the bounce — consumers get whipsawed opposite biases on the same underlying within one session. Pick one regime interpretation per state, or gate breakout-vs-reversion on the slope.
2. **Statistically thin inputs.** `MIN_POINTS=10` → analysis on 50 minutes of data; band σ from ≤10 residuals; trend from a 3-point slope of a smoothed line with `MIN_SLOPE_PCT=0.05%` — all noise-level at the 09:50 scan.
3. **Repaint risk.** `series[-1]` is the latest fetched candle with no completed-candle check — an in-progress 5-min bar can print beyond the band and pull back before close. Momentum's own fetchers deliberately use *completed* candles; sonar doesn't.
4. **Full-day residual σ mixes regimes** — the volatile open inflates bands all afternoon.
5. Empirically, band-breakout longs on 5-minute equity bars are one of the most commonly negative-expectancy patterns in intraday literature; the *reversal* (band re-entry) reads have better priors. The scanner ranks breakouts *above* reversals.

**Verdict:** never a hard veto (the P0 fix already made it veto-not-flip; consider `soft` until measured). Require completed candles, raise MIN_POINTS to ≥ 24 (2 hours), and log the veto rate — a filter that vetoes 40% of entries on noise is a silent P&L tax you currently cannot see.

### 4.3 Break & Bounce — `break_bounce_strategy.py` — **KEEP (best design in the repo), fix the exits**

**Purpose.** Yesterday's high/low breakout on a completed 15-min candle, then a 5-min retest entry (hammer / engulfing with prior-candle context).

**Strengths:** level-based (non-repainting reference), multi-timeframe confirmation, pattern definitions with context requirements (≥2 prior red candles falling into the level), one-trade-per-symbol-day, setup voiding after 11:45. This is the only strategy whose entry logic a professional desk would recognize.

**Flaws:**

1. **Exits are premium-percent, not structure.** SL = −30% premium, target = 2.5× SL distance. The natural stop for a retest entry is *the level failing* (5-min close back below yesterday's high), translated to premium via delta. A −30% premium stop on an ATM option ≈ 1-1.5% adverse spot move — often far beyond level-invalidation, so you lose more than the setup ever risked.
2. **No volume/participation gate on the 15-min breakout** — a 0.05% marginal close above yesterday's high in a dead tape qualifies the same as a conviction break.
3. **Gap-open pathology.** A gap-and-go open beyond yesterday's high makes the first 15-min candle a "breakout"; the "retest" of yesterday's high is then a deep fade — precisely the entries that fail on true gap-and-go days (no retest ever comes) and fill on gap-fade days (retest = the reversal). Consider excluding breakouts where open > level (that's the gap scanner's regime, not a break-and-bounce).
4. **Retest tolerance 0.3% is one-size** — too tight for high-beta names, too loose for NIFTY heavyweights. Scale by ATR.
5. Candlestick-pattern hit rates on 5-min bars are barely above chance in most studies; the prior-candle context helps, but this is exactly what the backtest harness must measure first.

**FN by construction:** the strongest trend days never retest — the strategy structurally misses the best moves; that's an accepted cost of pullback entries but should be measured. **Best:** post-consolidation range expansion days. **Worst:** expiry pinning, gap days, low-ADX chop producing marginal breaks.

### 4.4 OI Build-up (incl. Long/Short Build-up, Short Covering, Long Unwinding) — `oi_buildup_scanner.py` — **REBUILD**

**Purpose.** Classify each name into the four OI quadrants from aggregate option OI (first vs latest intraday snapshot); feeds composite, auto-exit, and risk warnings.

**The core flaw: aggregate call+put option OI is the wrong instrument.** Total option OI rises mechanically through nearly every session as the day's positions open; it barely ever *falls* intraday. So `oi_chg > 0` is the default state, and the quadrant classifier degenerates into `sign(price move)`: LONG_BUILDUP ≈ "price up ≥ 0.3%", SHORT_BUILDUP ≈ "price down". SHORT_COVERING / LONG_UNWINDING (OI down) will be rare intraday and mostly snapshot artifacts. **This scanner is a momentum-sign detector wearing an OI costume** — and it feeds the auto-exit, meaning a position can be auto-closed on what is effectively "spot moved 0.3% against you plus the mechanical OI drift."

Aggravating details: the baseline snapshot is the 09:15-09:50 warmup, when exchange OI dissemination is stale and small (denominators tiny → % changes inflated); baselines are taken at different times per symbol (P0 §2.2); the classifier's strict `> 0` boundaries put noise-level moves into confident quadrants; call/put split and PCR are computed but unused in classification.

**Rebuild direction:** per-contract **futures** OI (the `oi_validator` used by B&B already speaks this language and the Upstox candles carry OI), or delta-weighted net option OI change; compare vs same-time-yesterday, not vs 9:15; require magnitude z-scores rather than sign. Until rebuilt, keep `AUTO_EXIT` mode = `soft` and treat the quadrant labels as descriptive annotation. **Long/Short Build-up etc. as separate scanners: do not build them** — they'd be four filters of the same (flawed) computation.

### 4.5 Delivery-% Surge (≈ Accumulation/Distribution proxy) — `delivery_surge_scanner.py` — **KEEP, fix the baseline**

**Purpose.** Today's delivery % ≥ 1.5× trailing average, ≥ 45% absolute, with ≥ 1% price move → CE (up) / PE (down) BTST bias. The thesis (delivered stock = positioned money → multi-day follow-through) has real literature behind it for Indian cash equities.

**Flaws:** `MIN_HISTORY_DAYS = 2` — a "surge vs trailing average" against a **2-day baseline is statistically void**; raise to ≥ 15 before a symbol is eligible. Delivery % spikes on non-informative events the scanner doesn't exclude: block-deal crossings (the data to exclude them is *already in the same DB* — join against the `deals` table), dividend/record dates, index-rebalance days (MSCI/NIFTY reconstitutions produce the biggest delivery spikes of the year with zero directional information). Direction from a single day's close-to-close also tags reversal-day accumulation (big red day, huge delivery = institutions *buying the dip*) as PE — the most interesting case is mislabeled. **Best:** normal news-driven repositioning. **Worst:** rebalance weeks, result days with block crossings.

### 4.6 Smart-Money (Bulk/Block deals) — `smart_money_scanner.py` — **DEMOTE**

**Purpose.** Net bulk(+block) deal value over 3 days, |net| ≥ ₹5cr → CE/PE bias.

**Why the edge is doubtful:** bulk-deal disclosure (>0.5% of equity) captures prop desks, HNI momentum chasers and operators, not merely "smart" money — Indian-market studies of bulk-deal follow-through are weak to negative at short horizons; block deals are pre-negotiated crosses whose buyer conveys ~no information (and `INCLUDE_BLOCK` mixes them in); ₹5cr is small vs F&O-name turnover; disclosure lands *after* the move (you buy the CE after the price already absorbed the flow). The netting handles circular trades, which is good design. **Verdict:** context annotation. Its 0.25 composite weight — second-highest — is the suite's weakest-evidence factor at nearly its highest weight. Cut to ≤ 0.10 until measured.

### 4.7 Gap / Extreme-Opening — `gap_scanner.py` — **KEEP, fix the "open"**

**Purpose.** |gap| ≥ 1.5% AND open beyond yesterday's range → gap-and-go CE/PE. Opening-drive continuation is a real, documented effect — best prior of the alert scanners.

**Flaws:** (1) "Open" = first IV snapshot **at/after 09:30** — up to 15+ minutes after the actual 09:15 open; by then the gap may have half-filled, and what's labeled "open beyond prior high" is really "09:30 print beyond prior high" — a different (weaker) signal. Use the actual session open (a 1-minute candle fetch for gap candidates would cost ~30 API calls). (2) The intraday-proxy prior-day OHLC truncates true high/low (15-min snapshot grid misses extremes) → range-break passes too easily → FP; the delivery_daily path fixes this, so make TRUE-OHLC mandatory once bhav history covers the universe. (3) No regime conditioning: gap-and-go dominates in trending regimes, gap-fade in ranges — a VIX/breadth conditioner would likely double the signal's precision. The STALE-proxy guard is good defensive engineering.

### 4.8 IV Rank — `iv_rank_scanner.py` — **KEEP (cleanest scanner in the suite)**

Percentile-first with honest adaptive labeling, pure functions, correct fail-open. Two cautions: all of its historical output up to today was computed over the P0-polluted daily history — **discard/ignore `iv_rank_history` rows written before the DB rebuild**; and low IV rank predicts low *realized* vol as much as cheap premium — cheap options are usually cheap for a reason. The alert's own caveat ("needs a catalyst + direction") is the correct epistemics; the composite honors this by using it as modifier, not vote. Correct design.

### 4.9 Composite + Trade Suggester + Morning Confluence — **REBUILD the weights, KEEP the architecture**

The vote/modifier separation (directional factors vote; IV/VIX scale) is the right shape. Two serious problems:

1. **The four "independent" votes are ~one vote counted three times.** oi_buildup ≈ sign(today's price move) (§4.4); gap ≈ sign(price at open); delivery-surge *requires* |price| ≥ 1% and takes direction from the same day's move. Three of four directional factors are deterministic functions of today's price direction. The confluence bonus (+10%/+20% for 3/4 agreeing) rewards *the same information* for showing up in three tables. True confluence needs orthogonal factors: price (one vote, once), positioning (futures OI), flow (deals), cost (IV) — as separate axes.
2. **Timeframe incoherence.** At the authoritative 20:15 run, "gap" is 11 hours stale (this morning's open voting on tomorrow), OI buildup is an intraday read, delivery is tonight's, smart money is 3 days. Each factor needs an explicit validity horizon; a stale factor should decay, not vote at full strength.

Same critique applies to `trade_suggester` (soft, so lower stakes) and `morning_confluence`.

### 4.10 Accumulation / Distribution Detection — **DOES NOT EXIST**

No Wyckoff/OBV/A-D/volume-profile accumulation module exists. Current proxies: delivery-surge (§4.5) and smart-money (§4.6). Recommendation: do **not** build a new one until the backtest harness (§6) exists — you have five directional scanners already, three of which are correlated; a sixth adds noise, not information.

---

## 5. Quantitative validation

**Look-ahead bias.** Live scanning is mostly structurally safe (only current snapshots exist at scan time). Exceptions: sonar's in-progress candle (repaints — §4.2); gap's 09:30 "open" label contains 15 min of post-open information (§4.7); oi_buildup baselines taken at different times per symbol destroy cross-sectional comparability (P0 §2.2; `fetched_at` now makes this measurable). For *future backtests*: `iv_history.timestamp` is pass-floored fiction — always join on `fetched_at`.

**Survivorship bias.** The universe is loaded from NSE's *current* stock-futures list every day. Any historical analysis over `iv_history` therefore contains only today's survivors — names removed from F&O (typically after distress) vanish from history, flattering every scanner. Additionally, **F&O ban-list names are not filtered**: scanners will signal names in the ban period where fresh positions are prohibited — operational false positives. Persist the daily universe (one small table: date, security_id) and the ban list; this costs nothing now and is unrecoverable later.

**Data leakage.** The composite's 20:15 "next-session" score mixes same-day factors (gap, OI) with EOD factors — a naive backtest that scores it against *same-day* returns would leak badly; score only against next-session returns. Delivery data lands T+1-ish in the evening — fine as designed; the gap scanner's stale-proxy guard prevents multi-day moves masquerading as gaps — good.

**Overfitting.** The system has ~80 tunable constants (weights, bonuses, thresholds, dead-bands) and **zero fitting procedure, zero experiment log, zero holdout**. This is not "no overfitting" — it is *unaudited* fitting: values were adjusted while watching live behavior over weeks, which is overfitting to a small sample with no record of the search. Every threshold flagged in §4 (score<40, 45, BUY_ZONE 30, GAP 1.5%, SURGE 1.5×, ±0.3% dead-bands, composite weights) must be treated as arbitrary until the harness produces sensitivity curves.

**Indicator redundancy.** IV-cheapness is computed in three places with three definitions (discount per-strike rank, iv-rank ATM percentile, composite modifier); OI is interpreted three ways (discount per-strike buildup, oi_validator futures quadrants, oi_buildup aggregate quadrants); price-move-sign appears inside ≥4 factors. One concept → one module → one definition, consumed everywhere.

**Signal overlap / scanner correlation.** Predicted rank correlations (measure them once the harness exists — one SQL join over the `*_history` tables): oi_buildup vs gap vs delivery-surge directional agreement should run 0.6-0.8 on trending days because all derive from today's price. Sonar in-band "soft bias" also follows price sign → adds to the same cluster. Genuinely orthogonal axes available to you: IV level, futures-OI change, deals flow, delivery %, market breadth. The composite should be rebuilt on those five, not on four price-echoes.

**Expected edge.** Unmeasurable today, and the paper book is currently contaminated: every pre-fix trade that went through the Sonar side-flip (P0 §3.2) or level-price SL fills is invalid, and IV-rank-gated entries used the polluted daily history. **Reset the paper experiment**: archive `paper_trades.db`, start a clean sample post-fixes, and persist per-trade the full factor vector at entry (scanner votes, gate results, spread, breadth) — without that, even 500 paper trades won't tell you *which* component carries the edge. Minimum sample before believing anything: ~100 trades per strategy tag.

---

## 6. Backtesting framework

**Verdict: there is no backtesting framework.** The roadmap lists one; the tree has a single-path *forward* test (paper trading) with an optimistic fill model. Assessment against your own checklist:

| Capability | Status |
|---|---|
| Trade simulation | Paper only, 5-min LTP sampling; gap-aware SL as of today's fix; intrabar touches still missed; T1 books 70% at exact level (assumes infinite liquidity at the touch) |
| Entry logic | At scanner-time mid; **no spread crossing** — live you pay the ask; on 20%-spread stock options that's ~10% premium disadvantage at entry, unmodeled |
| Exit logic | State machine is sound (post-P0); no partial-fill or gap modeling on targets |
| Position sizing | Lots from ScripMasterLotSizer in momentum/B&B; discount paper book sizes 1× lot implicitly via lot_size — no vol-scaled sizing anywhere |
| Slippage | Absent |
| Brokerage | Absent |
| Taxes/charges | Absent |
| Walk-forward | Absent |
| Out-of-sample | Absent |

**Costs are not a rounding error here.** For NSE options (per round trip, discount broker): brokerage ~₹40 flat, STT 0.1% of *sell-side* premium, exchange charges ~0.035% of premium both sides, SEBI + stamp + 18% GST on charges. On a typical ₹8-10k premium position this is ₹70-110 ≈ **0.8-1.2% of premium per round trip**, plus spread crossing which on stock options routinely costs 2-8% of premium. Against a +25% T1 target with 70% booked, realistic frictions consume roughly a quarter of the edge the paper book reports. **Every paper P&L figure produced so far is overstated by construction.**

**What to build (priority order, smallest useful first):**

1. **Costs + spread in the paper path (1 day).** A `costs.py` with the NSE fee schedule; entry fill = mid + half-spread (bid/ask are already in the candidates), exit fill = level − half-spread; add a `costs` and `slippage` column to `paper_trades`. This single change makes the forward test honest.
2. **Replay harness (the real unlock, ~1 week).** All scanners are already pure functions of `iv_history.db` + `*_history` tables — replay them over stored history day by day: for each historical scan time, truncate the tables to `fetched_at ≤ t`, run the scanner, score signals against forward returns from stored spot snapshots. No broker needed. Deliverables: per-scanner precision (next-1h/next-day direction hit rate), per-gate marginal contribution (re-run with each gate disabled), factor-correlation matrix (§5).
3. **Option-premium reconstruction.** You cannot backtest premium-level exits without premium history. Two routes you already own: (a) `fetch_expired_option_data` (discount.py) pulls expired-contract candles from Upstox — reconstruct entry-to-exit premium paths for historical candidates; (b) going forward, have the collector persist the ATM±3 strike quotes it already fetches (~10 extra columns, zero extra API calls). Do (b) tonight; it is free and unrecoverable later.
4. **Walk-forward + OOS protocol.** Only after 1-3: rolling 8-week train / 2-week test for every threshold flagged in §4-5; final month is a lockbox nobody tunes against. Log every parameter set tried (a 10-line JSON appender) — the experiment log is what separates calibration from curve-fitting.
5. **Sizing.** Replace flat lots with risk-parity on premium: lots = risk_budget / (premium × SL%×premium), which momentum already implements — port it to the discount paper path, then vol-scale the risk budget by VIX regime.

**Sequencing rule from P0 stands:** no new scanners, no weight tweaks, no threshold "improvements" until item 2 exists. Every hour spent tuning before the harness is an hour spent memorizing noise.

---

## Priority actions

1. Reset the paper experiment (archive pre-fix trades; persist full factor vector per trade). (§5)
2. Costs + spread into the paper fill path. (§6.1)
3. Recalibrate `score<40` / `min_discount_score=45` on the new raw score scale. (§4.1)
4. Sonar: completed candles only, MIN_POINTS≥24, log veto rate, keep as soft bias. (§4.2)
5. OI buildup: switch to futures OI or same-time-yesterday baseline; keep auto-exit soft until then. (§4.4)
6. Delivery: MIN_HISTORY_DAYS→15, exclude block-deal/rebalance days via the deals table. (§4.5)
7. Composite: cut smart-money weight, collapse the three price-echo factors into one price vote. (§4.9)
8. Persist daily universe + F&O ban list starting tonight. (§5)
9. Collector: persist ATM±3 strike quotes for future premium backtests. (§6.3)
10. Build the replay harness; then and only then, walk-forward every threshold. (§6.2, §6.4)
