# ROADMAP — CTO Execution Plan

**Written:** 2026-07-11. **Owner:** us. **Goal:** first live rupee traded with measured positive expectancy in ~6 months, on ₹1L capital.
**This document replaces:** ARCHITECTURE_GREENFIELD.md, ARCHITECTURE_REVIEW_GREENFIELD.md, ARCHITECTURE_REFACTOR_PLAN.md, FRONTEND_REDESIGN_PLAN.md as active plans. They move to `docs/archive/`. V2_BLUEPRINT.md stays — it is the vehicle.

---

## 0. CTO position — what the repo actually says

Three facts from this review outrank every architecture opinion written this month:

1. **The engine's conviction ladder is inverted.** Week-1 observe data (engine/WEEK1_REVIEW): A+ grades hit 21% at 60m with −0.46% average move; B grades hit 50%. Higher conviction = worse outcome, monotonic across horizons. Plus `index_slope_pct` is NULL on all 269 regime rows, so regime lean — which drives ~1,000 rejects/day — runs on breadth alone. **The intelligence layer currently anti-predicts, and one of its two regime legs is disconnected.** Nothing matters more than this.
2. **We have almost no data.** candles_5m since ~Jul 3. Scanner histories since mid-June. Delivery since Jun 5. Four observed trading days, all VIX 11–12 flat. Every plan that assumes "backtest over history" is fantasy for another 6+ months. The corollary: **every market day that passes without automated outcome labeling is a wasted day of the scarcest resource we have.**
3. **V2 strangler is already in flight and correct.** engine/ P0 runs, triggers moved in-engine ahead of schedule, decisions journal with full factor breakdowns. The greenfield rebuild I proposed two days ago is a 6-month detour to re-solve problems V2 already solves. I'm killing my own plan. We keep exactly five ideas from that review and inject them into V2: quote capture at decision time, pessimistic paper fills, an outcome store, decay/calibration monitoring, and lineage stamps (FORMULA_VER already does half of this).

**The one business question:** does anything here have positive expectancy after costs? Nobody knows. Four days of data say "not yet, possibly negative." So the company priority is not building — it's **measuring, fixing the brain, and letting the market grade us for 8 weeks.**

Also flagged: reading `data/*.db` through this session showed several tables as malformed (likely lazy-mount artifacts of live WAL files, but `paper_trades.db` too). Verify integrity on the host tonight; nightly `PRAGMA integrity_check` + `VACUUM INTO` backup becomes a task (E6-2). Losing the journal now would be losing the only asset.

---

## 1. STOP immediately

**Stop: the greenfield rebuild.** All of it — event sequencer, semantic replay, feature store, experiment manifests, portfolio construction, Kafka/Postgres seams. Interesting engineering; poor business. It re-platforms a system whose signal is unproven. If the edge is real, we can afford to re-platform later; if it isn't, the platform is worthless anyway.

**Stop: new factors, new scanners, new analytics products.** Seven factors is already more than we can statistically validate with ~5 signals/day/grade. Skew analytics shipped — fine, freeze it. The V2 blueprint's rule stands: nothing re-enters except through journaled evidence.

**Stop: frontend work.** FRONTEND_REDESIGN_PLAN.md → archive. The cockpit gets one minimal pass (E3-4) only because "human approves trades" needs a screen. Sector heatmaps, mobile stacks, chart galleries: no.

**Stop: architecture documents.** Five reviews and three plans were written in six weeks (P0 review, P1 strategy review, P2/P3 quality review, code review, greenfield + its review). The marginal return of document #9 is zero. This roadmap is the last planning artifact; from here, output is commits and journal rows.

**Stop: maintaining the discontinued.** momentum and directional-iv services are profile-gated corpses with Dockerfiles, configs, and tests still in the tree. Delete from compose, move code to `old/`. Also stop *improving* anything on the V2 kill list (composite, trade_suggester, morning_confluence, per-scanner services) — they get life support only until their strangler phase kills them.

**Stop: 13 Dockerfiles / 4 compose files sprawl** — consolidated in E3, not incrementally polished.

---

## 2. What MUST exist before the first profitable live trade

Seven things. Nothing else.

