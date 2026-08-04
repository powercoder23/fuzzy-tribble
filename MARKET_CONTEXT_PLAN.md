# Market Context Subsystem — Design & Implementation Plan

**Author:** Lead quant research / architecture
**Date:** 2026-08-03
**Scope:** Add a shared Market Context subsystem. **No strategy is redesigned. No strategy is added.**
**Status:** Rev 2 — operator decisions applied. **Phase 0 implemented.**

---

## Rev 2 — Operator Decisions (2026-08-03)

These decisions **supersede** the sections named. Where this block and a later
section disagree, this block wins.

| # | Decision | Supersedes |
|---|---|---|
| **1** | **Plan tier is a deployment concern, not an architectural one.** Do not block on it. The collector detects the connection allowance at startup and scales: Standard → NIFTY + BANKNIFTY + India VIX + top 50–60 liquid F&O stocks; Plus → automatically expand to the full monitored universe. | §6.2 (no longer a blocker), §14 Q1 (**closed**) |
| **2** | **Six independent axes, not four, and no composite score.** Expose `trend`, `volatility`, `liquidity`, `participation`, `positioning`, `breadth`. Each strategy consumes only the axes it needs — e.g. Vol-Expansion: trend + volatility + participation; Break & Bounce: trend + liquidity; Discount: volatility + positioning. **The Regime Engine describes the market; it does not decide trades.** No trading decision may be hardcoded in it. | §7.1 (four axes → six), §7.6 (**posture rules removed**), §9.1 (contract), §14 Q2 (**closed**) |
| **3** | **Sizing stays fixed at 1 lot.** `size_multiplier` is **not** wired into `book_signal` and is **removed from the contract entirely**. Changing size changes the P&L distribution and makes strategy evaluation harder. Sizing stays independent until a statistically significant edge is proven. | §0.2 C6, §9.3 site 7 (**dropped**), §12.5, §14 Q3 (**closed**) |
| **4** | **Phase 0 approved for immediate implementation** — schema, config, `market_context.get()` returning neutral, and persistence of the snapshot into `factors_json`. No strategy behaviour changes. | §11 Phase 0 — **done** |
| **5** | **Market Context is observational only.** It must **not** veto entries, change exits, change stop-losses, change targets, or change sizing. Collect and persist only. After sufficient paper-trading data, evaluate whether context improves expectancy **before** it is allowed to influence any decision. | §8 (**Exit Engine deferred, not built**), §9.3 sites 5–7 (**deferred**), §11 (phases revised below) |

### Consequences worth stating plainly

**Objectives 5–8 move out of the engine.** The original brief asked the subsystem to answer *"should long premium be aggressive / defensive?"*, *"should option selling be avoided?"* and *"should trades be exited?"*. Under decision 2 those are **trading decisions**, so the engine no longer answers them. It supplies the axes each question needs, and the strategy that owns the position makes the call. `long_premium_bias`, `short_premium_bias`, `size_multiplier` and `exit_warning` are **gone from the contract**, and `test_market_context.py::test_as_dict_has_no_trading_decision_fields` fails the build if anyone reintroduces them.

**Decision 5 is stronger than my own C5 recommendation, and it is the right call.** I argued the Exit Engine was the wrong first lever because 62.4% of exits are already time-based and frictions are 75% of the loss. Decision 5 goes further and forbids *any* influence until measured. That makes the whole subsystem a clean natural experiment: context is recorded alongside every trade while changing nothing about the trade, so its predictive value can be measured without confounding.

**Revised phase plan** (supersedes §11):

| Phase | Deliverable | Influences trading? |
|---|---|---|
| **0 ✅** | Schema, config, `get()`, `factors_json` persistence | No |
| **1 ✅** | Dockerfile + compose service, WS feed client, tier-aware subscription, normaliser, bar aggregator, VIX/futures/breadth/sector collectors, **REST resync**, **plan-tier probe** | No |
| **2 ✅** | Yang-Zhang RV / efficiency-ratio / VRP estimators, feature builder (`mc_features`), six-axis classifiers with asymmetric hysteresis, dwell + K-of-M confirmation, confidence and transition probability, persisted to `mc_regime` | No |
| **3** | Accumulate ≥20 sessions of context-tagged trades | No |
| **4** | **Measurement gate.** Does context predict expectancy? Answer from `factors_json` | No |
| **5** | Only if Phase 4 supports it, and only for the specific axes it supports | Decision required |

`config.influences_trading()` returns `False` and is the single greppable predicate guarding phases 0–4.

### Rev 2 addendum — findings from building Phase 1

Three things surfaced during implementation that change earlier statements in this document.

**A. The breadth sample is not "large-cap biased" — it was penny-stock biased.** §6.3 assumed a liquidity-trimmed universe skews toward large caps. Measured on live data (2026-08-04), `OI × volume` — the metric `discount.py` uses for its top-120 trim — is *share-count* based, so it ranked **IDEA (₹13), YESBANK (₹23), SUZLON (₹49)** above RELIANCE and INFY. Breadth computed on that is speculative-retail breadth wearing the label "market breadth".

`market_context.instruments.liquid_symbols()` therefore ranks by **rupee turnover** (`price × option volume`) by default — a deliberate divergence from `discount.py`, documented in-place. Same query, different question: "which chains are worth scanning" vs "what is the market doing". Switchable via `MC_LIQUIDITY_METRIC=shares`. Tier 3 now resolves to M&M / PAYTM / RELIANCE / BAJAJFINSV / SBIN / INFY / HDFCBANK.

**B. Index symbols were consuming breadth slots.** `NIFTY` and `BANKNIFTY` appear in `iv_history` as if they were stocks and rank 1st/2nd by turnover. They are already tier-1 instruments and are not constituents. Now excluded explicitly, with a regression test.

**D. The REST full-quote schema is not the WebSocket schema.** `MarketQuoteApi.get_full_market_quote` returns `last_price` / `volume` / `average_price` / a plain `ohlc` dict; the streamer returns `ltp` / `vtt` / `atp` / an interval-keyed `ohlc` list. §6.7 assumed one normaliser would cover both. It did not — `restore_quotes()` seeded **zero** ticks, so a resync would have appeared to run and recovered nothing, leaving NULL volume for the remainder of every session with a long gap. Both vocabularies are now in `normaliser._SCALARS` with a regression test. Verified against the installed SDK, not the docs.

**E. Plan detection is measured, not asked.** There is no REST endpoint reporting the connection allowance, so `feed/probe.py` opens two throwaway sockets and counts how many coexist — one always succeeds on any plan, so *concurrency* is the thing that discriminates. Result cached in `mc_meta` for `MC_PLAN_PROBE_TTL_DAYS` (7): a plan change is a billing event, and re-probing every restart would burn connections during a reconnect storm. Any failure returns Standard, because under-subscribing degrades breadth *visibly* (`is_subsample`) whereas over-subscribing would have the socket silently carry fewer instruments than the plan claims.

**C. The breadth universe is capped by collector coverage, not by the plan tier.** `iv_history` carries usable intraday rows for roughly **119 of ~208** F&O names, so even on the Plus plan tier 3 cannot exceed what the IV collector actually sees. `mc_breadth.universe_size` and `sample_quality` record this per row rather than letting it be silent. This is the same coverage gap already noted for the backtest engine.

---

## 0. Executive Summary

### 0.1 What this adds

A single always-on service (`market-context`) that owns a WebSocket feed, computes a point-in-time market context snapshot, persists it, and exposes it to every strategy through one call:

```python
import market_context
ctx = market_context.get()      # frozen dataclass, ~10µs, zero broker calls
```

Strategies stop computing context independently. The subsystem answers all eight objective questions explicitly.

### 0.2 Seven things I want to challenge before we build

These are grounded in the code and in the 573-row paper book, not opinion.

**C1 — You already have a regime engine. It is buried inside one service.**
`engine/regime.py` classifies GREEN/AMBER/RED from VIX + breadth + a NIFTY SuperSmoother slope, and writes `engine_regime`. It is private to `convex-engine`. Building a second regime engine beside it is exactly the fragmentation this project is trying to remove. **Plan: promote it into `market_context`, and reduce `engine/regime.py` to a thin adapter.** Its SuperSmoother implementation is good and gets reused verbatim.

**C2 — Your VIX is EOD-only, so every intraday regime decision uses yesterday's volatility.**
`collectors/vix_collector.py` scrapes NSE `allIndices` at 18:30 and writes `vix_daily`. `engine/regime.py` reads `SELECT close FROM vix_daily ORDER BY date DESC LIMIT 1`. During a live session that row is *yesterday's close*. Your `VIX_RED = 22` no-trade gate cannot fire on a day that spikes to 26 intraday. This is the single highest-impact gap and the WebSocket fixes it for free — India VIX streams as `NSE_INDEX|India VIX`.

**C3 — Your breadth is a by-product of the IV collector, not a measurement.**
`breadth.compute()` derives advance/decline from `iv_history` spot snapshots. Three defects:
- The "open" is the *first IV-collector sweep of the day* (~09:15–09:30), not the actual open. Any opening gap is invisible.
- The sample is the 208 F&O names — large-cap-biased. That is not market breadth; it is F&O-universe breadth.
- It is a **full table scan of today's intraday rows on every call**, and `paper_trader.collect_factor_snapshot()` calls it **once per booked trade**. On 2026-07-31 that was 303 full scans.

**C4 — You have no futures data at all, and two of your requested regime states are definitionally futures states.**
Short Covering and Long Liquidation are price×OI quadrants on *futures* OI. `oi_buildup_scanner` uses aggregate *option* OI, which is a different thing. Nothing in the repo reads a futures contract. This part is genuinely new build, not refactor.

**C5 — The Exit Engine is probably the wrong first lever, and the data says so.**
From the paper book: **62.4%** of closes are already `Time 15:20`, and frictions (₹70,107) are **75%** of the total loss — 3.07× larger than the gross signal deficit (−₹22,845). An exit engine that fires *more often* adds round-trips to a book that is already friction-dominated. The higher-expectancy use of market context is **entry suppression** — trading less, on better context — which reduces friction and loss simultaneously. I will build the Exit Engine because you asked for it, but I am sequencing entry-gating first and recommending the exit path start in `soft` (log-only) mode with a shadow comparison.

**C6 — Market context's natural output is a size multiplier, and you cannot use one.**
Every trade is flat 1 lot (`paper_trader`, confirmed). `engine/config.py` already defines `SIZE_MULT = {GREEN:1.0, AMBER:0.5, RED:0.0}` and `GRADE_SIZE_MULT` — neither reaches the paper path. So `market_context.get()` will *return* `size_multiplier`, but until sizing is wired it can only be consumed as a binary gate (`size_multiplier == 0 → skip`). That is a one-line change in `book_signal` when you want it; I am flagging it rather than silently designing around it.

