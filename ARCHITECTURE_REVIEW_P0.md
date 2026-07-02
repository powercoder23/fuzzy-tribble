# P0 Architecture Review — Dhan/Upstox F&O Trading Platform

**Reviewer:** Senior quant architecture review (adversarial)
**Date:** 2026-07-02
**Scope:** P0 items — (1) Overall architecture, (2) Data pipeline, (3) Core trading engine.
**Method:** Full dependency-graph extraction, source read of all core modules, live inspection of `data/iv_history.db`.

Severity legend: **[CRITICAL]** wrong results or data loss now · **[HIGH]** wrong results under common conditions · **[MED]** maintainability/perf drag · **[LOW]** hygiene.

> **Status update (2026-07-02, same day):** All CRITICAL and HIGH code issues fixed —
> WAL+busy_timeout (§0), EOD daily promotion + read-side date dedup + sole-writer
> enforcement (§2.1), fetched_at column (§2.2), expiry-cache midnight reset (§2.3),
> bulk-fetch path deleted (§2.4), monitor thread split (§3.1), Sonar veto-not-flip
> (§3.2), score floor removed (§3.3), gap-aware SL fills (§3.5), gate-failure alerts
> (§3.6), direction/sector concentration caps (§3.7), rate-limiter lock (§1.4),
> CLAUDE.md synced to reality (§1.2 docs). Deliberately NOT done (structural, multi-week):
> §1.1 god-module decomposition and §3.4 backtest harness. Data broker is Upstox-only;
> Dhan reserved for future order placement. The corrupt iv_history.db in data/ is a
> hot-copy artifact — recover the live volume copy with VACUUM INTO (see chat notes) or
> scripts/recover_iv_history.py for genuine corruption.

---

## 0. Headline finding — your production database is corrupted RIGHT NOW

**[CRITICAL]** `data/iv_history.db` fails `PRAGMA quick_check` with ~100 errors of the form *"invalid page number 3365"* — the B-tree references pages beyond the end of the file. The file is byte-for-byte the same size (11,538,432) as the June-10 snapshot `iv_history_10-06-2026.db` (which passes quick_check cleanly), i.e. the live DB appears truncated back to its June-10 size while its page tree kept growing. Classic outcome of concurrent writers + a hot file copy or crash mid-write.

Consequences, today:

1. Every scanner that reads this DB (iv-rank, oi-buildup, gap, delivery-surge, smart-money, composite, breadth, pre-market gate, auto-exit, cycle gate) is **fail-open by design** — so they are all silently degrading to "no signal / no gate" while the system keeps trading. Nothing alerts you.
2. Any row that lands on a corrupted page is unreadable; queries can return partial results or raise, depending on the page touched.
3. Because reads that raise return `{}`/`[]` everywhere, you cannot distinguish "market is quiet" from "database is broken."

**Why it happened (root cause, not bad luck):** at least nine separate modules across seven+ containers write into this single SQLite file (`iv_store.py`, `discount.py` directly, bhav/deals/vix collectors, and the six `*_scanner.py` persist paths). There is **no `PRAGMA journal_mode=WAL`, no `busy_timeout`, no isolation configuration anywhere in the codebase** (grep confirms zero occurrences). SQLite's default rollback-journal mode across multiple containers on a shared Docker volume is a corruption machine.

**Fix (in order):** (a) restore from the June-10 snapshot + re-collect, or salvage via `.recover`; (b) enable WAL + `busy_timeout` on every connection *today*; (c) enforce the single-writer contract that `iv_store.py` already declares in its first line and every module ignores; (d) medium-term, this is the Postgres migration `iv_store.py`'s docstring already anticipates. Also add a startup + hourly `PRAGMA quick_check` with a Telegram alert — the absence of that alarm is why you're finding this out from a code review.

---

## 1. P0-1 — Overall architecture

### 1.1 `discount.py` is a 3,177-line god module and everything depends on it — [HIGH]

`DiscountedPremiumScanner` is simultaneously: broker-client factory (`__init__` builds the `UpstoxDhanAdapter`), rate limiter (`rate_limited_call`), retry engine, F&O universe loader, IV-history schema owner (`ensure_iv_history_schema`, `init_iv_db`, `migrate_csv_to_sqlite`), quant library (HV, IV rank/percentile, expected move, OI walls, buildup classification), scoring engine, strike scanner (`scan_single_strike` alone is ~500 lines), Telegram reporter, and CSV writer.

