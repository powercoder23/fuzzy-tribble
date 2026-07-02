# CONVEX — V2 Blueprint

**Platform for one job: help an option buyer consistently find the highest-probability trades.**

Named for the only structural edge an option buyer has: convexity — capped loss,
uncapped gain. Everything in the platform exists to buy that convexity *cheaply*,
*with a catalyst*, *in the right regime*. Anything that doesn't serve that is deleted.

Status: approved direction (2026-07-02). Migration model: strangler — V2 core runs
beside V1, absorbs it service by service. Horizon: **intraday only**.

---

## 1. Diagnosis (why V1 must be restructured, not extended)

V1 grew 13 docker services and ~18k lines around one missing abstraction. The
evidence is in the code itself:

- **Three fusion layers approximating each other.** `composite_scanner` fuses five
  factors into a conviction score. `trade_suggester` re-fuses the same five factors
  around discount candidates. `morning_confluence` fuses gap+OI+IVR a third way.
  Three scoring formulas, three weight sets, three Telegram voices — one job.
- **Four gates approximating one risk policy.** `entry_gate`, `cycle_gate`,
  `pre_market_gate`, and the breadth gate inside `order_manager` each veto trades
  from a different corner. No single place answers "may I trade this, now, at what size?"
- **Scanners alert instead of decide.** Ten Telegram streams push *biases* at the
  trader; nothing converts bias → orderable contract → sized order except the
  discount god-module (3,167 lines) and per-strategy duplicated plumbing.
- **Strategy/scanner distinction is false.** ORB, VWAP, B&B are *entry triggers*.
  OI buildup, delivery, deals, gap, IV rank are *context factors*. V1 treats both
  as peer "services", so triggers fire without context and context alerts without triggers.

V2's single organizing idea fixes all four at once.

## 2. Core trading philosophy

From the option-buyer doctrine the platform enforces (not merely suggests):

1. **Theta is the enemy** → every trade needs a *catalyst* (trigger), never direction alone.
2. **Regime first** → no setup is evaluated before the market posture is known.
3. **Expectancy over win-rate** → 38% win at 1:3 beats 65% at 1:1. Score for asymmetry.
4. **Cheap convexity or nothing** → IV rank gates every entry; expensive premium = no trade.
5. **Liquidity or nothing** → spread > 5% of premium kills the trade unconditionally.
6. **One decision, fully explained** → every emitted trade carries its complete "why";
   every *rejected* trade is journaled with its reject reason. The journal is how edge
   is measured and factors earn or lose their weights.

## 3. The organizing idea: Factors × Triggers × Gates → one Conviction Engine

```
FACTORS  (slow, context, per-symbol)     TRIGGERS (fast, price-action events)
  OI flow (all 4 quadrants)                ORB break        (15m close + volume)
  Institutional flow (deals + accum/dist)  VWAP reclaim/break
  Delivery surge                           Break & retest   (PDH/PDL, hammer/engulf)
  Gap (morning catalyst)                   Sonar band breakout (SuperSmoother)
  Trend (SuperSmoother slope)
  Sector relative strength
  Premium value (IV rank + discount)

           FACTORS give the WHERE and WHY.  TRIGGERS give the WHEN.
                 GATES give the WHETHER and HOW MUCH.

                       ┌─────────────────────────┐
   factors ──────────► │                         │
   triggers ─────────► │    CONVICTION ENGINE    │ ──► ranked Decisions
   regime ───────────► │  (one score, one gate   │      (or explained rejects)
   risk state ───────► │   stack, one formula)   │
                       └─────────────────────────┘
```

**The invariant that kills noise: no trigger, no trade.** Factors never open a
position; a factor change is silent context. Triggers never fire blind; a trigger
without factor confluence is a rejected candidate with a journal entry. Telegram
shrinks from ten streams to one: emitted Decisions (plus a morning and EOD digest).

## 4. Architecture — 13 services become 4