1. A decision engine whose grades **correlate positively with forward outcomes** over ≥2 non-flat weeks (E2).
2. **Automated outcome labeling** of every decision — emitted AND rejected — with costs and realistic fills (E1). Without this we can't know #1.
3. **Paper fills that cross the spread** + recorded bid/ask at decision time (E1-2). Optimistic fills would promote a losing system.
4. **8 weeks of paper trading with positive expectancy after costs** on ≥40 trades, through the promotion gate defined in E5-4.
5. **Order safety that survives our death:** entry+SL-M pair at broker (exists), verified-SL loop, daily-loss lockout (exists, on), EOD flat, kill switch, restart reconciliation (E4).
6. **A ₹1L affordability reality check** (E5-1): at 2% risk = ₹2,000/trade, most stock option lots are unaffordable or force >30% SL. The live MVP may be NIFTY-weekly-only. This math decides the product and nobody has run it.
7. **A watchdog + rehearsed recovery runbook** (E4-5): the box dying at 13:40 with an open position must be a boring event.

Explicitly NOT required: portfolio construction, experiment registry, feature store, ML, backtester over deep history, multi-broker, event sourcing, knowledge graph.

---

## 3–4. The backlog (ROI-ordered epics; [EDGE] = moves expectancy, [ENG] = engineering quality only)

Estimates are solo-dev days. Milestones: **M1** = brain trustworthy (end wk 3) · **M2** = V1 sprawl dead (end wk 6) · **M3** = live-ready executor (end wk 9) · **M4** = paper campaign verdict (end wk 17) · **M5** = first live trade.

---

### EPIC E1 — Truth & Measurement (Research Engine v0) — [EDGE] — ~9 days — start Monday

*The highest-ROI work in the company. It converts every market day into learning, forever. Everything else waits on nothing; this waits on nothing.*

**E1-1 · Outcome labeler (nightly)** — 3d · P0 · blocked by: none
Nightly job reads `engine_decisions` (EMITTED, REJECTED, WATCH) + `candles_5m`, writes `decision_outcomes`: forward spot move at 30/60/90m and to-close; for decisions with contract+premium, option P&L path from premium candles where fetchable, else Black-Scholes approximation off spot+IV (flagged as approx); MAE/MFE; costs from `costs.py`; would-have-been R for rejects (the veto counterfactual).
*AC:* every decision row ≥1 day old has an outcome row; job idempotent; runs in cron after 18:00; backfills from Jul 3.

**E1-2 · Quote capture + pessimistic paper fills** — 2d · P0 · blocked by: none
At decision time, persist bid/ask/spread% of the *actual contract* onto the decision row. Paper fills: buy at ask, sell at bid, +1 tick adverse. No mid-price fills anywhere.
*AC:* new decisions carry quotes; paper_trader fill path uses them; historical fills flagged `fill_model=v0`.

**E1-3 · Nightly digest (one Telegram message)** — 2d · P0 · blocked by: E1-1
Per grade × trigger: N, hit rate, avg R after costs, expectancy; calibration table (score decile → hit rate); top-3 veto reasons with counterfactual P&L; slippage-model vs realized (once live-paper). One message, one HTML page in cockpit.
*AC:* arrives nightly; a wrong-direction ladder is visible within one glance.

**E1-4 · Weekly attribution report** — 2d · P1 · blocked by: E1-1
Per factor: alignment vs outcome correlation (point-biserial + simple logistic coefficient), sample counts, "this factor earned/lost its weight" verdict. Per gate: rejects that would have won/lost.
*AC:* runs Sundays; directly consumable for E2-3 weight retune.

*Deliberately absent from v0:* full market replay backtester (no history yet — candles are accumulating, revisit month 4+), FDR machinery (sample sizes make it moot; enforce instead: no conclusions on N<30), experiment registry (FORMULA_VER + config snapshot per decision row covers lineage at our scale).

---

### EPIC E2 — Fix the Brain — [EDGE] — ~8 days + 2 weeks observation — M1

**E2-1 · Fix `index_slope_pct` NULL** — 0.5d · P0 · blocked by: none
Pass NIFTY SuperSmoother slope into `regime.load()`. *AC:* non-NULL on every new regime row; breadth/slope disagreement path unit-tested.