**C7 — A tick feed will outrun your 5-minute monitor, and that changes your fill model.**
`paper_trader.apply_tick()` is documented as sampling 5-minute LTPs, with the explicit caveat *"intrabar touches between samples are still missed."* Once a WebSocket exists, it becomes tempting to drive exits at tick speed. **Do not do that implicitly.** Paper P&L comparability across the existing 372 trades depends on the 5-minute cadence. Market context runs at its own cadence; the *paper fill model stays at 5 minutes* unless you decide to change it deliberately and re-baseline.

### 0.3 What I am deliberately NOT doing

- Not touching any `*_strategy.py` entry or exit logic.
- Not adding a strategy.
- Not changing the paper fill model.
- Not replacing `breadth.py`'s public API — `market_context` will supersede it, and `breadth.py` becomes a thin shim so nothing breaks.

---

## 1. Research: How Institutions Actually Classify Regimes

You asked for empirically supported ideas and no indicator soup. Here is the honest state of the literature and practice, and what I am taking from each.

### 1.1 The single most important empirical fact

**Volatility is highly persistent and forecastable. Direction is barely forecastable.**

This is the most robust finding in quantitative finance — ARCH/GARCH (Engle 1982), and every realized-volatility study since. Autocorrelation of daily realized variance is strongly positive and decays slowly; autocorrelation of daily returns is approximately zero.

**Design consequence, and it is the central one:** a volatility-state classifier deserves high confidence weight and can drive hard decisions. A direction-state classifier deserves low confidence weight and should only ever *modulate*, never *trigger*. Systems that treat "trending up" with the same conviction as "high volatility" are miscalibrated by construction.

This directly shapes the confidence model in §7.4.

### 1.2 Markov regime-switching (Hamilton 1989)

The canonical academic framework: a latent discrete state with a transition probability matrix, estimated by maximum likelihood. Widely used in institutional asset allocation.

**What I take:** the *structure* — a state vector with transition probabilities, which gives you "is a reversal developing?" as a genuine probability rather than a heuristic, plus dwell-time modelling.

**What I reject for v1:** live MLE estimation. It is unstable on short samples, silently look-ahead-biased if refit on the full history, and unexplainable when it misfires at 14:45. **Plan: implement the transition/dwell *structure* with transparent configurable rules in v1, and leave a clean seam to swap in a fitted HMM later (§12.3).** You need the observation log before you can fit anything anyway.

### 1.3 Trend × Volatility quadrant (the CTA standard)

Managed-futures firms (AHL, Winton, Aspect, Transtrend) overwhelmingly frame regime as a low-dimensional grid — typically trend strength × volatility level — rather than a single label. Time-series momentum is the best-documented cross-asset anomaly (Moskowitz, Ooi & Pedersen, *Time Series Momentum*, JFE 2012, across 58 instruments), though at 1–12 month horizons, not intraday.

**What I take:** the **multi-axis** representation. Your eleven requested states are not mutually exclusive — "Trending Up" and "High Volatility" co-occur constantly, and "Short Covering" is orthogonal to both. Forcing one label destroys information and creates flip-flop.

**Design consequence:** the engine emits **four independent axes** plus a derived composite label (§7.1). This is a genuine improvement on the brief and I want explicit sign-off on it.

**What I reject:** applying TSMOM's 12-month findings to a 5-minute chart. The intraday trend axis gets low confidence weight per §1.1.

### 1.4 Efficiency Ratio (Kaufman) for trend-vs-range

```
ER = |P_t − P_{t−n}| / Σ|P_i − P_{i−1}|     ∈ [0, 1]
```

**Why included:** it directly measures *directional efficiency* — how much net travel you got per unit of path. ER→1 is a clean trend; ER→0 is chop with the same gross movement. It is the cleanest single discriminator between "Trending" and "Range" and it is scale-free, so one threshold works across NIFTY and BANKNIFTY.

**Why this over EMA crossovers:** an EMA stack tells you where price *was*; ER tells you whether the path was *tradeable*. Your existing `directional_iv` EMA-stack classifier (9/20/50/200) reports "bullish" in a grinding chop as readily as in a clean trend. ER separates them.

### 1.5 Realized volatility estimators

Close-to-close vol wastes the OHLC information you already collect. The estimator hierarchy is well established:

| Estimator | Uses | Efficiency vs close-to-close | Handles |
|---|---|---|---|
| Close-to-close | C | 1× | nothing |
| Parkinson (1980) | H, L | ~5× | — |
| Garman–Klass (1980) | O,H,L,C | ~7× | — |
| Rogers–Satchell (1991) | O,H,L,C | ~6× | drift |
| **Yang–Zhang (2000)** | O,H,L,C + overnight | ~8× | **drift + opening jumps** |

**What I take: Yang–Zhang on 1-minute bars.** Indian index futures gap at the open regularly; YZ is the only common estimator that handles both the overnight jump and intraday drift. Its ~8× efficiency means a usable vol estimate from ~30 minutes of 1-min bars instead of needing days.

**Why this matters to you specifically:** `discount.py` computes a `weighted_hv` whose window composition is not even surfaced as named constants (flagged as `[NOT SPECIFIED]` in the strategy spec). A proper, named, tested RV estimator at index level is a genuine upgrade and becomes reusable.

### 1.6 Variance Risk Premium (Bollerslev, Tauchen & Zhou 2009)

```
VRP = IV² − E[RV²]        (implied variance minus expected realized variance)
```

Documented to predict equity returns, and it is *the* structural reason option selling is profitable on average: implied consistently exceeds subsequent realized.

**Why this is your biggest missing feature.** You run an options platform with an IV collector, an IV-rank scanner, and a premium-selling strategy (`iv_seller`) — and you have **no realized volatility at all**, so you cannot compute the one number that says whether selling premium is currently rich or cheap. `iv_seller` gates on IV *percentile* (IV vs its own history), which answers "is IV high for this name?" but not "is IV high *relative to what actually happened*?" Those differ exactly when it matters — a name whose IV is at the 70th percentile because realized vol tripled is not a sell.