| V2 service | Absorbs (V1) | Responsibility |
|---|---|---|
| **convex-data** | iv-collector, bhav/deals/vix collectors, DataProvider pollers | *Sole* broker/API caller (Upstox). Writes market.db: IV snapshots, spot, candles cache, bhavcopy, deals, VIX. Sole-writer + WAL contract unchanged. |
| **convex-engine** | composite, sonar, iv-rank, oi-buildup, gap, delivery-surge, smart-money, trade_suggester, morning_confluence, entry/cycle/pre-market gates, breadth, momentum + break-bounce *logic* | Runs the decision funnel every 5 min. Computes factors, detects triggers, classifies regime, scores conviction, selects contract, sizes risk, emits Decisions. Zero API calls (reads market.db only). |
| **convex-executor** | order_manager, paper_trader, auto-exit, token_manager (Dhan, future) | Consumes Decisions. Paper by default (`AUTO_EXECUTE` gate preserved). Places entry + hard SL-M pair, trails, enforces time-stop and OI-contradiction auto-exit, closes all by 15:12. |
| **convex-cockpit** | dashboard_app + dashboard.html, all Telegram notifiers | One screen + one Telegram channel. Renders regime strip, ranked opportunities with reasons, positions, journal. Read-only over the DB. |

Data flow is strictly one-directional:

```
Upstox ──► convex-data ──► market.db ──► convex-engine ──► decisions/journal tables
                                             │                     │
                                             ▼                     ▼
                                      convex-executor ──►   convex-cockpit
                                      (positions table)     (reads everything)
```

## 5. The decision funnel (replaces the "workflow of screens")

The trader's old workflow — check ten alerts, cross-reference manually — becomes a
pipeline the engine walks every cycle, top-down, cheapest checks first:

```
1 REGIME     market posture: GREEN / AMBER / RED + directional lean + size multiplier
             inputs: VIX level+change, market breadth, index trend (SuperSmoother on NIFTY),
             event calendar (expiry day, RBI/budget blackout)
             RED → engine still *observes* and journals, but emits nothing.

2 UNIVERSE   F&O names, liquidity-prefiltered (spot volume, option OI floor)

3 CONTEXT    per-symbol FactorSet (the 7 factors) + sector RS vs market

4 TRIGGER    completed-candle events only (no wicks, volume-confirmed):
             ORB break | VWAP reclaim/break | PDH/PDL break-retest | Sonar band break
             No trigger → symbol stays on the WATCH list with its context score. Not a trade.

5 GATES      hard, unordered, any-fail = reject with reason:
             VIX > 22 · regime RED · IVR > 55 (EXPENSIVE) · spread > 5% · OI/vol floor
             · DTE < 3 unless same-day exit guaranteed · entry after 14:30
             · daily loss ≥ 3% · 2 SL hits today · max 3 concurrent (correlated = 1)
             · factor confluence contradicts trigger direction (net factor score < 0)

6 CONVICTION 0–100, weighted confluence (see §7) → grade A+ / A / B / reject

7 CONTRACT   strike ATM/1-OTM in direction (never >2 OTM, premium ≥ ₹20 for index),
             expiry = current weekly if DTE ≥ 3 else next, liquidity re-check on the
             actual contract, premium-value check (discount vs own IV history)

8 RISK       lots = floor(2% capital × regime-multiplier × grade-multiplier / (premium × 30% × lot))
             SL = 30% premium (hard SL-M at entry), T1 = 1.8× exit half, T2 = 3× trail rest,
             time-stop, EOD flat by 15:12

9 EXECUTE    paper (default) or live; entry + SL placed as a pair, emergency market
             exit if SL placement fails (V1 order-safety contract preserved)

10 JOURNAL   every Decision AND every reject persisted with full factor/gate breakdown.
             Weekly attribution: which factors correlated with winners → weights evolve
             on evidence, not opinion.
```

## 6. Scanner verdicts — every V1 feature, one decision each