**E2-2 · Root-cause the inverted ladder** — 3d · P0 · blocked by: E1-1 (needs labeled data to verify fix)
Audit in order: factor sign conventions vs trigger direction; trigger direction labels (SONAR_BAND fired 1,019× in 4 days — 3× more than ORB+VWAP+BR combined: check its direction assignment and whether it should carry 30-weight trigger quality); `with_the_tape` lean logic (running on breadth alone until E2-1); score-weight table vs §7 of blueprint. Hypothesis to test explicitly: in a flat mean-reverting week, *momentum* triggers anti-predict — meaning the ladder may be regime-conditional, not sign-flipped.
*AC:* written root-cause note in engine/; fix shipped; FORMULA_VER bumped; re-scored historical decisions (offline, from journaled factor breakdowns) show non-inverted ladder on the same 4 days OR documented conclusion that the week was the cause.

**E2-3 · Offline formula replay tool** — 2d · P0 · blocked by: E1-1
Re-score journaled decisions under a candidate weight-set/formula and report the outcome table per E1-3 — *this is our backtester until real history exists*, it costs nothing and answers "would formula v3 have ranked better?" instantly.
*AC:* `python -m engine.rescore --formula v3` produces the digest tables for any past window.

**E2-4 · Two-week non-flat re-observation** — 0.5d setup + calendar time · P0 · blocked by: E2-1..3
Engine observe/soft mode through ≥2 weeks including some VIX>13 or AMBER days. GO criteria: ladder monotonic in the right direction; A-grades ≥55% 60m hit or positive avg R; RED posture path exercised at least once (synthetically if the market refuses).
*AC:* GO/NO-GO note. NO-GO → repeat E2-2. **E3 is gated on this GO.**

---

### EPIC E3 — Kill V1 Sprawl (strangler P1+P2) — [ENG, enables EDGE] — ~8 days — M2 · blocked by: E2-4 GO

**E3-1 · P1 cutover** — 1d — entry_gate reads `engine_decisions` (hard mode). Kill: composite, trade_suggester, morning_confluence services. *AC:* B&B + discount gated by engine for a full week; kill-list containers removed from compose.
**E3-2 · P2 factor migration** — 3d — factor computations move in-engine (drop `get_latest_*` table indirection). Kill: iv-rank, oi-buildup, gap, delivery-surge, smart-money, sonar *services* (logic already ported/absorbed). *AC:* engine factors computed from market data directly; `*_history` writers retired; compose at 5 services.
**E3-3 · Telegram 10→1** — 1d — one Decisions channel + morning/EOD digest (E1-3 plugs in here). Delete per-scanner notifiers. *AC:* exactly one bot voice.
**E3-4 · Cockpit v1 (minimal)** — 2d — regime strip + ranked decisions with why + positions + journal, per blueprint §8, over engine tables. No other frontend work. *AC:* ten-second read test.
**E3-5 · Tree cleanup** — 1d — momentum/directional-iv to `old/`; docs to `docs/archive/`; one docker-compose.yml; delete 6 orphan Dockerfiles. *AC:* root .py count roughly halves; new-machine bringup documented in README (≤10 lines).

---

### EPIC E4 — Executor & Live Safety (strangler P3) — [EDGE-critical] — ~10 days — M3 · blocked by: E3-1

**E4-1 · Unify executor** — 3d — paper_trader + order_manager + auto-exit → convex-executor consuming Decisions; OI-contradiction exit survives as executor policy; daily-loss lockout (RISK-1) stays on. *AC:* paper trades flow end-to-end through the one executor; test_paper_* green.
**E4-2 · Dhan live adapter + token lifecycle** — 2d — order placement, SL-M pair, position/fill polling; token refresh alerting *before* expiry. *AC:* places+cancels a 1-lot real order in a supervised smoke test (then AUTO_EXECUTE back off).
**E4-3 · SL-verification loop** — 1d — poll that the protective SL exists at broker for every open position; missing → emergency flatten + alert. *AC:* chaos-tested by manually cancelling an SL in paper smoke.
**E4-4 · Restart reconciliation** — 2d — on boot: rebuild book from journal, diff vs broker positions, operator must confirm before re-arming. *AC:* kill -9 during open paper position → clean recovery, documented.
**E4-5 · Watchdog + runbook + drill** — 2d — heartbeat file + external check (cron on phone/VPS/UptimeRobot) alarming if engine silent during market hours; kill-switch command; one-page runbook; run the failure drill once for real. *AC:* drill executed; time-to-recover recorded.

