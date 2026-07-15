# Design Review: ARCHITECTURE_GREENFIELD.md

**Reviewer role:** Principal Architect / Quant Researcher / CTO
**Verdict up front:** Conditionally approved — approved as a Phase 1–3 plan, **rejected as a 10-year architecture** until the Critical items in §11 are addressed. Score: **6.5/10**.

---

## 1. Overall Architecture

**Is it sound?** As a skeleton, yes. Event log as spine, pure strategies, injected clock, single risk choke point, backtest-as-replay, promotion pipeline — these are the correct five ideas and most retail platforms never get any of them. The document's real achievement is making backtest/live parity structural instead of aspirational.

**Would I approve it?** For the next 18 months of build, yes. For 10 years of evolution and real capital, no — it has three load-bearing assumptions that will fail, and two entire layers missing.

**Biggest strengths:** (1) Everything derives from one immutable log — audit, replay, and rebuild come free. (2) Strategy purity + Clock injection — the single most valuable discipline in the doc. (3) One risk kernel between intent and order. (4) Promotion as a state machine with logged transitions. (5) Honest non-goals — refusing Kafka/k8s at this scale is correct.

**Biggest weaknesses, each with consequence and fix:**

**W1 — SQLite is being asked to be a message bus, and it isn't one.** Three writer processes plus N read-only consumers over one WAL file. WAL permits exactly one writer at a time; your "three writers with disjoint tables" still serialize on the same file lock. Consumers have no subscription mechanism — the doc silently implies polling, but never defines consumer cursors, delivery semantics, or backpressure. *Consequence:* the day you move from 1-minute polling sweeps to websocket ticks (the doc's own "escape hatch"), the spine melts: writer contention, poll latency in the decision path, dashboard queries stalling the engine. *Institutional solution:* an ordered, append-only log with real consumer semantics (Kafka/Redpanda), or a single-writer sequencer process that owns all appends (LMAX-style). *Pragmatic alternative:* keep SQLite but (a) make the event store a **single daemon** that owns the file — other processes append via a local socket, eliminating multi-writer WAL contention; (b) define explicit `consumer_offsets(consumer, last_seq)` tables and a documented poll contract; (c) split hot (`events.db`) from analytics (dashboard reads a replica synced every N seconds). *Complexity:* moderate — one new small process. *Trade-off:* a socket hop in the write path; worth it, because it also makes the Postgres migration a swap of one daemon's internals instead of touching every process.

**W2 — "Bit-for-bit" golden replay is oversold and will rot.** Byte-identical decision events across commits requires: strict single-threaded deterministic engine scheduling, stable float behavior across numpy/BLAS versions, canonical JSON serialization, no wall-clock leakage anywhere. One dependency bump breaks it; after the third false failure the team (you) marks it flaky and the crown jewel becomes decoration. *Institutional solution:* deterministic simulation cores are built as such from day one (single-threaded event loop, integer/fixed-point arithmetic for money, canonical serialization) and regression is *tolerance-based with versioned baselines* — semantic comparison of decisions (same instruments, same directions, sizes within ε, same veto reasons), with an explicit "re-baseline" ceremony when behavior changes intentionally. *Fix:* demand determinism of *decision semantics*, not bytes; store money as integer paise; make the engine loop single-threaded and sequence-driven. *Complexity:* low. *Trade-off:* semantic diffing is more code than `sha256`, but it's the version that survives.

**W3 — Backtest-as-replay is only as good as data you recorded, and you have none.** Phase 1 starts recording; therefore for the first 1–2 years every backtest runs on a few months of self-recorded data. NSE options history is the hard part the document skips entirely: acquiring deep chain history, lot-size changes, F&O ban entries/exits, strike-scheme changes, expiry-calendar changes (weekly expiry day moved twice in recent years), symbol renames, corporate actions. *Consequence:* walk-forward validation on 4 months of data is statistically meaningless; the promotion pipeline's thresholds become theater. *Institutional solution:* market-data engineering is a first-class team: golden sources, point-in-time reference data, data-quality gates before anything reaches research. *Fix:* add a **Historical Data Acquisition & Quality layer** (see §2) and treat purchased/scraped history as a supported ingestion path into the same lake schema, with a `data_source` and `quality_tier` on every row. *Complexity:* high — this is genuinely tedious. *Trade-off:* none; without it the research plane is a simulator of the last quarter.