**Design consequence:** VRP is a first-class feature and the primary input to the `avoid_option_selling` output (objective #7).

### 1.7 Volatility term structure

VIX futures contango/backwardation is a practitioner-standard stress gauge: backwardation (front > back) signals near-term stress being priced.

**India constraint:** India VIX futures are effectively illiquid, so the standard construction is unavailable. **Substitute:** synthesise a term structure from NIFTY ATM IV at the near vs next expiry — both already reachable through your existing `get_expiry_list` + option chain path.

```
ts_slope = (ATM_IV_next − ATM_IV_near) / ATM_IV_near
ts_slope < 0  → backwardation → near-term stress
```

**Why included:** it distinguishes "high IV because of a scheduled event next week" from "high IV because the market is breaking now." Your platform explicitly has *no event calendar* (`iv_analytics.py` says so outright), so term structure is the only handle you have on that distinction. This is a **direct, data-derived partial substitute for the missing event calendar.**

### 1.8 Dealer gamma exposure (GEX)

Practitioner framework (popularised by SqueezeMetrics): estimate net dealer gamma from open interest × gamma × contract multiplier, signed by an assumption about which side dealers are on.

- **Dealers net long gamma** → they hedge *against* moves → suppressed realized vol, mean-reversion, pinning near large-OI strikes.
- **Dealers net short gamma** → they hedge *with* moves → amplified moves, trend persistence, gap risk.

**Why this matters more to you than to most:** it is the most direct available answer to objectives #5 and #6. *Should long premium be aggressive?* Long premium in a positive-gamma pinning regime is a theta donation regardless of direction; in a negative-gamma regime the same position is convex. Your buy-side strategies (`discount`, `vol_expansion`, `directional_iv`) currently have no way to distinguish these.

**Honest caveat:** the dealer-positioning sign is an *assumption* (conventionally: dealers short calls, long puts, from retail flow). It is a proxy, not measured positioning. **Plan: implement as `gex_proxy` in Phase 4, ship it in observe-only mode, and label it a proxy in the schema.** Not a v1 gate.

### 1.9 Breadth and participation

Breadth thrust indicators (Zweig) are widely cited and weakly supported in isolation. Advance/decline *divergence* from index price has better standing as a **confirmation/warning** signal than as a trigger.

**What I take:** breadth as a *confirmation and divergence* input, never a standalone trigger.

The higher-value construction is **volume breadth** — up-volume / total volume — rather than name counts. A 55% advance/decline on thin volume and a 55% on heavy volume are different tapes. You currently compute only name counts, and only on the F&O universe.

**Divergence rule (this is the empirically defensible part):** index makes a new session high while breadth *fails* to confirm → `REVERSAL` risk. That is a real, testable pattern and it feeds objective #4.

### 1.10 Correlation / dispersion regime

Average pairwise correlation of constituents rises toward 1 in stress (CBOE publishes an implied correlation index on exactly this basis).

**Why you specifically need it:** your concentration gate is **armed but empty** — `PORTFOLIO_GATE_MODE = "hard"` with `PORTFOLIO_MAX_SAME_DIRECTION = 0` and `PORTFOLIO_MAX_PER_SECTOR = 0`, both meaning unlimited. It runs, loads the sector map, counts everything, and blocks nothing. On 2026-07-31 you held 12 HINDUNILVR positions, 10 LT, 10 M&M. In a high-correlation regime, 30 "diversified" long-premium positions are **one** bet.

Dispersion also has a direct strategy meaning: **low dispersion / high correlation favours index premium selling; high dispersion favours single-stock premium buying.** That is real institutional logic and it maps onto strategies you already run.

**Cheap proxy for v1 (no covariance matrix needed):**
```
dispersion = stdev(sector_returns)          # cross-sectional
implied_corr_proxy = 1 − (dispersion / mean|sector_return|)   # bounded, clipped
```

### 1.11 Futures basis and positioning quadrants

Basis `F − S` decomposed as annualized carry:
```
basis_ann = ((F − S) / S) × (365 / days_to_expiry) × 100
```
Persistent basis above cost-of-carry indicates long positioning demand; discount indicates hedging/short pressure.

The India-standard price×OI quadrant maps **exactly** onto four of your requested states:

| Price | OI | State | Interpretation |
|---|---|---|---|
| ↑ | ↑ | **LONG_BUILDUP** | New longs, conviction |
| ↓ | ↑ | **SHORT_BUILDUP** | New shorts, conviction |
| ↑ | ↓ | **SHORT_COVERING** | Forced buying, weak rally |
| ↓ | ↓ | **LONG_LIQUIDATION** | Position unwind, weak selloff |

**Why this is high-value:** it distinguishes a *sustainable* move from a *forced* one. A rally on short covering with falling OI typically doesn't continue — the fuel is exhausted buying, not new conviction. Your strategies currently cannot tell those apart, and short-covering rallies are precisely where a long-CE breakout entry gets trapped.

### 1.12 Regime persistence, hysteresis, and dwell time

This is the least glamorous and most important section, and it is what separates a production regime engine from an indicator mashup.

A classifier that flips state every bar is worse than no classifier: it whipsaws every consumer downstream. Institutional implementations universally apply:

- **Hysteresis** — asymmetric entry/exit thresholds (enter HIGH_VOL at 75th percentile, exit only below 60th). Prevents boundary chatter.
- **Minimum dwell time** — a state must persist N observations before it is published.
- **Confirmation count** — K of the last M observations must agree.
- **Explicit `TRANSITIONING` state** — do not force a label during genuine ambiguity; say so, and let confidence collapse.

**All four are in the design (§7.5) and all are configurable.** This is where most home-built regime engines fail.

### 1.13 Point-in-time discipline

Any regime series used for research must be reconstructible *as it was known at the time*. Rows are append-only, never updated; every snapshot stores its input feature vector and the config hash that produced it.

**Why this matters here:** your platform's stated purpose is measuring edge. A regime label that got silently retro-corrected makes every backtest that uses it invalid. Your `paper_trades.factors_json` already follows this discipline correctly — the market-context tables will match it.

---

## 2. Institutional Features You Are Currently Missing

Ranked by (value to your platform) × (feasibility with what you already have).

| # | Feature | Why it matters to *you* | Data needed | Phase |
|---|---|---|---|---|
| **1** | **Intraday VIX** | Every regime decision today uses yesterday's close (C2). `VIX_RED=22` cannot fire intraday | WS: `NSE_INDEX|India VIX` | 1 |
| **2** | **Realized volatility (Yang–Zhang)** | You have zero RV. Blocks VRP, blocks vol-state, blocks IV-vs-RV | 1-min bars (WS) | 1 |
| **3** | **Variance Risk Premium** | The one number that says whether selling premium is currently rich. `iv_seller` has no such input | RV + ATM IV (have) | 2 |
| **4** | **Futures basis + OI quadrant** | Only way to get SHORT_COVERING / LONG_LIQUIDATION. Distinguishes real moves from forced ones | WS futures | 1 |
| **5** | **IV term structure (near vs next)** | Partial substitute for your missing event calendar | Option chain (have) | 2 |
| **6** | **Volume breadth** | Name-count breadth can't tell thin from heavy participation | WS stock ticks | 2 |
| **7** | **Correlation / dispersion** | Your concentration gate is a no-op; this tells you when N positions are 1 bet | Sector index ticks | 2 |
| **8** | **FII/DII participant-wise OI** | India-specific, free, daily, genuinely institutional. NSE publishes F&O participant OI (FII/DII/Pro/Client) | NSE CSV, EOD | 3 |
| **9** | **Rollover %** | Near→far OI migration; expiry-week positioning | WS futures (2 expiries) | 3 |
| **10** | **Dealer gamma proxy (GEX)** | Most direct answer to "should long premium be aggressive?" | Option chain OI + greeks (have) | 4 |
| **11** | **Intraday seasonality normalisation** | You *discovered* the U-shape (`intraday_decay_curve`) and never use it. A vol reading at 09:20 and 13:00 aren't comparable without it | 1-min bars, N sessions | 3 |
| **12** | **Regime dwell / hysteresis** | Prevents whipsaw. Missing from `engine/regime.py` today | none — pure logic | 1 |
| **13** | **Feed-gap register** | Research honesty: know which minutes were blind | WS connection log | 1 |

**#8 deserves a special note.** NSE publishes participant-wise open interest daily (FII / DII / Pro / Client, split by index futures, index options, stock futures, stock options). It is free, it is genuinely institutional positioning rather than a proxy, and almost no retail system uses it. For an India-specific F&O platform this is unusually high value-per-effort — one daily CSV fetch, same shape as your existing `bhav_collector`.

---

## 3. Architecture

### 3.1 Placement in the existing system

```
                    ┌──────────────────────────────────────────┐
                    │  UPSTOX  (REST + WebSocket V3)           │
                    └───────┬──────────────────────┬───────────┘
                            │ WS (new)             │ REST (existing)
                            ▼                      ▼
        ┌───────────────────────────────┐   ┌──────────────────┐
        │  market-context  [NEW]        │   │  iv-collector    │
        │  ───────────────────────      │   │  (unchanged)     │
        │  • WS feed  • collectors      │   └────────┬─────────┘
        │  • features • regime engine   │            │
        │  • exit engine                │            │
        │  SOLE WRITER: mc_* tables     │            │ SOLE WRITER:
        └───────────────┬───────────────┘            │ iv_history
                        │                            │
                        ▼                            ▼
        ┌───────────────────────────────────────────────────────┐
        │            data/market_context.db  +  iv_history.db   │
        └───────────────┬───────────────────────────────────────┘
                        │  read-only
        ┌───────────────┴───────────────────────────────────────┐
        │             market_context.get()   ← THE ONLY API     │
        └───┬────────┬────────┬────────┬────────┬───────────┬───┘
            ▼        ▼        ▼        ▼        ▼           ▼
        discount  break-  vol-exp  direct-  iv-seller  convex-engine
                  bounce           ional                (regime.py →
                                                         thin adapter)
                            ▲
                            │
                  order_manager gates + exit engine
```

**Design rules, matching your existing discipline:**

1. **One writer.** Only `market-context` writes `mc_*` tables. Identical to your `iv_store` sole-writer contract.
2. **One socket.** Only `market-context` holds a WebSocket. Strategy containers never open one — they'd multiply connections against your per-user cap and duplicate work.
3. **Consumers read SQLite.** Zero broker calls in any consumer, matching your zero-API scanner pattern.
4. **Fail-open everywhere.** `get()` never raises. On any failure it returns a neutral context with `available=False`. No strategy can be broken by this subsystem.
5. **Config idiom preserved.** `off / soft / hard` modes, env override, settings-DB override via `settings_store.flag_*` — identical to `PORTFOLIO_GATE_MODE`, `AUTO_EXIT_OI_MODE`, etc.

### 3.2 Why a separate DB file (`market_context.db`)

`iv_history.db` is **315 MB with a 5.2 MB WAL** and 19 tables, written continuously by `iv-collector`. Tick-derived 1-minute bars across ~100 instruments add roughly 40k rows/day.

Separating gives:
- Write contention isolation — a WS burst cannot stall the IV collector's writes.
- Independent retention/vacuum policy.
- Trivially droppable during development without risking your IV history.
- Independent backup cadence.

**Cost:** cross-DB joins for research. Mitigated by `ATTACH DATABASE` in the research helper, and by `mc_features` denormalising the few `iv_history` values it needs (ATM IV) at snapshot time — which is *required anyway* for point-in-time correctness.

### 3.3 Internal pipeline

```
  WS frames ──► Normaliser ──► TickCache (in-memory, last value + 1-min aggregator)
                                   │
                     ┌─────────────┴──────────────┐
                     ▼ (every 60s)                ▼ (every SNAPSHOT_INTERVAL, default 60s)
              BarWriter → mc_bars_1m       FeatureBuilder
                                                  │
                                            mc_features
                                                  │
                                            RegimeEngine ──► mc_regime
                                                  │
                                             ExitEngine ──► mc_exit_signals
                                                  │
                                          market_context.get()
```

**Two cadences, deliberately separate:**
- **Ingest** is event-driven (WS frames arrive continuously).
- **Snapshot** is wall-clock (default 60s, configurable).

Decoupling them means feature computation cost is bounded and independent of tick rate, and every snapshot has a clean, reproducible timestamp for point-in-time research.

---

## 4. Folder Structure

Matches the `engine/` package convention already in the repo (internal `config.py`, `contracts.py`).

```
market_context/
├── __init__.py                 # PUBLIC API: get(), MarketContext, refresh()
├── config.py                   # every threshold; env + settings-DB overridable
├── contracts.py                # frozen dataclasses (MarketContext, RegimeState, ...)
├── store.py                    # schema DDL + sole-writer persistence + connect()
├── service.py                  # daemon entrypoint (the container command)
│
├── feed/
│   ├── __init__.py
│   ├── client.py               # UpstoxFeedClient: connect/auth/reconnect/heartbeat
│   ├── subscription.py         # tiered, budget-aware subscription planner
│   ├── normaliser.py           # Upstox frame → internal Tick/Quote dataclass
│   ├── cache.py                # TickCache: last-value + 1-min bar aggregator
│   └── resync.py               # REST backfill after a gap; gap register
│
├── collect/
│   ├── __init__.py
│   ├── futures.py              # basis, OI change, rollover
│   ├── breadth.py              # adv/dec + volume breadth
│   ├── sector.py               # sector breadth + relative strength
│   ├── vix.py                  # intraday VIX series
│   └── participants.py         # [Phase 3] NSE FII/DII participant OI (EOD)
│
├── features/
│   ├── __init__.py
│   ├── trend.py                # ER, SuperSmoother slope, vol-scaled momentum, VWAP position
│   ├── volatility.py           # Yang-Zhang RV, VIX z-score, VRP, vol-of-vol, RV ratio
│   ├── structure.py            # range position, ORB, prior-day levels, breadth divergence
│   ├── positioning.py          # futures price×OI quadrant, basis, rollover
│   ├── breadth_features.py     # adv/dec %, volume breadth, thrust
│   ├── dispersion.py           # sector dispersion, implied-correlation proxy
│   ├── term_structure.py       # [Phase 2] near vs next ATM IV slope
│   └── gex.py                  # [Phase 4] dealer gamma proxy
│
├── regime/
│   ├── __init__.py
│   ├── engine.py               # orchestrates axes → composite → confidence
│   ├── axes.py                 # the 4 independent axis classifiers
│   ├── hysteresis.py           # dwell time, confirmation count, asymmetric bands
│   ├── transition.py           # transition detection, strengthening/weakening
│   └── confidence.py           # confidence scoring
│
├── exit/
│   ├── __init__.py
│   └── engine.py               # context-deterioration exit signals
│
└── research/
    ├── __init__.py
    └── replay.py               # point-in-time regime replay for backtesting
```

**Root-level additions:**
```
Dockerfile.market-context
market_context_shim.py          # optional: keeps legacy `import breadth` working
test_market_context_*.py        # tests stay flat, matching repo convention
```

---

## 5. Database Schema

`data/market_context.db`, WAL, `busy_timeout=30000` — same connection discipline as `iv_store.connect()`.

### 5.1 Instrument registry

```sql
CREATE TABLE IF NOT EXISTS mc_instruments (
    instrument_key  TEXT PRIMARY KEY,       -- 'NSE_INDEX|Nifty 50'
    symbol          TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- index|futures|equity|vix|sector_index
    underlying      TEXT,
    expiry          TEXT,                   -- futures only
    tier            INTEGER NOT NULL,       -- 1=critical .. 4=optional
    mode            TEXT NOT NULL,          -- ltpc|full|option_greeks|full_d30
    lot_size        INTEGER,
    active          INTEGER DEFAULT 1,
    added_at        TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_mc_inst_tier ON mc_instruments(tier, active);
```

Makes the subscription set **data-driven and auditable**, not a hardcoded list. Rotating the futures expiry each month is an UPDATE, not a deploy.

### 5.2 1-minute bars — the research substrate

```sql
CREATE TABLE IF NOT EXISTS mc_bars_1m (
    instrument_key TEXT NOT NULL,
    ts             TEXT NOT NULL,           -- bar START, 'YYYY-MM-DD HH:MM:00' IST
    open  REAL, high REAL, low REAL, close REAL,
    volume         REAL,                    -- cumulative vtt delta within the bar
    oi             REAL,                    -- snapshot at bar close
    oi_chg         REAL,                    -- vs previous day close OI (poi)
    vwap           REAL,                    -- Upstox atp
    tick_count     INTEGER,                 -- frames seen; data-quality signal
    PRIMARY KEY (instrument_key, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_mc_bars_ts ON mc_bars_1m(ts);
```

**Duplicate-write avoidance:** `PRIMARY KEY (instrument_key, ts)` + `INSERT ... ON CONFLICT DO NOTHING` — matching your `iv_history` `UNIQUE(security_id, timestamp, data_type)` idiom exactly. A restart mid-minute cannot double-write. `WITHOUT ROWID` because the PK *is* the natural key — meaningfully smaller and faster for this access pattern.

**Volume derivation caveat:** Upstox `vtt` is cumulative volume-traded-today. Bar volume is `vtt_end − vtt_start`. On the first bar after a reconnect the delta is unknown → write `NULL`, not `0`. A false zero corrupts volume breadth; a NULL is honest and excludable.

### 5.3 Futures snapshot

```sql
CREATE TABLE IF NOT EXISTS mc_futures (
    ts              TEXT NOT NULL,
    instrument_key  TEXT NOT NULL,
    symbol          TEXT,
    expiry          TEXT,
    dte             INTEGER,
    ltp             REAL,
    spot            REAL,                   -- matched underlying at same ts
    basis           REAL,                   -- ltp - spot
    basis_pct       REAL,
    basis_annualised REAL,
    oi              REAL,
    oi_prev_day     REAL,                   -- Upstox poi
    oi_chg_pct      REAL,
    price_chg_pct   REAL,                   -- vs prev close
    quadrant        TEXT,                   -- LONG_BUILDUP|SHORT_BUILDUP|SHORT_COVERING|LONG_LIQUIDATION|NEUTRAL
    volume          REAL,
    vwap            REAL,
    day_high REAL, day_low REAL, day_open REAL, prev_close REAL,
    PRIMARY KEY (ts, instrument_key)
) WITHOUT ROWID;
```

### 5.4 Breadth, sector, VIX

```sql
CREATE TABLE IF NOT EXISTS mc_breadth (
    ts             TEXT PRIMARY KEY,
    universe_size  INTEGER,
    advancing      INTEGER,
    declining      INTEGER,
    unchanged      INTEGER,
    adv_dec_pct    REAL,                    -- adv/(adv+dec)*100
    up_volume      REAL,
    down_volume    REAL,
    volume_breadth_pct REAL,                -- up_vol/(up_vol+down_vol)*100
    new_highs      INTEGER,
    new_lows       INTEGER,
    thrust         REAL,                    -- d(adv_dec_pct)/dt over THRUST_LOOKBACK
    sample_quality REAL                     -- fraction of universe with fresh ticks
);

CREATE TABLE IF NOT EXISTS mc_sector (
    ts             TEXT NOT NULL,
    sector         TEXT NOT NULL,
    ret_pct        REAL,                    -- vs day open
    rel_strength   REAL,                    -- sector ret - NIFTY ret
    rs_rank        INTEGER,
    advancing      INTEGER,
    declining      INTEGER,
    breadth_pct    REAL,
    n_names        INTEGER,
    PRIMARY KEY (ts, sector)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS mc_vix (
    ts          TEXT PRIMARY KEY,
    ltp         REAL,
    prev_close  REAL,
    chg_pct     REAL,
    day_high    REAL,
    day_low     REAL,
    percentile  REAL,                       -- vs VIX_PERCENTILE_LOOKBACK days
    z_score     REAL,
    vol_of_vol  REAL                        -- stdev of VIX % changes, rolling
);
```

`mc_vix` complements — does not replace — the existing daily `vix_daily`. That table stays, its collector stays.

### 5.5 Feature vector — point-in-time

```sql
CREATE TABLE IF NOT EXISTS mc_features (
    ts              TEXT PRIMARY KEY,
    -- trend
    ef_ratio        REAL,   ss_slope_pct   REAL,
    mom_z           REAL,   vwap_position  REAL,
    -- volatility
    rv_yz_short     REAL,   rv_yz_long     REAL,   rv_ratio  REAL,
    vix_level       REAL,   vix_percentile REAL,   vol_of_vol REAL,
    vrp             REAL,   iv_ts_slope    REAL,
    -- structure
    range_position  REAL,   orb_state      TEXT,
    prior_day_state TEXT,   breadth_divergence REAL,
    -- breadth
    adv_dec_pct     REAL,   volume_breadth_pct REAL,   thrust REAL,
    -- positioning
    nifty_quadrant  TEXT,   banknifty_quadrant TEXT,
    basis_ann_nifty REAL,   basis_ann_banknifty REAL,
    stock_fut_long_pct REAL,
    -- dispersion
    sector_dispersion REAL, implied_corr_proxy REAL,
    -- meta (point-in-time integrity)
    data_quality    REAL,                   -- 0..1
    missing_inputs  TEXT,                   -- JSON array of names
    config_hash     TEXT                    -- hash of the config that produced this row
);
```

`config_hash` is what makes historical research valid: if a threshold changed on 2026-09-01, every row before it carries the old hash and you can partition rather than silently mixing two definitions. **This is the single most important column in the schema for your stated research purpose.**

### 5.6 Regime output

```sql
CREATE TABLE IF NOT EXISTS mc_regime (
    ts                TEXT PRIMARY KEY,
    -- four independent axes
    trend_state       TEXT,   -- TRENDING_UP|TRENDING_DOWN|RANGE|TRANSITIONING
    trend_score       REAL,   -- signed, -1..+1
    vol_state         TEXT,   -- LOW_VOL|NORMAL_VOL|HIGH_VOL|PANIC
    vol_score         REAL,   -- 0..1
    structure_state   TEXT,   -- NONE|BREAKOUT|BREAKDOWN|REVERSAL
    structure_score   REAL,
    positioning_state TEXT,   -- LONG_BUILDUP|SHORT_BUILDUP|SHORT_COVERING|LONG_LIQUIDATION|NEUTRAL
    positioning_score REAL,
    -- derived
    composite_regime  TEXT,   -- single label by configurable precedence
    confidence        REAL,   -- 0..1
    -- dynamics
    direction         TEXT,   -- STRENGTHENING|WEAKENING|STABLE
    momentum_of_state REAL,   -- d(dominant score)/dt
    dwell_minutes     INTEGER,
    transition_prob   REAL,   -- P(state change within TRANSITION_HORIZON)
    prev_regime       TEXT,
    transitioned      INTEGER DEFAULT 0,
    -- posture (advisory)
    long_premium_bias TEXT,   -- AGGRESSIVE|NEUTRAL|DEFENSIVE|AVOID
    short_premium_bias TEXT,  -- FAVOURABLE|NEUTRAL|AVOID
    size_multiplier   REAL,
    reasons           TEXT    -- JSON array, human-readable
);
CREATE INDEX IF NOT EXISTS ix_mc_regime_transitioned ON mc_regime(transitioned, ts);
```

### 5.7 Exit signals and the gap register

```sql
CREATE TABLE IF NOT EXISTS mc_exit_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    scope       TEXT NOT NULL,              -- GLOBAL|SIDE|SECTOR|SYMBOL
    scope_value TEXT,                       -- 'CE' / 'NIFTY IT' / 'INFY'
    level       TEXT NOT NULL,              -- NONE|WATCH|REDUCE|EXIT
    score       REAL,
    trigger     TEXT,                       -- REGIME_FLIP|TREND_DECAY|VOL_SPIKE|...
    reasons     TEXT,
    regime_before TEXT, regime_after TEXT,
    acted_on    INTEGER DEFAULT 0,          -- did order_manager act? (hard mode only)
    UNIQUE(ts, scope, scope_value, trigger)
);

CREATE TABLE IF NOT EXISTS mc_feed_gaps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    duration_sec  REAL,
    reason        TEXT,                     -- DISCONNECT|STALE|AUTH_EXPIRY|SHUTDOWN
    instruments_affected INTEGER,
    resynced      INTEGER DEFAULT 0
);
```

**`mc_feed_gaps` is a research-integrity feature, not an ops nicety.** Without it, a 20-minute outage looks in the data like 20 minutes of a perfectly stable regime. Any study using regime series must be able to exclude blind windows.

### 5.8 Retention

| Table | Retention | Rationale |
|---|---|---|
| `mc_bars_1m` | 90 days hot, then aggregate to 5-min and archive | ~40k rows/day; the volume driver |
| `mc_features` | Forever | ~375 rows/day. Tiny, and it is the research asset |
| `mc_regime` | Forever | Same |
| `mc_futures` | 90 days | Reconstructible from bars |
| `mc_breadth` / `mc_sector` | Forever | Small |
| `mc_exit_signals` | Forever | Audit trail |
| `mc_feed_gaps` | Forever | Research integrity |

Nightly `PRAGMA optimize` + weekly `VACUUM` in a maintenance job.

---

## 6. WebSocket Design

### 6.1 Confirmed API surface

From current Upstox docs (verified 2026-08-03):

- **Class:** `upstox_client.MarketDataStreamerV3(api_client, instrument_keys, mode)`
- **Modes:** `ltpc` · `full` · `option_greeks` · `full_d30`
- **Methods:** `connect()`, `disconnect()`, `subscribe(keys, mode)`, `unsubscribe(keys)`, `changeMode(keys, mode)`, `auto_reconnect(enable, interval, retryCount)`
- **Instrument key format:** `NSE_INDEX|Nifty 50`, `NSE_INDEX|Nifty Bank`, `NSE_EQ|INE002A01018`, `NSE_FO|50201`

**Mode payloads:**
- `ltpc` → `ltp`, `ltt`, `ltq`, `cp` (prev close)
- `full` → LTPC + D5 depth + **1-min / 30-min / daily candles** + `eFeedDetails`
- `eFeedDetails` carries: `atp` (**VWAP**), `cp` (**prev close**), `vtt` (**volume today**), `oi`, `poi` (**prev-day OI → OI change**), `dhoi`/`dloi` (day high/low OI), `tbq`/`tsq`, `lc`/`uc`

**This single `full` frame supplies almost your entire collection list** — LTP, OHLC, Volume, OI, OI Change, VWAP, Day High/Low, Prev Close, Open. Only **Basis** must be computed (`futures_ltp − spot_ltp`), which is why futures and their underlying must be subscribed *in the same connection* so both are timestamp-aligned.

### 6.2 ⚠ Subscription limits — must verify before building

The docs surface two figures and they need reconciling against your actual plan:

- A **100 instrument keys per socket** limit, stated on a page titled *"Market Data Feed Discontinued"* — this is the **legacy V2** feed.
- **Websocket Plus** (Upstox Plus plan): **5 concurrent connections per user**, and `full_d30` capped at **50 instruments per connection**.

**I am not going to assert a precise V3 per-mode cap I cannot verify.** Instead the design is **budget-aware with graceful degradation** (§6.3), driven by config:

```python
WS_MAX_KEYS_PER_CONNECTION = int(os.getenv("MC_WS_MAX_KEYS", "100"))
WS_MAX_CONNECTIONS         = int(os.getenv("MC_WS_MAX_CONNECTIONS", "1"))
```

**Action item before Phase 1:** confirm your plan tier and the live V3 caps. This changes stock-universe coverage materially and is the one open input I need from you.

### 6.3 Tiered, budget-aware subscription strategy

Instruments are ranked by tier. The planner fills the budget top-down and **degrades gracefully** — it never silently drops a Tier-1 instrument to fit a Tier-3 one.

| Tier | Instruments | Count | Mode | Why this mode |
|---|---|---|---|---|
| **1** | NIFTY 50 spot, NIFTY BANK spot, **India VIX**, NIFTY fut (near+next), BANKNIFTY fut (near+next) | **7** | `full` | Need OHLC/OI/VWAP/basis. Non-negotiable — the regime engine cannot run without these |
| **2** | Sector indices: IT, AUTO, PHARMA, FMCG, METAL, REALTY, ENERGY, FINSERVICE, PSU BANK, MEDIA, CONS DURABLES, OIL&GAS, HEALTHCARE, PVT BANK | **~14** | `ltpc` | Only need returns for RS + dispersion. `ltpc` + `cp` gives % change. Cheapest possible |
| **3** | Top-N F&O **stock spots** by liquidity, for breadth | **N (budget)** | `ltpc` | Breadth needs direction-of-move only |
| **4** | Top-M F&O **stock futures** by OI, for aggregate positioning | **M (budget)** | `full` | Need OI + basis |

**Budget allocation with 1 connection @ 100 keys:**
```
Tier 1:   7  (full)          → mandatory
Tier 2:  14  (ltpc)          → mandatory
Tier 3:  59  (ltpc)          → top 59 stocks by OI×volume
Tier 4:  20  (full)          → top 20 stock futures by OI
         ───
        100
```

**With Plus (5 connections):** Tier 3 expands to the full ~200 stock universe across connections 2–3; Tier 4 to ~60.

**Honesty requirement — and this matters.** Breadth computed on a 59-name liquid subsample is *large-cap-biased breadth*, which is the exact flaw in your current `breadth.py` (C3). It must not be reported as "market breadth." Therefore:

- `mc_breadth.universe_size` records the actual sample size.
- `mc_breadth.sample_quality` records the fraction with fresh ticks.
- `market_context.get().breadth.is_subsample` is `True` whenever `universe_size < BREADTH_FULL_UNIVERSE_MIN`.
- Confidence is **penalised** when breadth is a subsample.

This is a real limitation of the cheap plan, surfaced rather than hidden.

**Selection is dynamic and cached daily.** Tier 3/4 membership is recomputed at 08:45 from `iv_history` OI×volume (same ranking `discount.py` already uses for its liquid-120 trim — reuse that code) and written to `mc_instruments`. Stable membership avoids intraday churn; a name entering the top-59 mid-session is not worth a resubscribe.

### 6.4 Connection lifecycle

```
                 ┌─────────────┐
        ┌───────►│ DISCONNECTED│
        │        └──────┬──────┘
        │               │ connect()
        │        ┌──────▼──────┐
        │        │ AUTHORISING │  REST → short-lived WSS URL
        │        └──────┬──────┘
        │               │
        │        ┌──────▼──────┐
        │        │ CONNECTING  │
        │        └──────┬──────┘
        │               │ on_open
        │        ┌──────▼──────┐
        │        │ SUBSCRIBING │  replay FULL desired-state set
        │        └──────┬──────┘
        │               │ first frame
        │        ┌──────▼──────┐
        │        │   STREAMING │◄─┐
        │        └──────┬──────┘  │ frames
        │               │         └─
        │      stale / error / close
        │        ┌──────▼──────┐
        └────────┤ BACKING_OFF │  exp backoff + jitter
                 └─────────────┘
```

**Resubscribe from declarative desired state, never from an incremental log.** On every reconnect the client re-sends the complete `{mode: [keys]}` set from `mc_instruments`. Incremental subscribe/unsubscribe replay is the classic source of silent drift where you believe you're subscribed and aren't.

### 6.5 Reconnect strategy

```python
RECONNECT_BASE_SEC     = 1.0
RECONNECT_MAX_SEC      = 30.0
RECONNECT_MULTIPLIER   = 2.0
RECONNECT_JITTER_PCT   = 0.25
RECONNECT_MAX_ATTEMPTS = 0          # 0 = unlimited during market hours
```

delay = `min(BASE × MULT^(n−1), MAX) × (1 ± JITTER)` → 1s, 2s, 4s, 8s, 16s, 30s, 30s…

Jitter matters if you ever run multiple connections — it prevents synchronised reconnect storms.

**Decision: do NOT use the SDK's built-in `auto_reconnect()`.** Reasons:
1. It cannot re-run **REST re-authorisation**, and the WSS URL is short-lived. Your access token also rotates daily (`upstox_token_manager.py`). A reconnect that reuses a dead URL loops forever.
2. It gives no hook to write `mc_feed_gaps`.
3. It cannot trigger REST resync.

We own the loop. `auto_reconnect(enable=False)` explicitly.

**Outside market hours** the client idles rather than reconnect-looping: `MARKET_OPEN = "09:00"`, `MARKET_CLOSE = "15:45"` (configurable, IST). Prevents overnight log spam and pointless token pressure.

### 6.6 Heartbeat and staleness — the critical detail

**A dead feed usually presents as a live TCP connection that has stopped delivering.** Waiting for a socket error can mean 10+ blind minutes. Therefore staleness detection is primary, ping/pong secondary:

```python
last_frame_at = monotonic()          # updated on EVERY frame

# watchdog thread, every WS_WATCHDOG_INTERVAL_SEC (default 5)
if market_is_open() and (monotonic() - last_frame_at) > WS_STALE_TIMEOUT_SEC:   # default 20
    log_gap(reason="STALE")
    force_reconnect()
```

**Per-tier staleness.** A Tier-3 mid-cap can legitimately go 60s without a trade at 13:00; NIFTY cannot go 5s. So staleness is judged on **Tier-1 instruments only** — they always tick during market hours. A Tier-3 name going quiet is *data*, not an outage.

```python
WS_STALE_TIMEOUT_SEC       = 20    # tier-1 silence → reconnect
WS_TIER3_STALE_WARN_SEC    = 300   # tier-3 silence → mark stale, don't reconnect
```

### 6.7 Resync after a gap

On reconnect, before resuming normal operation:

1. **Record the gap** — close the open `mc_feed_gaps` row with `ended_at` and `duration_sec`.
2. **If `duration_sec > RESYNC_THRESHOLD_SEC`** (default 120):
   - REST `market-quote/quotes` for all Tier-1 + Tier-2 keys (one batched call) → recover current OHLC/OI/VWAP so day-cumulative values are correct.
   - REST 1-min historical candles for Tier-1 to backfill `mc_bars_1m` across the gap.
   - Mark backfilled bars `tick_count = 0` — distinguishes REST-backfilled from WS-observed bars. Research can then exclude or down-weight them.
3. **Reset cumulative-delta baselines.** `vtt` is cumulative; the first post-gap bar's volume delta is unknown → write `NULL` (§5.2).
4. **Set `data_quality`** on the next `mc_features` row proportional to time-since-resync, so confidence is automatically suppressed while the picture is still rebuilding.

**REST usage stays minimal:** one batched quote call plus Tier-1 candles per gap. On a clean day that is **zero** REST calls after startup. This satisfies your "minimize REST" requirement — REST is the *recovery* path, not the *data* path.

### 6.8 Caching

Three layers, deliberately:

| Layer | Contents | Lifetime | Purpose |
|---|---|---|---|
| **L1 — TickCache** | `{instrument_key: LastQuote}` in-memory dict | Process | O(1) current state; every feature reads here |
| **L2 — BarAggregator** | Open 1-min bar per instrument | 60s | Accumulates OHLCV/OI before flush |
| **L3 — Snapshot cache** | Last `MarketContext` object | `GET_CACHE_TTL_SEC` (default 10) | Makes `get()` free for callers |

**L1 is a bounded dict, not a growing list.** ~100 keys × ~200 bytes ≈ 20 KB. No memory growth.

**L3 lives in the *consumer* process.** Each strategy container caches the last row it read from SQLite for 10s. So `paper_trader.collect_factor_snapshot()` booking 40 trades in one batch does **one** read, not 40 — directly fixing the C3 recompute problem.

### 6.9 Persistence policy

**Never write per tick.** At ~100 instruments and a few ticks/second each, per-tick writes would be thousands of rows/second and would destroy the shared SQLite.

| Data | Cadence | Rows/day |
|---|---|---|
| `mc_bars_1m` | Every 60s, batched single transaction | ~100 × 375 = 37,500 |
| `mc_futures` | Every snapshot (60s) | ~25 × 375 = 9,375 |
| `mc_breadth` | Every snapshot | 375 |
| `mc_sector` | Every snapshot | ~14 × 375 = 5,250 |
| `mc_vix` | Every snapshot | 375 |
| `mc_features` | Every snapshot | 375 |
| `mc_regime` | Every snapshot | 375 |

**~54k rows/day, ~10 MB/day.** Batched into ~2 transactions per minute. Trivial for SQLite in WAL mode, and isolated from `iv_history.db`.

---

## 7. Regime Engine

### 7.1 Multi-axis design (design change — needs your sign-off)

Your eleven states are not mutually exclusive. Forcing one label loses information and causes flip-flop. The engine therefore emits **four independent axes** and derives a composite label:

| Axis | States | Confidence weight | Justification |
|---|---|---|---|
| **Volatility** | LOW_VOL · NORMAL_VOL · HIGH_VOL · PANIC | **High (0.40)** | Vol is persistent and forecastable (§1.1) |
| **Positioning** | LONG_BUILDUP · SHORT_BUILDUP · SHORT_COVERING · LONG_LIQUIDATION · NEUTRAL | **Med-high (0.25)** | Mechanical from price×OI, not a forecast |
| **Trend** | TRENDING_UP · TRENDING_DOWN · RANGE · TRANSITIONING | **Low (0.20)** | Intraday direction is weakly forecastable |
| **Structure** | NONE · BREAKOUT · BREAKDOWN · REVERSAL | **Low (0.15)** | Event-like, highest false-positive rate |

Every one of your eleven requested states is reachable, and combinations that were previously inexpressible now are — e.g. `TRENDING_UP + PANIC + SHORT_COVERING` is a distinct and very actionable tape that a single label cannot represent.

**Composite label** by configurable precedence (default):
```python
COMPOSITE_PRECEDENCE = [
    ("PANIC",           "vol_state == 'PANIC'"),
    ("REVERSAL",        "structure_state == 'REVERSAL'"),
    ("SHORT_COVERING",  "positioning_state == 'SHORT_COVERING'"),
    ("LONG_LIQUIDATION","positioning_state == 'LONG_LIQUIDATION'"),
    ("BREAKOUT",        "structure_state == 'BREAKOUT'"),
    ("BREAKDOWN",       "structure_state == 'BREAKDOWN'"),
    ("HIGH_VOLATILITY", "vol_state == 'HIGH_VOL'"),
    ("TRENDING_UP",     "trend_state == 'TRENDING_UP'"),
    ("TRENDING_DOWN",   "trend_state == 'TRENDING_DOWN'"),
    ("LOW_VOLATILITY",  "vol_state == 'LOW_VOL'"),
    ("RANGE",           "True"),
]
```
Ordered list of `(label, predicate)`, evaluated top-down — **add or reorder states without touching code.**

### 7.2 Features per axis

**Trend** (`features/trend.py`)
```python
ef_ratio      = |P_t − P_{t−n}| / Σ|ΔP|                 # Kaufman, n = TREND_ER_LOOKBACK
ss_slope_pct  = (SS[-1] − SS[-1−k]) / P[-1] × 100       # reuse engine/regime._super_smoother
mom_z         = (P_t − P_{t−n}) / (rv_yz_short × √n)    # vol-scaled momentum, CTA-standard
vwap_position = (P_t − VWAP_session) / (day_high − day_low)
trend_score   = Σ wᵢ·normalise(featureᵢ)                # signed, −1..+1, weights configurable
```

**Volatility** (`features/volatility.py`)
```python
rv_yz_short = yang_zhang(bars_1m, VOL_RV_SHORT_BARS)    # default 30
rv_yz_long  = yang_zhang(bars_1m, VOL_RV_LONG_BARS)     # default 120
rv_ratio    = rv_yz_short / rv_yz_long                  # >1 expansion, <1 compression
vix_percentile = percentile(vix_ltp, vix_daily[−LOOKBACK:])
vol_of_vol  = stdev(vix_pct_changes[−N:])
vrp         = vix_ltp² − (rv_yz_long × ANNUALISATION)²  # §1.6
vol_score   = Σ wᵢ·normalise(featureᵢ)                  # 0..1
```

**Structure** (`features/structure.py`)
```python
range_position     = (P_t − day_low) / (day_high − day_low)
orb_state          = P_t vs first STRUCT_ORB_MINUTES range   # ABOVE|INSIDE|BELOW
prior_day_state    = P_t vs prior day H/L
breadth_divergence = sign(index_new_high) − sign(breadth_new_high)   # §1.9
```
`REVERSAL` requires **breadth divergence AND** a trend-score sign change **AND** a positioning flip — three independent confirmations, because reversal calls have the worst false-positive rate of any state.

**Positioning** (`features/positioning.py`)
```python
price_chg_pct = (ltp − prev_close) / prev_close × 100
oi_chg_pct    = (oi  − poi)        / poi        × 100

quadrant = LONG_BUILDUP     if price↑ and oi↑
           SHORT_BUILDUP    if price↓ and oi↑
           SHORT_COVERING   if price↑ and oi↓
           LONG_LIQUIDATION if price↓ and oi↓
           NEUTRAL          if |price_chg| < POS_MIN_PRICE_CHG_PCT
                             or |oi_chg|   < POS_MIN_OI_CHG_PCT
```
Deadbands are mandatory — without them a 0.01% drift produces a confident quadrant. Index-level state = OI-weighted blend of NIFTY + BANKNIFTY futures; a separate `stock_fut_long_pct` aggregates the Tier-4 stock futures.

### 7.3 Nothing is hardcoded

`market_context/config.py`, following your existing three-level override idiom exactly (settings-DB → env → default):

```python
def _f(name, default): ...      # float, env-overridable
def _i(name, default): ...
def _s(name, default): ...

# ── master mode ──────────────────────────────────────────────────────────
MODE = _s("MC_MODE", "off")            # off | observe | soft | hard

# ── trend axis ───────────────────────────────────────────────────────────
TREND_ER_LOOKBACK        = _i("MC_TREND_ER_LOOKBACK", 20)
TREND_ER_TRENDING_MIN    = _f("MC_TREND_ER_TRENDING_MIN", 0.35)
TREND_ER_RANGE_MAX       = _f("MC_TREND_ER_RANGE_MAX", 0.20)     # hysteresis gap
TREND_SCORE_UP_MIN       = _f("MC_TREND_SCORE_UP_MIN", 0.30)
TREND_SCORE_DOWN_MAX     = _f("MC_TREND_SCORE_DOWN_MAX", -0.30)
TREND_WEIGHTS = {
    "ef_ratio":      _f("MC_TREND_W_ER",    0.35),
    "ss_slope":      _f("MC_TREND_W_SLOPE", 0.30),
    "mom_z":         _f("MC_TREND_W_MOM",   0.20),
    "vwap_position": _f("MC_TREND_W_VWAP",  0.15),
}

# ── volatility axis ──────────────────────────────────────────────────────
VOL_RV_SHORT_BARS        = _i("MC_VOL_RV_SHORT_BARS", 30)
VOL_RV_LONG_BARS         = _i("MC_VOL_RV_LONG_BARS", 120)
VOL_PANIC_VIX_PCTILE     = _f("MC_VOL_PANIC_VIX_PCTILE", 95)
VOL_HIGH_VIX_PCTILE      = _f("MC_VOL_HIGH_VIX_PCTILE", 75)
VOL_HIGH_EXIT_PCTILE     = _f("MC_VOL_HIGH_EXIT_PCTILE", 60)     # hysteresis
VOL_LOW_VIX_PCTILE       = _f("MC_VOL_LOW_VIX_PCTILE", 25)
VOL_PANIC_RV_RATIO       = _f("MC_VOL_PANIC_RV_RATIO", 2.0)

# ── positioning axis ─────────────────────────────────────────────────────
POS_MIN_PRICE_CHG_PCT    = _f("MC_POS_MIN_PRICE_CHG_PCT", 0.15)
POS_MIN_OI_CHG_PCT       = _f("MC_POS_MIN_OI_CHG_PCT", 0.50)

# ── hysteresis / dwell ───────────────────────────────────────────────────
MIN_DWELL_MINUTES        = _i("MC_MIN_DWELL_MINUTES", 5)
CONFIRMATION_COUNT       = _i("MC_CONFIRMATION_COUNT", 2)
CONFIRMATION_WINDOW      = _i("MC_CONFIRMATION_WINDOW", 3)

# ── confidence ───────────────────────────────────────────────────────────
CONF_WEIGHTS = {
    "agreement":    _f("MC_CONF_W_AGREE",  0.35),
    "data_quality": _f("MC_CONF_W_DATA",   0.25),
    "dwell":        _f("MC_CONF_W_DWELL",  0.20),
    "margin":       _f("MC_CONF_W_MARGIN", 0.20),
}
AXIS_WEIGHTS = {
    "volatility":  _f("MC_AXIS_W_VOL",   0.40),
    "positioning": _f("MC_AXIS_W_POS",   0.25),
    "trend":       _f("MC_AXIS_W_TREND", 0.20),
    "structure":   _f("MC_AXIS_W_STRUCT",0.15),
}

CONFIG_VERSION = "mc-v1.0"      # bumped on any shape change → mc_features.config_hash
```

Every threshold is a percentile or a normalised ratio, **not a raw price/point value** — so nothing needs recalibration when NIFTY moves from 24,000 to 30,000. That is a deliberate, load-bearing choice.

### 7.4 Confidence score

```python
confidence = ( w_agree  × agreement
             + w_data   × data_quality
             + w_dwell  × dwell_maturity
             + w_margin × boundary_margin )
```

| Component | Definition | Rationale |
|---|---|---|
| `agreement` | Weighted fraction of axes whose implied direction agrees, using `AXIS_WEIGHTS` | Independent confirmation is the strongest evidence |
| `data_quality` | `1 − (missing_inputs / total_inputs)`, penalised by feed-gap recency and by `breadth.is_subsample` | Never be confident on partial data |
| `dwell_maturity` | `min(dwell_minutes / MIN_DWELL_MINUTES, 1.0)` | A 1-minute-old state is not yet evidence |
| `boundary_margin` | Normalised distance of the dominant score from its state threshold | A score at 0.301 vs a 0.300 threshold is a coin flip |

Confidence is **not** a probability of profit. It is a measure of *how well-identified the current state is*, and it is what lets a strategy scale involvement rather than treating every classification as equally certain.

### 7.5 Hysteresis, dwell, transitions

```python
def update(axis, raw_state, cfg, history):
    current = history.current_state(axis)
    if raw_state == current:
        history.extend_dwell(axis)
        return current
    # asymmetric exit band — must clear the EXIT threshold, not the ENTRY one
    if not clears_exit_band(axis, raw_state, current, cfg):
        return current
    # K-of-M confirmation
    recent = history.recent_raw(axis, cfg.CONFIRMATION_WINDOW)
    if recent.count(raw_state) < cfg.CONFIRMATION_COUNT:
        return "TRANSITIONING" if cfg.EMIT_TRANSITIONING else current
    # minimum dwell in the OLD state
    if history.dwell_minutes(axis) < cfg.MIN_DWELL_MINUTES:
        return current
    history.transition(axis, current, raw_state)
    return raw_state
```

**Strengthening / weakening** — objectives #2 and #3 — is the derivative of the *within-state* score, not a state change:

```python
momentum_of_state = (score_now − score_prev) / Δt          # over MOMENTUM_LOOKBACK_MIN

direction = STRENGTHENING  if momentum_of_state > +MOMENTUM_EPS  and sign matches state
            WEAKENING      if momentum_of_state < −MOMENTUM_EPS
            STABLE         otherwise
```

So `TRENDING_UP` + `WEAKENING` means: still an uptrend, losing force. **That is the single most actionable output the whole subsystem produces** — it is early, unlike a state flip, which is late by construction.

**Transition probability** — objective #4, without an HMM in v1:
```python
transition_prob = clip(
      p_base(current_state)                      # empirical, from mc_regime history
    + k_momentum × |momentum_of_state|
    + k_margin   × (1 − boundary_margin)
    + k_conf     × (1 − confidence), 0, 1)
```
`p_base` is bootstrapped from a config default and replaced by the **empirical** transition frequency once ≥`TRANSITION_MIN_HISTORY` observations exist in `mc_regime`. This is the clean seam where a fitted Markov model drops in later (§12.3) — the consumer contract never changes.

### 7.6 Mapping to your eight objectives

| # | Question | Field | Derivation |
|---|---|---|---|
| 1 | Current regime? | `composite_regime` + 4 axes | §7.1 |
| 2 | Strengthening? | `direction == "STRENGTHENING"` | §7.5 |
| 3 | Weakening? | `direction == "WEAKENING"` | §7.5 |
| 4 | Reversal developing? | `transition_prob` + `structure_state == "REVERSAL"` | §7.5 |
| 5 | Long premium aggressive? | `long_premium_bias == "AGGRESSIVE"` | rule below |
| 6 | Long premium defensive? | `long_premium_bias in ("DEFENSIVE","AVOID")` | rule below |
| 7 | Avoid option selling? | `short_premium_bias == "AVOID"` | rule below |
| 8 | Exit on context change? | `exit_warning` | §8 |

**Posture rules — fully configurable, defaults justified:**

```python
# Long premium wants: vol expanding, direction resolving, NOT pinned
long_premium_bias =
    AGGRESSIVE  if vol_state in (NORMAL_VOL, HIGH_VOL)
                and rv_ratio > LP_AGGR_RV_RATIO          # 1.15 — vol expanding
                and vrp < LP_AGGR_VRP_MAX                # options not over-priced
                and trend_state != RANGE
                and confidence >= LP_AGGR_MIN_CONF       # 0.60
    AVOID       if vol_state == LOW_VOL and trend_state == RANGE
                or vrp > LP_AVOID_VRP_MIN                # IV richly over realized
    DEFENSIVE   if direction == WEAKENING or confidence < LP_DEF_MAX_CONF
    NEUTRAL     otherwise

# Short premium wants: rich IV vs realized, stable vol, no gap risk
short_premium_bias =
    AVOID       if vol_state == PANIC
                or rv_ratio > SP_AVOID_RV_RATIO          # 1.5 — realized exploding
                or vrp < SP_AVOID_VRP_MIN                # premium not rich enough
                or iv_ts_slope < SP_AVOID_TS_SLOPE       # backwardation = stress
                or structure_state in (BREAKOUT, BREAKDOWN)
    FAVOURABLE  if vol_state in (LOW_VOL, NORMAL_VOL)
                and vrp > SP_FAVOUR_VRP_MIN
                and trend_state == RANGE
    NEUTRAL     otherwise
```

**Note the economic logic:** `long_premium` and `short_premium` are *not* mirror images. Long premium wants realized vol to **exceed** implied going forward; short premium wants implied to **exceed** realized. VRP appears in both with opposite signs, which is exactly right and is only possible because we now compute RV (§1.6).

---

## 8. Market Context Exit Engine

### 8.1 Principle

Exits fire on **market context deterioration**, not on an indicator crossing. Every trigger references a *change in the regime picture* since the position was opened.

**Prerequisite:** the position's entry context must be recorded. `paper_trades.factors_json` already exists and is populated for every trade — **add one key, `market_context`, containing the regime snapshot at entry.** No schema migration needed. This is the cheapest possible integration and it makes every exit decision auditable against what was true at entry.

### 8.2 Triggers

| Trigger | Condition | Level |
|---|---|---|
| `REGIME_FLIP` | Composite regime flipped to one contradicting the position side, `confidence ≥ EXIT_MIN_CONF` | **EXIT** |
| `VOL_PANIC` | `vol_state → PANIC` and position is **short** premium | **EXIT** |
| `POSITIONING_FLIP` | LONG_BUILDUP → LONG_LIQUIDATION (CE) or SHORT_BUILDUP → SHORT_COVERING (PE) | **REDUCE** |
| `TREND_DECAY` | `trend_state` unchanged but `direction == WEAKENING` for ≥`EXIT_DECAY_BARS` consecutive snapshots | **REDUCE** |
| `REVERSAL_FORMING` | `transition_prob > EXIT_TRANSITION_PROB` **and** `structure_state == REVERSAL` | **WATCH** |
| `CONFIDENCE_COLLAPSE` | Entry confidence ≥ 0.6 and current < `EXIT_CONF_COLLAPSE` | **WATCH** |
| `CONTEXT_STALE` | Feed gap > `EXIT_STALE_SEC` — context unknown, not benign | **WATCH** |

**Deliberate asymmetry:** `VOL_PANIC` is an EXIT for short premium and explicitly **not** an exit for long premium — a panic is precisely when a long option is doing its job. An engine that flattens everything on a vol spike destroys the convexity you paid for. This is the kind of distinction indicator-based exits cannot make.

### 8.3 Confidence score for an exit

```python
exit_score = ( w_sev   × trigger_severity        # EXIT 1.0 / REDUCE 0.6 / WATCH 0.3
             + w_conf  × regime_confidence       # confident about the deterioration?
             + w_delta × context_delta           # how far context moved since entry
             + w_agree × triggers_agreeing )     # multiple independent triggers
level = EXIT if exit_score >= EXIT_THRESHOLD_EXIT else \
        REDUCE if exit_score >= EXIT_THRESHOLD_REDUCE else \
        WATCH if exit_score >= EXIT_THRESHOLD_WATCH else NONE
```

`context_delta` is the normalised distance between the entry regime vector and the current one — a continuous "how much has the world changed" measure rather than a binary flip test.

### 8.4 Integration — reuses your existing pattern exactly

Add `_auto_exit_on_context()` to `OrderManager`, structurally identical to the existing `_auto_exit_on_oi_contradiction()` and `_auto_exit_on_sonar_reversal()`:

- Same `off / soft / hard` mode idiom (`MC_EXIT_MODE`, default **`off`**).
- Same fail-open + `_alert_gate_failure()` behaviour.
- Same `_close_trade_and_partner()` call, so a combo is never split.
- Same `MAX_PROFIT_PCT` escape hatch — never dump a clear winner on context alone.
- Called from `track()` alongside the existing auto-exits.

**Zero new architecture.** One more sibling in a pattern you already have three instances of.

### 8.5 ⚠ Recommended sequencing, per C5

Given 62.4% of exits are already time-based and frictions are 75% of losses:

1. Ship `MC_EXIT_MODE=off`. Log `mc_exit_signals` **anyway** (the engine runs, records what it *would* do, acts on nothing).
2. After ≥20 sessions, join `mc_exit_signals` against actual trade outcomes: **would acting have helped?** You now have the counterfactual, which is exactly what `scan_log` does for entries.
3. Only then consider `soft`, then `hard`.

**Prediction, stated in advance so it can be checked:** context exits will fire most often on trades that would have closed at `Time 15:20` near flat, converting ~₹0 outcomes into ~−₹188 friction outcomes. The `WATCH`/`REDUCE` levels exist so you can act on the severe tail only. If the measurement contradicts me, act on it — that is the point of measuring.

---

## 9. Public API and Integration

### 9.1 The contract

```python
# market_context/contracts.py
@dataclass(frozen=True)
class BreadthContext:
    adv_dec_pct: float | None
    volume_breadth_pct: float | None
    thrust: float | None
    universe_size: int
    is_subsample: bool            # ← honesty flag, see §6.3
    sample_quality: float

@dataclass(frozen=True)
class VixContext:
    level: float | None
    percentile: float | None
    state: str                    # LOW|NORMAL|HIGH|PANIC
    chg_pct: float | None
    vol_of_vol: float | None
    is_intraday: bool             # False ⇒ fell back to yesterday's vix_daily

@dataclass(frozen=True)
class FuturesContext:
    nifty_quadrant: str
    banknifty_quadrant: str
    nifty_basis_ann: float | None
    banknifty_basis_ann: float | None
    stock_fut_long_pct: float | None

@dataclass(frozen=True)
class SectorContext:
    strongest: list[tuple[str, float]]
    weakest: list[tuple[str, float]]
    dispersion: float | None
    implied_corr_proxy: float | None
    def rs_for(self, symbol: str) -> float | None: ...

@dataclass(frozen=True)
class ExitWarning:
    level: str                    # NONE|WATCH|REDUCE|EXIT
    score: float
    triggers: tuple[str, ...]
    reasons: tuple[str, ...]

@dataclass(frozen=True)
class MarketContext:
    available: bool               # False ⇒ everything below is neutral
    as_of: str | None
    age_seconds: float | None

    regime: str                   # composite label
    trend_state: str
    vol_state: str
    structure_state: str
    positioning_state: str

    confidence: float             # 0..1
    trend_strength: float         # signed −1..+1
    direction: str                # STRENGTHENING|WEAKENING|STABLE
    transition_prob: float
    dwell_minutes: int

    breadth: BreadthContext
    vix: VixContext
    sector: SectorContext
    futures: FuturesContext

    long_premium_bias: str
    short_premium_bias: str
    size_multiplier: float

    exit_warning: ExitWarning
    reasons: tuple[str, ...]

    # convenience predicates — keep call sites readable
    def favours(self, side: str) -> bool: ...
    def blocks_long_premium(self) -> bool: ...
    def blocks_short_premium(self) -> bool: ...
```

### 9.2 `get()` — the only entry point

```python
# market_context/__init__.py
_cache: MarketContext | None = None
_cache_at: float = 0.0

def get(max_age_seconds: float | None = None) -> MarketContext:
    """Latest market context. Never raises. Never calls a broker.

    Reads the newest mc_regime + mc_features row from market_context.db,
    process-cached for GET_CACHE_TTL_SEC. On ANY failure — missing DB,
    stale row, malformed data, subsystem off — returns NEUTRAL_CONTEXT
    with available=False, so no caller can be broken by this subsystem.
    """
```

**Guarantees consumers can rely on:**
1. Never raises.
2. Never blocks on I/O beyond one indexed SQLite read.
3. Never calls a broker API.
4. Returns `available=False` rather than stale data past `MAX_CONTEXT_AGE_SEC` (default 300).
5. `NEUTRAL_CONTEXT` is designed so that **every gate that consumes it passes** — fail-open, matching your existing convention.

### 9.3 Integration points (ordered, low → high risk)

| # | Site | Change | Risk |
|---|---|---|---|
| 1 | `paper_trader.collect_factor_snapshot()` | Add `snap["market_context"] = ctx.as_dict()` | **None** — additive; starts the observation record immediately |
| 2 | `breadth.compute()` | Delegate to `market_context.get().breadth` when available, else current logic | Low — fixes the N× rescan (C3) |
| 3 | `engine/regime.py::load()` | Return a `RegimeState` built from `market_context.get()` | Low — kills the duplicate engine (C1), fixes stale VIX (C2) |
| 4 | `order_manager._apply_breadth_gate()` | Read from context instead of recomputing | Low |
| 5 | `order_manager._apply_context_gate()` | **New** gate: block entries when `long_premium_bias == AVOID`. `off/soft/hard`, default `off` | Medium — this is the entry suppression of C5 |
| 6 | `order_manager._auto_exit_on_context()` | **New**, default `off` | Medium |
| 7 | `paper_trader.book_signal()` | Honour `size_multiplier == 0` as a skip | Medium — requires the C6 decision |
| 8 | `dashboard_app.py` | Regime panel: current state, confidence, axes, history strip | None |

**Site 1 is the most important and the least risky.** It costs nothing, breaks nothing, and starts accumulating the entry-context record that every later measurement depends on. **Ship it in Phase 1 even though nothing consumes it yet.**

### 9.4 What strategies stop doing

| Before | After |
|---|---|
| `breadth.compute()` per trade — full table scan | `market_context.get().breadth` — cached read |
| `engine/regime.py` — private classifier, stale VIX | `market_context.get()` — shared, intraday VIX |
| Each container computing index trend independently | One computation, shared |
| No futures view anywhere | `ctx.futures` |
| VIX only at EOD | `ctx.vix.level` live |

---

## 10. Service Definition

```yaml
  # ── Service 17: Market Context ────────────────────────────────────────────
  # Owns the ONLY WebSocket connection. Sole writer of mc_* tables.
  # Every strategy reads via market_context.get() — zero broker calls.
  market-context:
    build:
      context: .
      dockerfile: Dockerfile.market-context
    container_name: market-context
    restart: unless-stopped
    env_file: .env
    environment:
      TZ: Asia/Kolkata
      APP_TIMEZONE: Asia/Kolkata
      MC_MODE: observe            # off | observe | soft | hard
    volumes:
      - .:/app
      - ./data:/app/data
      - ./logs:/app/logs
    depends_on:
      - iv-collector              # needs the liquid-universe ranking
    command: python -m market_context.service
```

**Logging — learning from a defect found in the strategy spec.** `logs/scheduler.log` has been 0 bytes since 2026-03-31 despite the discount service booking 356 trades. `market_context.service` will therefore:
- Configure a `FileHandler` on a **named** logger (`market_context`), not the root logger — avoiding the `directional_iv.log` cross-contamination problem.
- Verify at startup that the log file is writable and **emit a Telegram alert if it is not**.
- Log a one-line heartbeat every `HEARTBEAT_LOG_MINUTES` (default 15) with connection state, frames/sec, current regime, confidence — so silent death is visible.

**New dependency:** `upstox-python-sdk` is already in `requirements.txt`. Note your recorded finding that *the Upstox SDK hangs (use raw REST)* — that observation was about REST calls. **Phase 1 must validate `MarketDataStreamerV3` specifically.** If it exhibits the same hang, fall back to `websockets` + the published protobuf schema. Budget for this; it is the main technical risk (§13).

---

## 11. Rollout Phases

Each phase is independently shippable and independently revertible.

| Phase | Deliverable | Consumers affected | Risk |
|---|---|---|---|
| **0** | `config.py`, `contracts.py`, `store.py` schema, `get()` returning `NEUTRAL_CONTEXT`. Integration site 1 only | None | **Zero** |
| **1** | WS client, Tier-1+2 subscription, `mc_bars_1m`, `mc_vix`, `mc_futures`, gap register. **No regime engine yet** | None — data only | Low |
| **2** | Feature layer: trend, volatility (YZ RV), positioning, breadth. `mc_features` populated | None | Low |
| **3** | Regime engine, `MC_MODE=observe`. Writes `mc_regime`. Dashboard panel. `engine/regime.py` → adapter | Convex only | Low |
| **4** | VRP, term structure, dispersion, volume breadth. Entry gate in `soft` | Logged only | Low |
| **5** | Exit engine in `off` (logs `mc_exit_signals`, acts on nothing) | None | Low |
| **6** | **Measurement checkpoint.** ≥20 sessions. Answer: does context predict outcomes? | — | — |
| **7** | Selectively enable `hard` on whichever gate the Phase-6 data supports | Live gating | Medium |

**Phase 6 is a gate, not a formality.** Your stated principle is that nothing graduates without demonstrated expectancy. This subsystem should be held to the same bar it is meant to enforce.

---

## 12. Future Scalability

### 12.1 Multi-connection scaling
`FeedClient` is written to own **one** socket; a `FeedPool` manages N with instruments partitioned by tier. Moving from 1 → 5 connections (Plus plan) becomes a config change, not a rewrite. Tier 3 expands to the full ~200-name universe, eliminating the subsample caveat.

### 12.2 Per-symbol context
v1 is market-level. The schema already supports per-symbol extension (`mc_sector` is keyed by `(ts, sector)`). A future `mc_symbol_context` keyed `(ts, symbol)` slots in with no contract change — `get()` gains an optional `symbol=` argument, and existing callers are unaffected.

### 12.3 Fitted regime models
`transition.py` isolates `p_base()` behind a function boundary. Replacing rule-based transition probability with a fitted HMM or a gradient-boosted classifier trained on `mc_features` → forward outcomes touches **one function**. By then you will have months of point-in-time feature vectors — the training set the current system cannot produce.

### 12.4 Backtest integration
`research/replay.py` reconstructs `MarketContext` for any historical timestamp from `mc_features` + a config hash. Your `backtest/engine.py` gains regime-conditional analysis — *"what is this strategy's expectancy in HIGH_VOL + SHORT_COVERING?"* — which is the question your platform exists to answer and currently cannot.

### 12.5 Live execution readiness
When `AUTO_EXECUTE` eventually goes true, `size_multiplier` and `exit_warning` are already the natural pre-trade risk layer. No new component required.

### 12.6 Additional assets
Adding MCX or currency = adding rows to `mc_instruments` plus a sector-map entry. No code change in the feature or regime layers, because every threshold is a percentile or normalised ratio (§7.3).

---

## 13. Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | **`MarketDataStreamerV3` hangs like the REST SDK** | Medium | High | Phase 1 spike validates it first. Fallback: `websockets` + protobuf. Budget 2–3 days |
| 2 | **Subscription cap tighter than assumed** | Medium | Medium | Budget-aware planner degrades gracefully; `is_subsample` surfaces it. **Needs your plan-tier answer** |
| 3 | Token expiry mid-session kills the socket | Medium | Medium | Own the reconnect loop, re-auth via existing `upstox_token_manager` |
| 4 | SQLite contention | Low | Medium | Separate DB file; batched writes; WAL |
| 5 | Regime whipsaw | Medium | Medium | Hysteresis + dwell + confirmation (§7.5). Phase 3 observe-only proves stability first |
| 6 | Context gates reduce trade count below statistical usefulness | Medium | Medium | Start `soft`; `scan_log` records every would-be block, so the counterfactual survives |
| 7 | Exit engine increases friction (C5) | **High** | Medium | Ship `off`, measure, gate on Phase 6 |
| 8 | Adds a new silent-failure surface | Medium | Medium | Heartbeat logging, named logger, writability check, gap register |

---

## 14. Open Questions — I need answers on these three

1. **Upstox plan tier.** Standard or Plus? Determines 1 vs 5 connections and therefore whether breadth is a 59-name subsample or the full universe. **Blocks Phase 1 sizing.**

2. **Multi-axis regime (§7.1) — approved?** It is a deliberate deviation from the single-label brief. I believe it is strictly better and I have justified why, but it changes the shape of `get()`, so I want explicit sign-off before building.

3. **Position sizing (C6).** Do you want `size_multiplier` wired into `book_signal` in Phase 7, or should context stay a binary gate? This is the difference between context modulating risk and merely permitting it.

---

## Appendix A — Summary of Challenges to Current Architecture

| # | Finding | Evidence | Resolution |
|---|---|---|---|
| C1 | Duplicate regime engine | `engine/regime.py` | Promote to `market_context`; `engine/regime.py` → adapter |
| C2 | VIX is EOD-only; intraday decisions use yesterday's close | `vix_daily ORDER BY date DESC LIMIT 1` | Stream `NSE_INDEX\|India VIX` |
| C3 | Breadth is an IV-collector artifact; full rescan per trade | `breadth.compute()` in `collect_factor_snapshot()` | Dedicated collector + cached `get()` |
| C4 | No futures data at all | No futures reference in repo | New `collect/futures.py` |
| C5 | Exit engine may worsen friction | 62.4% time exits; frictions = 75% of loss | Sequence entry-gating first; exits ship `off` |
| C6 | Sizing is flat 1-lot, so `size_multiplier` is unusable | `paper_trader`; `SIZE_MULT` never reaches paper path | Return it; wiring is your decision |
| C7 | Tick feed will outrun the 5-min fill model | `apply_tick()` docstring | Context cadence ≠ paper cadence; keep 5 min |
| C8 | `logs/scheduler.log` 0 bytes since 2026-03-31 | Filesystem | Named logger + startup writability check + alert |

---

*End of plan. Nothing implemented. Awaiting decisions on §14.*