---

### EPIC E5 — Paper Campaign & Promotion — [EDGE] — ~4 days + 8 weeks calendar — M4/M5 · blocked by: E2-4, E4-1

**E5-1 · ₹1L affordability audit** — 1d · P0 · blocked by: none — **do this in week 1, it shapes everything.** For the current universe: lots affordable at ₹2k risk/30% SL, premium sizes, spread% distribution at our entry times, fixed-cost drag per round trip at 1 lot. Decide: stocks+index, or index-only live with stocks on paper. *AC:* a table and a decision.
**E5-2 · Campaign definition** — 0.5d — instruments (per E5-1), risk 1% while proving, max 2 concurrent, lockout 3%, formula frozen except scheduled FORMULA_VER bumps, weekly review ritual = E1-4 report. *AC:* written, dated, signed by both of us.
**E5-3 · 8-week measured paper campaign** — calendar — engine hard-gated, executor unified, digests nightly. Mid-campaign weight retune allowed once (via E2-3 evidence, version-bumped).
**E5-4 · Promotion gate + first live arm** — 0.5d + ceremony — GO requires: ≥40 trades, positive expectancy after costs, ladder calibrated (A>B in realized R), max DD < 15% of paper capital, all E4 drills passed. Then ₹1L, 1% risk, index-only if E5-1 says so, manual arm daily for week 1.
*AC:* first live trade with a pre-written abort rule: 2 consecutive daily-lockout hits or DD>10% → back to paper, no debate.

---

### EPIC E6 — Data hygiene (parallel, background) — [ENG] — ~3 days