**W4 — The fill simulator decides whether the whole promotion pipeline means anything, and it gets one clause.** "Configurable slippage" is fantasy for NSE stock options where spreads are routinely 1–5% of premium and top-of-book depth is thin. If paper fills are optimistic, "live-vs-backtest tracking error < ε" passes strategies that die on contact with real spreads. *Institutional solution:* execution simulators are calibrated continuously from real fills (spread-crossing models, size-dependent impact, latency distributions) and simulator error is itself a monitored metric. *Fix:* record L1 bid/ask at decision time in every `intent` event; fill at touch-adjusted prices; once live, run a **fill-model calibration job** that regresses realized slippage on spread/size/time-of-day and feeds coefficients back to the simulator. *Complexity:* moderate. *Trade-off:* needs live fills to calibrate — bootstrap with conservative (pessimistic) defaults, never optimistic ones.

**W5 — Reproducibility stamps are incomplete.** Config hash on events is necessary but insufficient: identical config + different code = different decisions. *Fix:* every decision event carries `(config_hash, code_version, feature_set_version, data_snapshot_id)`. *Complexity:* trivial. Do it before the first event is ever written — retrofitting lineage is archaeology.

**W6 — Single box, real capital, no failure story.** Compose on one machine; the doc's recovery narrative is "rebuild from the log." What happens when the box dies with an open position at 13:40? The broker-resting SL-M leg is the only protection, and reconciliation-on-restart gets one sentence. *Institutional solution:* hot/warm failover, journaled recovery drills, exchange-side protective orders. *Pragmatic fix:* (a) broker-side SL is mandatory and verified (poll that it exists — alarm if not); (b) a dead-man's switch: a tiny independent watchdog (can be a phone alert via a cron on a second machine/VPS) that alarms if the engine heartbeat stops during market hours; (c) a written, *rehearsed* recovery runbook: restart → rebuild book from log → reconcile vs broker → operator confirms before re-arming. *Complexity:* low. *Trade-off:* none acceptable to skip when live.