Every other service imports it *just to get broker access*: `momentum_strategy`, `break_bounce_strategy`, `directional_iv_strategy`, `data_provider`, and — worst — `collectors/iv_collector_service.py`. Your "data-only" collector (Service 1) imports the full scoring engine, so a bug in scoring code can take down data collection. Instantiating a scanner also triggers `update_scrip_master()` + universe resolution — heavy side effects in a constructor.

**Fix:** extract three modules with no upward dependencies: `broker.py` (adapter + rate limit + retry), `iv_math.py` (pure functions — most already are), `persistence.py` (schema + writes, i.e. fold `discount.py`'s DB code into `iv_store`). The scanner should *receive* a broker client, not build one.

### 1.2 The stated architecture and the running architecture are different systems — [HIGH]

- `CLAUDE.md` says 4 services, discount paused via `profiles: ["discount"]`, "only iv-collector calls option-chain APIs continuously." **All three claims are false against `docker-compose.yml`:** there are 10 services; `discount` has no `profiles:` key (runs by default); the discount service does a full chain sweep of ~120 names every 15 minutes (§3.1), and `sonar` also instantiates a full scanner.
- Every scanner service's compose comment says *"To run manually: docker-compose --profile X up"* — but `iv-rank`, `oi-buildup`, `gap-scan`, `delivery-surge`, `smart-money`, `composite`, `sonar` have **no `profiles:` key**. A plain `docker-compose up` launches all of them. Either the comments or the keys are wrong; given the SQLite corruption above, this drift has real cost.
- Four compose files (`docker-compose.yml`, `.prod.yml`, `.prod.validated.yaml`, `.dev.yaml`) with no generation mechanism. Pick one base + one override; delete the rest.

### 1.3 Copy-paste framework instead of a scanner framework — [MED]

Eight near-identical `*_runner.py` files and twelve `*_config.py` files differ only in table name, scan times, and scanner class. Three `RiskManager` classes (momentum's and break-bounce's are ~90% identical), four Telegram notifier classes plus `notifications.py` plus `collectors/notify.py` (which wraps `notifications.py`). Every new scanner adds three files and another writer to the shared DB. You already have the implicit interface (`scan() / persist() / send_telegram() / get_latest_X()`) — make it an actual base class with one generic runner parameterized by config, and one notifier.

### 1.4 Shared mutable class-level state, used across threads, no locks — [HIGH]

`DiscountedPremiumScanner._shared_runtime_state` (discount.py:267, 303-338) is a class attribute mutated by every instance. `main.py` starts `DataProvider` pollers in daemon threads that lazily construct `MomentumScanner(self._scanner)` — so the same runtime state (including the rate limiter's `last_api_call_ts`) is read/written from multiple threads with no lock. Two consequences: the rate limiter does not actually serialize calls under threading (two threads can both observe `elapsed >= min_interval` and fire together), and metrics/caches can race. Either make the scanner explicitly single-threaded (and stop sharing it with pollers) or lock the state.

### 1.5 Dead and lying machinery — [MED]

- `main.py:114-120, 237-239` starts 5m/15m `CandlePoller` threads, but **nothing in the discount service ever subscribes** — sonar reads via `intraday_candles()` fallback and never calls `subscribe()`. The pollers spin forever doing nothing. Dead machinery that carries a live bug (§2.4) for the day someone does subscribe.
- Naming lies accumulate: the class is `DiscountedPremiumScanner` but runs a "volatility-only" system; the adapter is `UpstoxDhanAdapter` speaking "Dhan format" for a broker you left; `momentum` and `directional-iv` are "(DISCONTINUED)" in compose yet still imported by live code paths (`data_provider` → `momentum_strategy`; `main.py` imports `directional_iv_runner`). The dead-broker dialect as your internal data contract means every future adapter must emulate Dhan's quirks. Define your own chain/candle dataclasses and translate at the adapter edge.
- Repo hygiene: root contains a WhatsApp JPEG, stale CSVs, `iv_migrated.flag`, `fix_discount.md`, an `old/` directory, and `.venv` checked into the tree the tooling scans. Tests are root-level `test_*.py` mixed with production entry points.

---

## 2. P0-2 — Data pipeline

### 2.1 Your "daily" IV history is not daily, and IV Rank is computed on it — [CRITICAL]

Two independent defects compound:

**(a) The collector's daily row is an opening print, not EOD.** `iv_collector_service._collect_one` (lines 195-214) writes the `data_type='daily'` row on the *first successful intraday fetch of the day* — i.e. between 09:15 and 09:50. Opening IV is systematically elevated (overnight risk unwind, wide quotes at open). Your entire IV Rank / IV Percentile / "CHEAP zone" stack compares *current* IV against a history of *opening* IVs — a structural downward bias in rank that inflates "cheap IV" signals exactly when you shouldn't buy premium. Daily snapshots must be captured in a stable window (e.g. 15:15–15:25) — the comment at line 169-173 even calls them "daily (EOD) snapshots"; the code disagrees.

**(b) The discount service floods the daily history.** `scan_underlying` calls `persist_iv_snapshot` unconditionally (discount.py:2788). With `main.py`'s default scanner (`store_intraday=False`), `data_type` resolves to `'daily'` (line 1588) and the timestamp includes `%H:%M:%S` (line 1629) — so the `UNIQUE(security_id, timestamp, data_type)` constraint never dedups across scans. Result: **every 15-minute scan inserts another 'daily' row per symbol** — up to ~24 per symbol per day. `fetch_historical_iv` (line 889) and `iv_store.get_iv_history` (line 197) then take `tail(252)` **rows**, believing them to be 252 **days**. Your "1-year IV history" is roughly two weeks of intraday noise. Every IV-rank number in the system is statistically meaningless, and the docstring "Persist one ATM IV snapshot per day" (line 1581) is false.

**Fix:** date-level dedup (`WHERE NOT EXISTS same security_id + DATE(timestamp) + 'daily'`), delete the duplicate write path in `discount.py` entirely (the collector already owns this), and rebuild the daily table by collapsing to one row per symbol-day (last print of day) before trusting any IV-rank output again.

### 2.2 Snapshot timestamps are fabricated — cross-sectional consumers get non-simultaneous data — [HIGH]

`run_intraday_pass` stamps *all* ~200 symbols with one `_floor_to_five_minutes()` taken at pass start, then sweeps at 2s/symbol + retries — a 7-10 minute sweep recorded as a single instant. `breadth.py` (market/sector % from spot snapshots), OI-change-vs-day-open, and PCR trend all treat these as synchronous cross-sections. Early-alphabet symbols are 10 minutes staler than the timestamp claims during fast tape — precisely when breadth gates matter. Stamp each row with its actual fetch time and let consumers window, or accept and *document* the skew.

### 2.3 Collector's expiry cache is never invalidated — [HIGH]

`IVCollector._expiry_cache` (iv_collector_service.py:74, 99-122) persists for the process lifetime; the midnight reset (lines 468-472) clears `_pass_log`/`_fail_counts` but **not** the expiry cache. The morning after an expiry date passes, every chain request targets the expired contract; `_collect_one` fails all day (or worse, near-expiry garbage IV gets saved on expiry day itself since the shared `_nse_fno` expiry can be <7 DTE via the `expiries[min(1, ...)]` fallback). The restart-based recovery you presumably rely on is luck, not design. Clear the cache in the midnight reset.

### 2.4 `DataProvider._bulk_intraday` poisons the candle cache — [HIGH] (latent)

The bulk path (data_provider.py:224-288) fetches Upstox *market-quote OHLC snapshots* and caches a **single-row** DataFrame (with an `ltp` column) under the same key where every consumer expects a **multi-candle series** (momentum needs `iloc[-2]`, `prev 5 volume mean`, cumulative VWAP columns). Additionally the bulk results are keyed `"NSE_EQ|token"` while per-instrument subscriptions use whatever ID the strategy passed — two keyspaces in one cache. Currently harmless only because nothing subscribes (§1.5). The first strategy that adopts the pollers gets silently wrong signals, not an error. Delete the bulk path or make it fetch real candle series and unify the keyspace, with a schema assertion in `CandleCache.put`.

### 2.5 Per-row connections, per-insert DDL, N+1 chain fetches — [MED]

- `iv_store.save_snapshot` opens a fresh connection **and runs `PRAGMA table_info` + potential `ALTER TABLE`** on every single insert (lines 112-115). Migration checks belong in `init_db()` once.
- `paper_trader.monitor` prices each open position via `scanner.get_current_option_premium`, which fetches the **entire option chain to read one leg** (discount.py:607-632). Five open trades = five full chain downloads every 5 minutes. Batch by (underlying, expiry).
- `scan_underlying` re-fetches 252 days of daily candles, expiry lists, and expired-option history **per symbol per 15-minute cycle**. HV and trend context do not change intraday — compute once pre-market and cache with a date key. This is most of your scan latency (see §3.1).

### 2.6 Silent data-quality gates — [MED]

`save_snapshot` silently drops rows with `atm_iv < 1 or > 200` (and the `<= 0` clause is redundant against `< 1`); `fetch_historical_iv` silently filters the same band. No unit-consistency assertion at the adapter boundary (`greeks_obj.iv` is assumed percentage). No counter for "rows dropped by sanity gate." When Upstox changes units or a symbol's IV legitimately prints 0.9, you lose data invisibly. Count and alert on rejects.

---

## 3. P0-3 — Core trading engine

### 3.1 One thread runs both the scanner and the exit manager — your SL cadence is fiction — [CRITICAL]

`main.py` uses the `schedule` library: `run_pending()` in a single loop (line 255-257) executes jobs **sequentially in one thread**. The 15-min scan job (`scan_all_fno_stocks`: ~120 symbols × [expiry list + chain + 252d candles + expired-option history] at 7 req/s pacing **plus a hardcoded `time.sleep(1)` per symbol**, discount.py:3030) takes multiple minutes. While it runs, the 5-minute `run_monitor_cycle` — the thing that fires SLs, T1/T2, square-off — **does not run**. The documented design ("OrderManager tracks on its own cadence, independent of the scan") is false in execution: every 15 minutes your exit engine goes dark for the duration of a full scan. On a paper book this quietly biases results; if `AUTO_EXECUTE` ever gates a live path through this loop, it's a capital-loss bug. Fix: separate threads/processes for scan and monitor (the OrderManager already has the right seam), or asyncio.

### 3.2 The Sonar side-flip books trades with the wrong option's prices — [CRITICAL]

`paper_trader.process_signals` (lines 512-537): when Sonar says `BREAKOUT_UP`, a PUT candidate is force-flipped to `side="CALL"` — **but the row's `entry`, `stop_loss`, `t1`, `t2` were computed from the PUT's premium** in `scan_single_strike`. The book then opens a "CALL" whose entry price is the PE's premium; the monitor re-prices the actual CE at that strike, and `apply_tick` immediately fires phantom SL/T1 events against levels that never belonged to this option. Every flipped trade in your paper history is invalid, and it contaminates your strategy statistics. Fix: on flip, either re-price the opposite leg (entry/SL/T1/T2 from the CE quote) or drop the candidate. Never mutate `side` without recomputing the price plan.

### 3.3 The score floor makes your ranking filter decorative — [HIGH]

`score_option` returns `clip_score(40 + raw*0.55, floor=40, ceiling=95)` (discount.py:2160): **every option in the universe scores ≥ 40.** `scan_all_fno_stocks(min_discount_score=40)`'s default filter passes everything; `main.py`'s `min_discount_score=45` corresponds to a raw composite of ~9/100 — barely above the floor. The 40-95 compression also destroys cross-sectional discrimination: an 11-point raw-score difference becomes ~6 points of "score." Remove the affine floor; if you need a display scale, apply it at presentation, not before filtering.

### 3.4 Zero statistical validation anywhere — the weights are vibes — [HIGH]

`score_option`'s weights (0.30/0.40/0.10/0.10/0.20 and the directional variant), the delta-band steps (100/70/25), the relevance-score piecewise magic (`92 - (|x-0.75|/0.55)*42`), composite weights (0.30/0.25/0.20/0.15), momentum ranker bonuses (+40/+20/+30/+10/+5), IV boost ×1.20 / VIX penalty ×0.85 — none of these has a backtest, calibration set, or out-of-sample result anywhere in the repo. The roadmap lists "Backtesting" and "Historical Analysis" as modules; **they do not exist in the tree.** You are running a 10-container system to trade parameters that have never been tested against history you already collect. Before adding a single new scanner: build the replay harness (you persist every input — iv_history, `*_history` tables, paper_trades — replaying gates/scoring over them is days of work, not months) and report hit-rate/expectancy per strategy per gate mode.

### 3.5 The paper fill model biases your P&L estimate — [HIGH]

`apply_tick` + 5-minute LTP sampling: SL fills at exactly `sl` (gap-through slippage ignored — option premiums gap violently, so realized SL losses will be materially worse live), T1/T2 touched *between* samples are missed entirely, entries fill at scanner-time mid with no spread cost (live you pay the ask + impact on stock options with wide books). These errors don't cancel; they're regime-dependent. At minimum: fill SLs at `min(sl, observed_price)`, charge half-spread + a slippage constant on entry/exit, and tag each fill with `sampled` so you can later bound the error. Otherwise the paper book cannot answer the only question it exists to answer.

### 3.6 Every gate fails open, defaults off, and has no health signal — [HIGH]

`pre_market_gate`, `breadth`, `entry_gate`, `auto_exit` are each mode-gated (off/soft/hard) and wrapped in `except: return opportunities` (order_manager.py:243-245, 292-294, 311-313, 412-414). Fail-open is a defensible paper-trading choice, but combined with §0 (corrupted DB) the *entire* gate stack is likely a no-op right now and nothing tells you. There is no metric distinguishing "gate evaluated and passed" from "gate crashed." Emit a per-cycle gate-health line (evaluated / passed / blocked / errored) and alert on `errored > 0`. A risk system whose failure mode is silence is not a risk system.

### 3.7 No portfolio-level risk exists — [HIGH]

Dedup is per (symbol, strike, side); the cap is 5 trades/day. Nothing prevents 5 same-sector, same-direction, high-beta CEs — one correlated bet with 5× the intended risk. Momentum and B&B each carry *their own* `RiskManager` with *their own* capital constant, so combined exposure across strategies is unbounded and unmeasured. You built `sector_mapping.db` for the breadth heatmap — reuse it for a sector/direction concentration cap in `OrderManager.submit_*`, the single choke point you correctly created.

### 3.8 Smaller engine defects — [MED]

- **CycleGate fires on stale data:** watermarks start empty, `_latest()` is an all-time `MAX(timestamp)`, so yesterday's rows satisfy `> ""` and the TradeSuggester fires on the first tick of the day with stale gap/OI/IV-rank inputs (cycle_gate.py:52-55). Scope `_latest` to today.
- **Momentum regime is premarket-frozen:** `_regime_cache` computed at 09:00 is trusted until EOD; a stock that breaks its daily EMA structure at 11:00 still passes the regime gate.
- **Auto-exit depends on another container being alive:** `_auto_exit_on_oi_contradiction` reads `oi_buildup_history`; if the oi-buildup scanner is down (§1.2 profile confusion), auto-exit silently never fires. No staleness check on the row it reads.
- **Wasted last scan:** scans are scheduled to 15:15 but `no_entry_after` is 15:00 — the 15:00/15:15 scans burn a full API sweep to book nothing.
- **`fetch_with_retry` validator loophole:** if the validator fails but the response has a `"data"` key, it's returned anyway (discount.py:359-361) — the validator is advisory, which defeats its purpose.

---

## 4. What to do, in order

1. **Today:** WAL + busy_timeout everywhere; restore/rebuild `iv_history.db`; add `quick_check` alarm. (§0)
2. **This week:** kill `discount.py`'s direct `iv_history` writes; fix daily-row dedup + move daily capture to EOD window; rebuild daily IV history; fix the Sonar flip bug; split scan and monitor onto separate threads. (§2.1, §3.1, §3.2)
3. **Next:** remove the score floor; add gate-health metrics; portfolio concentration cap in OrderManager; clear expiry cache at midnight; delete or fix `_bulk_intraday`. (§3.3, §3.6, §3.7, §2.3, §2.4)
4. **Then, before ANY new scanner:** the replay/backtest harness over the data you already persist. Every hand-tuned weight is unvalidated until this exists. (§3.4, §3.5)
5. **Structural (2-3 weeks, incremental):** extract `broker.py` / `iv_math.py` / persistence from `discount.py`; one generic scanner runner + base class; one notifier; one compose file + override; make CLAUDE.md match reality. (§1.1-1.3)

The strongest thing in this codebase is the `OrderManager` seam and the pure-function discipline in the newer scanners (`oi_contradicts`, `score_symbol`, `apply_tick` are properly unit-testable). The refactor plan's instincts (L2 DataProvider, L4 OrderManager) are right. The execution gap is that the old god module was never actually decomposed — it was wrapped — and the data layer's single-writer contract was never enforced. Those two facts, not any individual scanner, are what's currently costing you correctness.