| V1 feature | Verdict | Disposition in V2 |
|---|---|---|
| Discounted Options (`discount.py`, 3,167 ln) | **REBUILD** | Demoted from strategy to two engine components: *Premium-Value factor* (is this contract cheap vs its own IV history?) and the *Contract Selector* (step 7). The god-module dies; its IV/discount math survives as pure functions. |
| Sonar (SuperSmoother bands) | **MERGE** | Math kept verbatim (it's good). Slope → *Trend factor*; band breakout → a *Trigger*; the separate service, table, and Telegram stream die. |
| Laplace | **MERGE** | Same module as Sonar (already was — the name split was cosmetic). One name: Trend/Sonar. |
| Break & Bounce | **KEEP → MOVE** | Best entry logic in V1 (level + retest + candle anatomy). Becomes the *Break-Retest trigger* inside the engine. Its runner, config, container die. |
| Momentum ORB | **KEEP → MOVE** | *ORB trigger* (15m body close + 1.5× volume, 09:30–11:30 window, false-break re-entry exit). Service already discontinued; logic ported. |
| Momentum VWAP | **KEEP → MOVE** | *VWAP trigger*. Hardcoded 1.3× volume gate moves to config. |
| OI Build-up / Long Build-up / Short Covering / Long Unwinding | **MERGE** | One *OI-Flow factor* emitting the full 2×2 quadrant (price×OI) with strength. Fresh longs/shorts = strong vote; covering/unwinding = weak vote (doctrine-aligned). Four listed scanners were always one table. |
| Delivery Scanner | **KEEP** | *Delivery factor* (conviction of the move). EOD-computed, consumed next morning. |
| Bhavcopy Analysis | **MOVE** | Never was a scanner — it's ingestion. Lives in convex-data. |
| VIX | **MOVE** | Regime input, not a scanner. Lives in the Regime module. |
| Gap Scanner | **KEEP** | *Gap factor* — the morning catalyst vote. Also feeds the 09:25 first engine cycle (replacing morning_confluence). |
| Sector Breadth / Market Breadth | **MERGE** | Regime input (market) + *Sector-RS factor* (per-symbol). `breadth.py` computation kept. |
| Accumulation / Distribution | **MERGE** | Into *Institutional-Flow factor* with block/bulk deals (smart_money). One question: is real money entering or leaving this name? |
| Smart Money (deals) | **MERGE** | Same Institutional-Flow factor. |
| Composite Convection | **DELETE** | The engine *is* the composite, done once and properly. Its scoring skeleton (direction votes, confluence bonus, IV/VIX modifiers) is the seed of §7 — absorbed, then deleted. |
| trade_suggester | **DELETE** | Absorbed: engine ranks orderable contracts natively. |
| morning_confluence | **DELETE** | Absorbed: it's just the engine's 09:25 cycle. |
| entry_gate / cycle_gate / pre_market_gate / breadth gate | **MERGE** | One Gate stack (§5.5) inside the engine. Executor keeps only order-safety checks. |
| directional-iv strategy | **DELETE** | Already discontinued; nothing salvageable the IV-rank factor doesn't cover. |
| iv-rank scanner | **KEEP** | *Premium-cost gate* + factor modifier. Computation kept, service dies. |
| IV Collector | **KEEP** | Heart of convex-data. Sole-writer contract unchanged. |
| paper_trader / order_manager | **KEEP → MERGE** | Fused into convex-executor; the OI-contradiction auto-exit (config-gated off/soft/hard) survives as an executor policy. |
| dashboard_app | **REBUILD** | As the Cockpit (§8), reading engine tables instead of scraping per-scanner tables. |
| Intraday/EOD "experimental scanners" | **DELETE** | Anything not named above ships out. If an experiment earns statistical edge in the journal, it re-enters as a factor with a weight. That is the only door back in. |

Net: **10 Telegram streams → 1. 3 fusion layers → 1. 4 gates → 1 stack. 13 containers → 4.**

## 7. Conviction score (one formula, versioned in the journal)

Hard gates (§5.5) run first — the score only ranks candidates that are *allowed*.

```
score = Σ weight_i × alignment_i        alignment ∈ [-1, +1] vs trigger direction

  trigger quality      30   (volume ratio, candle body %, level cleanliness)
  OI flow              20   (fresh build ±1.0, covering/unwinding ±0.5)
  trend (Sonar slope)  15
  sector RS            10
  institutional flow   10
  premium value        10   (CHEAP +1, FAIR 0 — EXPENSIVE never reaches scoring: gated)
  gap/catalyst          5

modifiers:  ×(1 + 0.10) if ≥3 factors agree · ×(1 − 0.15) if VIX elevated (18–22)

grade:  A+ ≥ 75 → full size        A 60–74 → full size
        B 45–59 → half size, flagged "B-grade" in cockpit
        < 45    → reject, journaled ("trigger without context")
```

Weights are config, the formula is versioned, and every Decision stores the
version + full breakdown — so weekly attribution can actually re-tune weights.

## 8. Cockpit — one screen, ten seconds

Top-to-bottom, matching the funnel, no navigation required:

```
┌─ REGIME STRIP ──────────────────────────────────────────────────────────┐
│ ● GREEN (bullish lean) · VIX 13.4 ▼ · Breadth 64% adv · NIFTY ↑ trend  │
│   Sector heat: AUTO ▲▲ PSU-BANK ▲ IT ▼ · Expiry in 4d · Size ×1.0      │
├─ OPPORTUNITY STACK (ranked Decisions + watchlist) ──────────────────────┤
│ A+ 82  TATAMOTORS 1080CE @ ₹23.5 · SL 16.4 · T1 42 / T2 70 · 2 lots    │
│        WHY: ORB break 2.1× vol · fresh long OI · AUTO strongest sector  │
│        · deals +₹18cr · IV CHEAP (IVR 22)              [TAKE] [PASS]    │
│ A  67  ...                                                              │
│ WATCH  RELIANCE — context 71, no trigger yet (VWAP 2942, price 2938)    │
├─ POSITION RAIL ─────────────────────────────────────────────────────────┤
│ TATAMOTORS 1080CE  +34% · SL→BE · T1 half booked · trail 15m swing      │
│ Day: +1.2% capital · 1 SL hit · 2 slots free · lockout at −3%           │
├─ JOURNAL (today) ───────────────────────────────────────────────────────┤
│ 09:35 REJECT M&M CE — trigger ok, OI flow contradicts (−0.4)            │
│ 10:05 DECISION TATAMOTORS A+ 82 → paper filled                          │
└─────────────────────────────────────────────────────────────────────────┘
```

Rules: every number answers a decision ("do I take this, at what size, why").
No raw scanner tables, no per-scanner tabs, no chart gallery. Telegram mirrors
the stack: one message per Decision, morning digest 09:20, EOD P&L + attribution 15:20.

## 9. Data plane

`market.db` (today's iv_history.db, renamed at Phase 4) keeps the sole-writer + WAL
+ `iv_store.connect()` contract. Engine adds four tables it alone writes:

```
engine_decisions  (id, ts, cycle, symbol, security_id, direction, grade, score,
                   trigger, trigger_quality, factor_json, gate_json, formula_ver,
                   strike, expiry, entry, sl, t1, t2, lots, status)   -- status: EMITTED/REJECTED/WATCH
engine_regime     (ts, posture, lean, vix, breadth_pct, index_slope, size_mult, detail_json)
engine_positions  (executor-owned: fills, trail state, exit reason, realized R)
engine_journal    (attribution rows: decision_id → outcome, MFE/MAE, factor hit/miss)
```

During the strangler phases the factor adapters *read the existing `*_history`
tables* (oi_buildup, gap, delivery_surge, smart_money, iv_rank, sonar, composite),
so V1 collectors keep feeding V2 until each computation moves in-engine.

## 10. Migration — strangler phases

| Phase | Work | Kill list after phase |
|---|---|---|
| **P0** (this session) | `engine/` package: contracts, regime, factor adapters over existing tables, conviction scorer, pipeline, decisions store, runner + tests. Runs beside V1, emits WATCH/DECISION rows + optional Telegram. Observe-only. | — |
| **P1** | `entry_gate` re-pointed at `engine_decisions` (one-line shim) so B&B/discount are gated by the engine. Cockpit v1: regime strip + opportunity stack over engine tables. | composite, trade_suggester, morning_confluence |
| **P2** | Triggers ported in-engine (ORB, VWAP, break-retest, sonar-band) reading candle cache; factor computations move in-engine (drop `get_latest_*` indirection). | momentum, break-bounce, sonar, iv-rank, oi-buildup, gap, delivery-surge, smart-money *services* (logic lives on inside engine) |
| **P3** | Executor unification: paper_trader + order_manager + auto-exit → convex-executor consuming Decisions. Discount reduced to premium-value factor + contract selector. | discount service, per-strategy notifiers, all gates |
| **P4** | Rename DB, compose file shrinks to 4 services, delete absorbed modules + `old/`. | everything not in §4 |

Rollback at every phase = stop the new container; V1 services are untouched until their kill line.

---
*Companion: `engine/` package (P0 implementation) — see `engine/README.md`.*