**E6-1 · Verify DB integrity on host** — 0.5d · P0 · **tonight** — `PRAGMA integrity_check` on iv_history.db and paper_trades.db on the Windows host (this session saw malformed reads through the mount). *AC:* clean, or recovered from backup.
**E6-2 · Nightly integrity + backup job** — 1d · P0 — `integrity_check` + `VACUUM INTO` dated backup + 7-day rotation + failure alert. *AC:* restore tested once.
**E6-3 · Candle retention + DB size policy** — 0.5d — candles_5m at 14k rows/day forever needs a cap: keep all (it's our future backtest data!) but move >30d partitions to monthly attached DB or parquet export. *AC:* main DB growth bounded; old candles still queryable.
**E6-4 · Delete data/ debris** — 0.5d — .fuse_hidden files, iv_history_recovered.db (0 bytes), dated copies after E6-2 backups exist.

---

### ROI ordering, stated plainly

**Buys expectancy directly:** E1 (measurement — compounding, start first), E2 (a brain that predicts instead of anti-predicts), E5-1 (may pivot the whole product to index options), E5-3/4 (the only path to justified live capital), E1-2 (kills the biggest false-positive risk in promotion), E4-3/4/5 (protects capital once live).
**Engineering quality only (kept because cheap and they reduce drag):** E3 (halves the operational surface — indirectly EDGE because 13 services × maintenance is why measurement never got built), E6.
**Rejected despite being "good engineering":** everything in §1's stop list.

**Dependency spine:** E1-1 → E2-2 → E2-4(GO) → E3-1 → E4-1 → E5-3 → E5-4. Everything else hangs off it. Total dev effort ≈ 42 days ≈ 9–10 wks part-time solo, + 8-wk campaign overlapping E3/E4 → **live around week 20–24.**

---

## 5. Every abstraction, challenged

| Module | Verdict | Why |
|---|---|---|
| engine/ (V2 P0) | **KEEP — the product** | Only defensible abstraction in the tree. Fix its brain (E2). |
| discount.py (3,170 ln) | **DO NOT REFACTOR — dismantle at P3** | Refactoring a module scheduled for organ donation is waste. Its IV/discount math exits as pure functions (premium-value factor + contract selector), the rest dies. |
| break_bounce_strategy.py (1,418 ln) | Trigger logic **KEEP**, service **KILL at E3** | Best entry anatomy in V1; already routed through shared book. Becomes/feeds the break-retest trigger only. |
| momentum_strategy.py (1,301 ln) | **DELETE at E3-5** (keep ScripMasterLotSizer + ported ORB/VWAP triggers) | Discontinued service; 1,300 lines hosting one lot-sizer class the whole system imports. Extract class → `old/`. |
| paper_trader + order_manager (1,700 ln) | **MERGE at E4-1** | Two halves of one executor. |
| 8 scanner services + runners + configs | **KILL at E3-2** | Factors, not services. ~2,500 lines + 8 containers → in-engine functions. |
| entry/cycle/pre-market gates + breadth gate | **MERGE** into engine gate stack (P1/P2) | Four corners of one question. |
| trade_suggester, morning_confluence, composite | **KILL at E3-1** | Three approximations of the engine. |
| dashboard_app (992 ln) + settings UI | **REBUILD small at E3-4**, settings UI **FREEZE** | Reads V1 tables that stop existing. Settings toggles for services being killed = dead weight. |
| collectors/ | **KEEP as-is** | Already correct: sole-writer, boring, works. Rename to convex-data at P4, not before. |
| upstox_adapter (Dhan-shaped shim) | **KEEP until P4** | Ugly but working; fixing the shape now buys zero expectancy. Debt bucket: 6-months. |
| iv_analytics / skew | **FREEZE** | Shipped; no consumer in the funnel yet. A factor candidate *only* via journal evidence. |
| directional_iv_* | **DELETE at E3-5** | Blueprint already sentenced it. |
| 13 Dockerfiles, 4 compose files | **→ 1 compose + ≤2 Dockerfiles at E3-5** | Pure drag. |
| Solving-a-hypothetical-future check | Everything above solves a today-problem. The greenfield stack (sequencer, manifests, feature store, portfolio layer) solved 2029 problems — archived accordingly. | |

## 6. The MVP (what trades real money in ~6 months)

**Exists:** 4 containers (convex-data, convex-engine, convex-executor, convex-cockpit) + nightly research cron. One SQLite DB (WAL, sole-writer per table, nightly verified backup). One Telegram channel. Engine: 7 factors, 4 triggers, one gate stack, one versioned formula — hard gates + calibrated-enough grades. Executor: paper/live behind AUTO_EXECUTE, entry+SL-M pair, SL-verify loop, OI-contradiction exit, 3% daily lockout, EOD flat 15:12, restart reconciliation. Research v0: outcome labeler, nightly digest, weekly attribution, offline formula replay. Watchdog + runbook. Universe: per E5-1 — likely NIFTY weekly + a short list of liquid stock options, 1% risk, max 2 concurrent.

**Does not exist:** 8 scanner services, 3 fusion layers, 10 Telegram bots, backtester-over-history, portfolio construction, feature store, experiment registry, event log, ML, knowledge graph, multi-broker, frontend beyond cockpit v1, and any document with "architecture" in its filename.

## 7. Technical debt, bucketed

**Must fix now (weeks 1–3):** inverted ladder (E2-2); index_slope NULL (E2-1); DB integrity verification + backups (E6-1/2, tonight/this week); no bid/ask on decisions (E1-2); no automated outcome labeling (E1-1); optimistic paper fills (E1-2).
**Can wait 6 months:** upstox_adapter's Dhan-shaped contract (fix at P4 rename); dashboard rebuild beyond v1; candle storage format (parquet export exists as E6-3 stopgap); settings UI; CRLF file gotchas (workaround known); god-module residue after P3.
**Can wait 2 years:** SQLite→Postgres (only if multi-machine or write contention actually appears); proper feature/experiment registries (only if strategy count > ~5); replacing hand weights with a fitted model (needs ≥200–500 labeled outcomes anyway — the journal is accumulating them).
**Never worth fixing:** V1 scanner code quality (kill list); momentum/directional-iv cleanup beyond deletion; byte-perfect replay; event-sourcing retrofit; the `old/` directory.

## 8. Research Engine v0 — the concrete spec

Not a service. **One module (`research/`) + two cron entries**, reading tables that already exist.

**Stores (new tables in the same DB, research-owned):**
`decision_outcomes` (decision_id, horizon, fwd_spot_pct, option_pnl_R, pnl_method{candle|bs_approx}, mae, mfe, costs, hit) · `formula_scores` (decision_id, formula_ver, score, grade — written by the rescore tool) · `weekly_attribution` (week, factor, n, hit_rate, corr, logit_coef, verdict) · `daily_digest` (date, json blob rendered to Telegram/HTML).

**Nightly (18:30 IST):** label yesterday-and-older unlabeled decisions (E1-1) → compute digest (E1-3) → integrity_check + VACUUM INTO (E6-2) → send one Telegram message. Runtime target: <5 min.

**Weekly (Sun):** attribution report (E1-4); calibration curve (score decile → realized hit, N per cell, cells with N<30 rendered grey — *no conclusions below N=30, enforced in the renderer, not in our discipline*); veto counterfactual ranking ("the `with_the_tape` gate rejected 4,900 trades; they would have made/lost X after costs" — this single number will tell us if regime lean helps or hurts); decay check v0 = 4-week rolling expectancy per trigger with a flag when it crosses zero (CUSUM later, a threshold now).

**Feature importance, v0 honesty:** with N≈tens per cell we do sign-and-magnitude checks, not ML: per-factor alignment vs hit correlation + a 7-coefficient logistic fit. Verdict labels: EARNING / NEUTRAL / COSTING / INSUFFICIENT-N. A COSTING factor for 3 consecutive weeks → weight-cut proposal.

**Parameter discovery, v0 = the rescore tool (E2-3):** because every decision journals its full factor breakdown, any weight-set/formula variant can be re-scored over all history in seconds *without market replay*. Grid over weight perturbations → pick plateaus, not peaks → propose. This is 80% of a backtester's decision-value at 2% of its cost, today.

**Recommendation + validation loop:** research proposes (weight change, gate threshold, factor cut) with evidence attached → human applies via config → FORMULA_VER bump → old and new formulas both scored on all *new* decisions (shadow, free) → next weekly report shows A/B. **Nothing self-modifies.** Risk-reducing automation only (lockouts, decay flags cutting size) — parameter changes always pass through us.

## 9. Convex — the intelligence layer, final shape

**Responsibility (unchanged from blueprint, restated as the company's one sentence):** every 5 minutes, convert market state into either *ranked, sized, fully-explained Decisions* or *journaled rejections* — and never let a trigger trade without context, or context trade without a trigger.

**Inputs:** market.db (candles, chain/IV snapshots, spot, VIX, breadth, deals, delivery) + risk state from executor (open positions, lockout status) + config (weights, gates, FORMULA_VER).
**Outputs:** `engine_decisions` (with factor_json, gate_json, quotes, formula_ver — the research substrate), `engine_regime`, one Telegram stream.
**Data flow:** exactly blueprint §4 — one direction, engine reads DB only, zero API calls, executor consumes decisions, cockpit reads everything. No new arrows.

**How Convex and Research interact — the actual learning loop:** Convex journals every decision with its full breakdown → Research labels outcomes nightly and attributes weekly → proposals → human bumps FORMULA_VER → rescore tool validates retroactively, shadow scoring validates prospectively → Convex's next cycle uses the new weights. Convex is the *decider*, Research is the *grader*; they share only tables. Neither ever blocks the other.

**Hardcoded now (deliberately):** the 7 factors and 4 triggers (closed set); gate thresholds (VIX 22, spread 5%, IVR 55, 14:30 cutoff, DTE rules); the weighted-sum formula shape; regime rules; grade boundaries. Hand-set, versioned, cheap to change through config.
**Data-driven later, in order, each gated by sample size:** (1) weights — from weekly attribution, human-applied, starts ~week 4; (2) grade→size mapping — from realized R per grade, ~200 outcomes; (3) score→p(win) calibration mapping, ~300 outcomes; (4) replace weighted sum with logistic regression fit on the journal — same inputs, same explainability, fitted weights, ~500 outcomes (roughly month 6–9); (5) regime-conditional weight sets — only if attribution shows factors flipping sign by regime (Week-1's inverted ladder hints this is real). Anything beyond (5) — trees, embeddings, whatever — must beat the logistic in shadow for 8 weeks. The bar only rises.

---

## Week 1, starting Monday

Mon: E6-1 (integrity, tonight) · E2-1 (slope fix) · E1-2 (quote capture — start).
Tue–Wed: E1-2 finish · E1-1 (labeler).
Thu: E1-1 backfill to Jul 3 · E5-1 (₹1L affordability audit).
Fri: E1-3 (first nightly digest goes out) · start E2-2 (ladder root-cause) with labeled data in hand.

By Friday we will know, with numbers, whether the last five weeks of signals had any edge after costs. That's the company actually starting.