**Scalability problems in years 3–5:** event volume (feature events at every tick × every instrument will dominate the log — see §6 for the fix), single-file SQLite as both OLTP and dashboard source, replay wall-time (replaying 3 years of events through a Python engine single-threaded — you'll need partitioned parallel replay by day, which conflicts with cross-day state unless day-boundaries are explicit snapshots; design day-snapshot checkpoints now).

**Dangerous assumptions:** that recorded data ≈ historical truth (W3); that paper fills ≈ live fills (W4); that walk-forward on small samples controls overfitting (it doesn't — §3); that "conviction score 0–100" means anything without calibration (§4); that three processes on one box is fine at live stage (W6); that JSON payloads with an integer `schema_ver` constitutes schema governance (ten years of events needs a schema registry file in-repo, compatibility rules, and migration tooling — cheap now, brutal later).

**Overengineered:** byte-identical replay (W2); event-sourcing every computed feature (volume bomb — store feature *inputs* and recompute deterministically, persist only snapshots/hashes for verification); possibly the `evidence.emitted` event granularity at tick frequency.

**Underengineered:** data quality/history (W3), fill realism (W4), portfolio construction (§8), the statistics of promotion (§3), schema governance, ops/alerting (process-death, feed-staleness, clock-skew monitoring get zero design), and the entire learning loop (§5) — the doc describes a system that executes research conclusions but has no machinery to produce them.

---

## 2. Missing Components

The document has four planes. A 10-year platform needs these additional first-class components (several are consolidated into the Research plane in §3):

**M1 — Market Data Quality & History Engine.** *Why:* garbage in, confident garbage out; and you need history you didn't record (W3). *Responsibilities:* ingest purchased/scraped history into lake schema; point-in-time reference data (lot sizes, ban lists, expiry calendars, strike schemes, F&O universe membership by date); quality gates (gap detection, stale-quote detection, IV outliers, crossed markets, volume spikes vs bhav reconciliation); quarantine bad data with `quality_tier`. *Inputs:* raw vendor/exchange files, live md events, bhavcopy. *Outputs:* certified lake partitions, data-quality events/alerts, point-in-time universe API. *Interactions:* research refuses uncertified data; the engine's stale-data guard consumes its live checks.

**M2 — Outcome & Label Store.** *Why:* every learning question in §5 requires labeled outcomes, and nobody builds this by accident. *Responsibilities:* for every intent (taken or vetoed) and every fill, compute and store outcomes: MAE/MFE, realized R, time-to-target/stop, post-veto counterfactual ("what would that vetoed trade have done?"), regime at entry, features at entry (already in the log — this joins them). *Inputs:* event log, market data. *Outputs:* one wide, versioned `outcomes` table — the training/analysis substrate for everything in §3–§5. *Interactions:* Research, Learning, and Convex calibration all read only this.

**M3 — Portfolio Construction layer.** Missing entirely; the risk kernel sizes trades one intent at a time and never sees the portfolio question. See §8.

**M4 — Strategy Lifecycle Manager.** The promotion pipeline is described as a diagram, not a component. *Responsibilities:* own the state machine; evaluate promotion/demotion criteria on schedule; enforce that live strategies carry a risk budget assigned by Portfolio Construction; execute automatic demotion; require human sign-off events for promotion. *Inputs:* Research Engine reports, decay monitors. *Outputs:* `strategy.promoted/demoted` events, risk-budget assignments. *Interactions:* the fusion layer refuses intents from strategies not in `live` state — enforcement in code, not convention.

**M5 — Regime Engine (promoted from "a feature").** *Why:* regime is the conditioning variable for every analysis in §3 and §5; if it's just another feature, its definition drifts and historical regime labels silently change. *Responsibilities:* versioned regime classification (trend/chop × vol level × breadth state), point-in-time labels persisted to the lake, regime-change events. *Inputs:* index candles, VIX, breadth. *Outputs:* `regime` labels with `regime_model_version`. *Interactions:* Convex conditions on it; Research stratifies by it; Lifecycle Manager uses regime-conditional performance.

**M6 — Fill-Model Calibration service** (per W4). Inputs: live fills + recorded L1. Outputs: simulator coefficients, simulator-error report.

**M7 — Model & Feature Registry** (see §6, §7). Versioned definitions of features, detectors, fusion models, regime models — with the same rigor the doc applies to strategy params.

**M8 — Ops/Watchdog layer.** Heartbeats, feed-staleness alarms, broker-connectivity checks, dead-man's switch, disk/backup monitoring. One small process plus one external check. Unsexy; mandatory before live.

A "Recommendation Engine" and "Learning Engine" from your list are real — I fold them into §3/§5 rather than making them separate services. A standalone "Knowledge Graph" I push back on in §9.

---

## 3. Research Engine

Agreed — this is the heart, and the greenfield doc under-designed it (one section, no data model, no jobs). Design:

**Responsibilities.** Turn the event log + lake into decisions about *what should trade tomorrow*: performance attribution, decay detection, discovery of profitable feature/strategy/regime combinations, parameter optimization, and ranked recommendations for the Lifecycle Manager and Portfolio Construction. It is **batch, offline, and advisory** — it never touches the live path directly.

**Architecture.** A DAG of batch jobs (a plain scheduler + DuckDB over Parquet is sufficient; do not import Airflow) reading three substrates: the lake (certified market data + features), the Outcome Store (M2), and the Experiment Registry (§7). All outputs are written back as versioned tables + rendered reports; recommendations are structured objects, not prose.

**Data model (core tables).** `outcomes` (one row per intent: strategy_ver, params_ver, feature vector ref, regime, conviction, taken/vetoed+reason, entry/exit, realized_R, MAE/MFE, slippage vs sim); `experiments` and `trials` (§7); `strategy_ledger` (daily per-strategy per-mode P&L, turnover, costs); `regime_calendar` (M5); `recommendations` (id, type, evidence refs, proposed change, status: proposed/approved/rejected/expired); `decay_monitors` (per-strategy rolling stats vs backtest reference distribution).

**Daily jobs (post-close):** ingest + certify the day's data; compute outcomes for today's intents and resolve matured counterfactuals; update decay monitors; slippage attribution (sim vs realized, by instrument/spread bucket); veto histogram (which gates fired, what the vetoed trades would have returned); daily digest.

**Weekly jobs:** live-vs-backtest tracking error per live strategy; regime-conditional performance refresh; fill-model recalibration (M6); feature-health report (coverage, staleness, distribution drift vs training window); parameter-sensitivity spot checks around current live params.

**Monthly jobs:** full walk-forward re-runs of all live/paper strategies on the extended dataset; discovery sweeps (below); portfolio review inputs for §8 (correlation matrix of strategy daily returns, capital-efficiency ranking); recalibration of the Convex model (§4); recommendation generation + expiry of stale recommendations.

**Statistics to generate.** Per strategy × regime × mode: profit factor, hit rate, avg win/loss R, expectancy, max DD, time-under-water, tail metrics (worst-5 trades' share of P&L), **deflated Sharpe ratio and Probabilistic Sharpe Ratio** (small samples — plain Sharpe will lie to you), turnover and cost drag, MAE/MFE distributions (exit-quality evidence), slippage vs simulation, conviction-score calibration curves (predicted vs realized win rate by score decile — §4), and *sample-size adequacy flags* on every cell (a PF of 2.1 on 14 trades gets rendered with its confidence interval or not at all).

**Discovering profitable combinations.** This is where retail systems destroy themselves via multiple testing. Rules: (1) hypotheses are registered *before* evaluation (an experiment record with a stated hypothesis — §7); (2) combinatorial sweeps (feature × regime × time-of-day × strike-offset) are allowed but their results pass through **FDR control (Benjamini–Hochberg) or White's Reality Check / SPA** before anything is called a finding; (3) any finding must survive on a held-out time period never used in the sweep; (4) findings become *candidate strategies* entering the pipeline at `research`, never shortcuts to live. Institutional reality: most "discoveries" are noise; the machinery's job is to say no cheaply.

**Decay detection.** Maintain, per live strategy, the backtest/paper reference distribution of rolling-20-trade expectancy. Monitor live expectancy with **CUSUM** (drift detection) and a simple SPRT against "edge = 0". Two thresholds: *warning* (alert + halve risk budget) and *demote* (automatic, to paper). Also monitor the inputs, not just P&L: feature-distribution drift and regime occupancy — a strategy can be "fine" while its regime has simply stopped occurring, which is a portfolio problem, not decay.

**Parameter optimization.** Walk-forward only (anchored or rolling IS windows, adjacent OOS scoring). Search with coarse grids or Bayesian optimization (Optuna is fine) — but the *selection criterion is parameter-plateau quality, not peak OOS score*: prefer the center of a flat neighborhood of good parameters over a sharp spike (spikes are overfit). Report sensitivity heatmaps. Optimized params become a `recommendation`, never a direct write.

**Recommendation generation.** Every recommendation is a typed object: `{type: promote|demote|reparam|reallocate|retire-feature, target, proposed_change, evidence: [report refs, stats], expected_impact, confidence, expires_at}`. The human approves via a logged event. See §5 for the automation boundary.

**Promotion/demotion — concrete criteria (tune the numbers, keep the shape):** research→backtest-approved: walk-forward positive expectancy after costs in ≥70% of OOS windows, deflated Sharpe > 0, plateau (not spike) parameters, minimum ~80–100 OOS trades. backtest-approved→paper: automatic. paper→live-eligible: ≥8 weeks paper, ≥40 trades, live-vs-sim tracking error within bounds, slippage within 1.5× modeled. live-eligible→live: human arm + risk budget from §8. Demotion: CUSUM breach, DD > 1.5× backtest max DD, or tracking error blowout — **automatic, no human required to reduce risk.**

---

## 4. Convex Engine (fusion → intelligence layer)

The greenfield doc's fusion layer is a placeholder: "converts evidence into conviction scores" with no math. Uncalibrated hand-weighted scores are astrology with extra steps. Redesign:

**Answer: hybrid, staged — and calibration is the non-negotiable part, not the model class.**

**Stage A (day one): calibrated linear scoring.** Evidence vector → logistic regression → probability. This *is* weighted scoring, but the weights are fit on the Outcome Store instead of invented, and the output is a probability, not a vibe. Trains on hundreds of samples, fully explainable (each evidence term's contribution is `weight × value`), cheap to retrain monthly.

**Stage B (year 2+, when outcomes ≳ 2–3k):** gradient-boosted trees over the same vector, regime-interaction terms included, wrapped in **isotonic/Platt calibration** so predicted probabilities match realized frequencies. Keep Stage A running in shadow as the fallback and sanity check. Never deploy a model whose calibration curve hasn't been reviewed.

**Inputs:** the frozen `FeatureFrame` (versioned), active Evidence set (each: source, direction, strength, age/ttl), regime label + regime-model version, portfolio context (existing exposure to symbol/sector), and strategy identity (fusion output is *per candidate intent*, conditioning on which strategy proposed it).

**Outputs:** `Conviction = {p_win: calibrated, expected_R: p·avgWin − (1−p)·avgLoss from regime-conditional history, uncertainty: sample-size-driven interval, contributions: [(evidence_id, signed contribution)], model_version}`. Downstream, Portfolio Construction sizes on expected_R and uncertainty; the risk kernel enforces limits.

**Conflict resolution.** Three explicit mechanisms, in order: (1) **Vetoes are not negative evidence** — a sonar regime-contradiction or breadth failure is a hard policy gate *before* scoring; never let a strong bullish OI signal "outvote" a structural veto by weight. (2) Opposing soft evidence enters the model with sign; the model learns whether, e.g., OI-buildup contra momentum historically kills the trade or merely dampens it — this is precisely what hand-weights can't do. (3) An **abstain band**: if p_win ∈ [0.45, 0.55] or uncertainty is wide (few similar historical samples), emit no intent. Not trading is an output.

**Explainability.** Every scored intent logs its contribution vector (Stage A: weights×values; Stage B: SHAP values). The dashboard renders "this trade scored 0.63 because: regime-aligned +0.09, IV-rank low +0.05, OI contra −0.04…". This is also your debugging tool when calibration drifts.

**Calibration & confidence over time.** Monthly job (§3): reliability diagram by score decile, Brier score trend, per-evidence-source contribution drift. Small-sample shrinkage: new evidence sources enter with their weight shrunk toward zero until they've accumulated N outcomes (a spike-and-slab-ish prior in practice: don't let 12 lucky trades give delivery-surge a dominant weight). Retrain cadence monthly; model versions are registry entries (§7) and every `conviction.scored` event carries the model version — so replay reproduces historical decisions with the historical model, not today's.

---

## 5. Learning Engine

Not a separate service — a **discipline built on §3's machinery** plus an automation policy. "Platform gets smarter every month" = the monthly research cycle closes the loop from outcomes back into configuration, with a human valve in the risk-increasing direction.

**How each question gets answered (all from the Outcome Store, all regime-stratified, all with sample-size gates):**
Which features work → per-evidence-source contribution and ablation analysis (re-run fusion with the source zeroed; measure expectancy delta). Which combinations → the FDR-controlled discovery sweeps of §3. Which symbols → per-symbol expectancy with shrinkage toward the universe mean (never trust a single stock's 9-trade record); output is universe *tiers*, not stock-picking. Which strikes → realized R by moneyness bucket × days-to-expiry × IV-rank — this is one of the highest-value analyses for an options buyer and it's nearly free given the log. Which exits → MAE/MFE analysis: distribution of max-favorable-excursion on losers (were targets too far?) and max-adverse on winners (were stops too tight?); compare realized exits vs oracle exits. Which entry timing → expectancy by time-of-day bucket per strategy. Which regimes → the regime-conditional ledger. What's decaying → §3's CUSUM monitors. Which parameters → walk-forward proposals with plateau analysis.

**Recommendation production.** The monthly run emits ranked recommendation objects (§3's schema) with expected impact and evidence links. They render in the dashboard as an approval queue.

**Should production parameters ever change automatically? Asymmetric automation — the institutional answer:**
- **Risk-reducing changes: automatic.** Demotion, budget cuts on decay warnings, universe-tier downgrades, kill-switch trips. No human should be required to make the system safer.
- **Risk-increasing or behavior-changing: never automatic.** Promotions, parameter changes, weight increases, new evidence sources — human approves a recommendation, and the approval is an event.
- **The middle path for parameters:** candidate params run in **shadow mode** — the engine computes shadow intents alongside live ones and logs both; after N weeks the recommendation carries real comparative evidence instead of backtest-only claims. This is cheap (pure computation, no orders) and it is how you change live parameters without gambling.

A fully self-modifying system fails not because ML can't pick parameters, but because a solo operator cannot audit a system that rewrote itself while he slept. Ten-year rule: the loop is closed *through* the human, at monthly cadence, with shadow evidence.

---

## 6. Feature Store

The greenfield doc's feature layer computes correctly but stores naively (every feature an event = volume bomb) and versions nothing. Redesign:

**Feature objects.** A feature is a registered, versioned definition: `{name, version, params_hash, deps: [features or md streams], window, dtype, owner, docs}`. The implementation is a pure function over its declared deps. Registry lives in-repo (a `features/registry.py` + generated manifest), not in a database — code review is the change-control mechanism.

**Versioning.** Semantic: change the computation → bump version → it is a *new feature*; old version remains computable for reproducing history. Every FeatureFrame carries a `feature_set_version` (hash of all member versions), stamped on every downstream event. Without this, "which VWAP did that 2027 trade see?" is unanswerable.

**Dependencies & computation.** Declared deps form a DAG; the engine topologically sorts and computes each feature once per tick (killing today's every-scanner-recomputes-candles disease). Cycles rejected at registration.

**Caching & storage — the volume fix.** Online: hot in-process cache keyed `(instrument, feature, version, ts)`; nothing persisted per tick. Offline: features are *recomputed* from certified market data by the same code during lake export — nightly batch materializes feature tables to Parquet. Verification: persist per-day *hashes* of feature streams from live; the nightly recompute must match, proving online/offline parity without storing every tick. Persist actual live values only for the frames referenced by intents (they're in the outcome rows anyway).

**Point-in-time correctness.** Features may read only events with `seq ≤ now` — enforced by the store's API, not convention (the API takes an as-of sequence number and the offline path uses the same accessor). This is the lookahead-bias firewall; make it impossible, not discouraged.

**Metadata, confidence, lifetime.** Each computed value carries: staleness (age of newest input), coverage (fraction of expected inputs present — a VWAP off 3 candles isn't a VWAP), and validity TTL. Consumers (detectors, Convex) receive these and the fusion model can learn to discount low-coverage features; policies can hard-gate ("no entries on frames with coverage < 0.9").

**API.** `store.frame(instrument, as_of_seq, feature_set_version) -> FeatureFrame` — the only way strategies/detectors get data. They declare required features; the Lifecycle Manager refuses to promote a strategy whose declared deps include deprecated feature versions.

**How strategies consume features:** exactly as the greenfield doc says (typed frame in, no I/O), plus declared dependencies and version pinning. The scanners-to-detectors collapse survives unchanged; detectors are just feature consumers that emit Evidence.

---

## 7. Experiment Framework

The greenfield doc names an "experiment registry" and gives it one sentence. The design:

**The run manifest is the atom.** Every executable run — backtest, walk-forward trial, shadow run, paper session, live session — starts by writing an immutable manifest: `{run_id, kind, code_version (git SHA), config_hash, params_version, feature_set_version, fusion_model_version, regime_model_version, data_snapshot_id, cost_model_version, seed, hypothesis (for experiments), parent_experiment_id}`. Reproduction is `replay --manifest <run_id>` — every input is pinned by the manifest. If any run cannot be launched from its manifest alone, that's a build failure.

**Data snapshots.** Backtests run against a named, immutable lake snapshot (`data_snapshot_id` = manifest of Parquet partitions + their hashes). Re-certifying or correcting historical data creates a *new* snapshot; old results remain reproducible against the old one, and the correction triggers re-runs. Without this, fixing a data bug silently invalidates every historical result while their reports still claim validity.

**Registry schema.** `experiments` (id, hypothesis, owner, created, status, conclusion) → `runs` (manifest fields, metrics summary, artifact paths) → `trials` (for parameter sweeps: one row per param point per OOS window). Strategy versions, feature versions, and model versions are rows in their own registry tables; runs reference them by id.

**Lineage to live trades — the requirement that pays for everything.** Every `intent.proposed` event already carries `(strategy_ver, params_ver, feature_set_version, fusion_model_version, regime_id, config_hash, code_version)` per W5. Therefore every paper and live trade joins back to: the exact strategy version, the run that promoted it, the experiment that produced that run, and the hypothesis it began as. "Why is this trade in my book?" terminates at a hypothesis with evidence — that chain is the difference between a research platform and a pile of scripts.

**Multiple-testing bookkeeping.** The registry counts trials per hypothesis family; §3's FDR corrections read the *registry*, not the researcher's memory of how many things were tried. This is the mechanism that keeps discovery honest.

---

## 8. Portfolio Intelligence

**Yes — and its absence is the greenfield doc's largest conceptual gap.** The risk kernel there is a *constraint checker*: it sizes each intent independently and blocks breaches. Nothing decides allocation *across* strategies, so capital allocation is implicitly "whichever strategy signals first eats the budget" — first-come-first-served is a portfolio policy, just an idiotic one.

**How institutions structure it:** three distinct layers that the doc collapses into one — **alpha generation** (strategies emit expected-return signals), **portfolio construction** (an optimizer/allocator turns signals + covariance + budgets into target positions), **risk management** (independent limits that can only reduce). Signals never size themselves; risk never selects.

**Design for this platform** — a Portfolio Construction service between Convex and the risk kernel:

*Capital allocation:* per-strategy risk budgets set monthly from the Research Engine's ledger — fractional Kelly (¼ Kelly at most) on regime-conditional expectancy, hard caps per strategy, floor for newly-promoted strategies. Vol-target the whole book: scale total daily risk to a target daily P&L volatility.

*Strategy rotation:* budgets shift with regime occupancy — a breakout strategy in a chop regime gets its budget cut *before* it bleeds, using §3's regime-conditional expectancy, not just trailing P&L (which reacts too late).

*Correlation & concentration:* daily strategy-return correlation matrix (shrunk — Ledoit-Wolf, given short histories) from the ledger; penalize allocating to highly-correlated strategies; sector/underlying concentration caps across *all* strategies combined (three strategies long CE on three private banks is one trade wearing three hats — today's per-strategy caps never see it).

*Capital efficiency:* rank strategies on return-per-rupee-of-risk-budget and expectancy-per-day-of-exposure; feed rankings to the Lifecycle Manager (a live strategy that's merely mediocre should lose budget before it loses its slot).

**Scale warning:** do not port a Barra-style optimizer. With 3–6 strategies and a premium-limited options-buying book, this layer is a few hundred lines of monthly allocation math plus daily exposure checks. The point is that it *exists as a layer with its own outputs* (`portfolio.allocation` events) — so when strategy count hits 15 in year 5, the seam is already there. *Complexity:* low now, grows with the book. *Trade-off:* another monthly human-review artifact; that's a feature.

---

## 9. Knowledge Graph

**Push-back: no — not as infrastructure, not for years, and possibly never.** The proposed relationships (regime→strategy, feature→symbol, OI→smart-money, strategy→performance) are real and valuable, but they are **join queries over the lineage schema you already need** (§7 + §3's tables), not a new storage paradigm. `Which strategies perform in which regimes` is a GROUP BY on the outcomes table. `Which features drive which strategies` is the ablation analysis joined to the registry.

Why a graph database would be a mistake here: (1) it adds an operational system to a solo-operated platform for zero query capability you can't get from DuckDB joins at this entity count (dozens of strategies, hundreds of features — not millions of nodes); (2) the *hard* part of "OI → smart money" is statistical (does the relationship hold, with what lag, in which regime — §3's job), not representational; a graph stores the claim, it cannot validate it; (3) graph schemas rot faster than relational ones when a solo team is also trading.

**What to adopt instead — the graph's *idea* at relational cost:** make relationships first-class rows. An `edges` table: `(from_entity, from_type, to_entity, to_type, relation, strength, evidence_run_id, valid_from, valid_to)` — written by Research Engine jobs when analyses find (or invalidate) relationships, each edge pointing at the run that established it. This gives you: queryable relationships, provenance, temporal validity, and — if in year 6 the entity count justifies it — a trivial export into an actual graph store. Future research gets easier because *validated* relationships accumulate with evidence attached, which is the useful property; the storage engine was never the point.

---

## 10. Ten-Year Vision

```
                                   ┌────────────────────────────────────────────────┐
                                   │              HUMAN (operator/CIO)              │
                                   │  approval queue · arm ceremony · monthly review │
                                   └────────▲──────────────────────────┬────────────┘
                                            │ recommendations           │ approvals (events)
┌──────────────────────┐          ┌─────────┴──────────┐               │
│  DATA & QUALITY      │ certified│  RESEARCH &        │               │
│  live feeds ─┐       │ lake     │  LEARNING          │      ┌────────▼─────────┐
│  history  ───┼─►gates├─────────►│  outcomes/labels    │      │ LIFECYCLE MGR    │
│  reference   │       │          │  experiments (§7)   ├─────►│ promote/demote   │
│  point-in-time universe│        │  discovery+FDR      │      │ shadow runs      │
└──────┬───────────────┘          │  decay (CUSUM)      │      └────────┬─────────┘
       │ md.* events              │  param walk-forward │               │ active set
       │                          │  fill-model calib ──┼──┐            │ + budgets
┌──────▼───────────────┐          └─────────▲──────────┘  │   ┌────────▼─────────┐
│  FEATURE STORE (§6)  │                    │ outcomes     │   │ PORTFOLIO        │
│  versioned, PIT-safe │                    │              │   │ CONSTRUCTION (§8)│
└──────┬───────────────┘          ┌─────────┴──────────┐  │   │ budgets·vol tgt  │
       │ frames                   │  OUTCOME/LABEL     │  │   │ correlation·conc │
┌──────▼───────────────┐          │  STORE (M2)        │  │   └────────┬─────────┘
│  DETECTORS→ CONVEX   │          └─────────▲──────────┘  │            │ sized targets
│  calibrated p_win(§4)│ intents            │ fills,      │   ┌────────▼─────────┐
│  regime engine (M5)  ├────────────────────┼─────────────┼──►│ RISK KERNEL      │
│  policies/vetoes     │                    │ vetoes,      │  │ limits·breakers  │
└──────────────────────┘                    │ outcomes     │  │ (reduce-only)    │
                                            │              │  └────────┬─────────┘
                          ┌─────────────────┴───────────┐  │           │ orders
                          │  EVENT LOG (sequencer, W1)  │  │  ┌────────▼─────────┐
                          │  single-writer daemon        │◄─┼──┤ EXECUTION GW     │
                          │  → Parquet lake nightly      │  └──┤ sim (calibrated) │
                          └──────────────▲───────────────┘     │ live brokers     │
                                         │ all events          │ reconciliation   │
                          ┌──────────────┴───────────────┐     └──────────────────┘
                          │ OBSERVE: dashboard·notifier·  │
                          │ watchdog (M8)·audit           │
                          └───────────────────────────────┘
```

**The feedback loops, explicitly — this is what makes it a platform rather than a pipeline:**
1. **Outcome loop (monthly):** fills/vetoes → Outcome Store → Research → recommendations → human approval → config/params → engine. Closed through the human; risk-reducing branch closes automatically.
2. **Calibration loop (monthly):** outcomes → Convex reliability curves → refit → new model version → conviction quality improves; every score carries its model version so history stays interpretable.
3. **Simulation loop (weekly):** live fills → fill-model calibration → simulator coefficients → paper/backtest realism → promotion decisions get honest.
4. **Allocation loop (monthly + daily):** strategy ledger → correlation/expectancy → budgets → portfolio construction → sizing; regime occupancy shifts budgets intra-month.
5. **Decay loop (daily, automatic):** live expectancy → CUSUM → warn (halve budget) → demote (paper) — the only fully-automatic loop, because it only ever reduces risk.
6. **Data-quality loop (daily):** quality gates → quarantine → re-certification → research re-runs against new snapshots, with old results preserved against old snapshots.
7. **Shadow loop (continuous):** candidate params/models compute alongside live, producing comparative evidence that feeds loop 1 — behavior change without capital risk.

Ten-year evolution happens inside seams already drawn: SQLite sequencer → Postgres/Kafka (same event contract); Stage-A fusion → Stage-B ML (same Evidence/Conviction contract); one broker → several (same ExecutionPort); solo human → small team (the approval queue and manifests are already the collaboration surface).

---

## 11. Final Verdict

**Score: 6.5/10.** The skeleton (event spine, purity, replay, one risk gate, promotion pipeline, honest non-goals) is genuinely strong — an 8.5 skeleton. It loses three points because: the spine's concurrency story is wrong (W1), the research/learning machinery — which the platform's own thesis says is the edge — is named but not designed (§3–§5), data history/quality doesn't exist (W3), fill realism is hand-waved on a platform whose asset class is *wide-spread options* (W4), and there is no portfolio layer (§8). It gains half a point back for being buildable by one person, which most "institutional" designs are not.

**What would make it 10/10:** the sequencer daemon owning the log; semantic (not byte) determinism with money as integers; a data quality & history layer with point-in-time reference data and named snapshots; the Outcome Store; the Research Engine of §3 with FDR-controlled discovery and CUSUM decay; calibrated probabilistic fusion with an abstain band; full manifest-based lineage from hypothesis to live fill; portfolio construction with fractional-Kelly budgets and cross-strategy concentration; asymmetric automation (auto-de-risk, human-gated re-risk) with shadow-mode evidence; and the watchdog/recovery layer rehearsed before the first live rupee.

**Top 20 improvements, priority order:**

*Critical — before any further build:*
1. Event-store **sequencer daemon** (single writer, consumer cursors, socket appends) — fixes W1 and pre-draws the Postgres seam.
2. Full **lineage stamps** on every decision event: code SHA + config hash + feature-set version + model versions + data snapshot (W5, §7). Retrofit is impossible; do it before event #1.
3. **Outcome & Label Store** (M2) — every downstream ambition depends on it.
4. Replace byte-identical replay with **semantic determinism**: single-threaded sequence-driven engine loop, integer paise, tolerance-based golden tests with re-baseline ceremony (W2).
5. **L1 quotes captured at decision time** + pessimistic-default fill model (W4's prerequisite).
6. **Historical data acquisition & quality layer** with point-in-time reference data and named data snapshots (W3, M1).

*High — before live capital:*
7. Run-manifest **Experiment Registry** with reproduce-from-manifest as a CI check (§7).
8. **Research Engine v1**: daily outcomes/decay jobs, monthly walk-forward, deflated-Sharpe reporting (§3).
9. **CUSUM decay monitors with automatic demotion** — the auto-de-risk loop (§3, §5).
10. **Calibrated Stage-A fusion** (logistic + reliability curves + abstain band) replacing hand weights (§4).
11. **Watchdog/dead-man's switch + rehearsed recovery runbook** + broker-side SL verification (W6, M8).
12. **Portfolio Construction v1**: monthly fractional-Kelly budgets, cross-strategy sector/underlying concentration caps (§8).
13. **Fill-model calibration job** from live fills, simulator-error as a monitored metric (M6).
14. **Regime Engine** as a versioned component with persisted point-in-time labels (M5).

*Medium — year 1–2:*
15. **Feature registry with versioning, PIT-safe API, and online/offline hash verification**; stop persisting per-tick feature events (§6).
16. **Shadow-mode execution** for candidate parameters/models (§5).
17. **FDR/SPA-controlled discovery sweeps** wired to the registry's trial counts (§3, §7).
18. **Schema registry + migration tooling** for event payloads; compatibility rules in CI (§1).
19. **Lifecycle Manager as enforcing component** — engine refuses intents from non-live strategies; approval queue UI (M4).

*Future — year 3+:*
20. Stage-B ML fusion with SHAP explainability; Postgres/Kafka migration when event volume demands; `edges` relationship tables (the knowledge-graph idea at relational cost, §9); multi-broker execution; second-machine warm standby.

The uncomfortable summary: the greenfield document designed the *trading* half of a research-driven platform and gestured at the *research* half. For a system whose stated edge is validated strategy quality, that's building the racecar and sketching the wind tunnel. Build items 1–6 before anything else; they are the ones you cannot retrofit.
