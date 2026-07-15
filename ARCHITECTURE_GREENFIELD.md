# Greenfield Architecture — NSE F&O Autonomous Trading Platform

**Status:** Design proposal (clean-slate). Written 2026-07-11.
**Constraint:** Python, solo-operable, Docker + SQLite-class infra. No Kafka, no k8s.
**Horizon:** 10 years of evolution without a rewrite.

This is not a refactor plan for the current tree. It is the architecture I would build today, keeping only the concepts that earned their place.

---

## 1. What the current system taught us

Three years of accretion produced 12 containers, but the lessons inside them are the real asset:

**Concepts that deserve to survive:**

| Concept | Where it lives today | Why it survives |
|---|---|---|
| Sole-writer contract | iv-collector owns `iv_history` | Generalizes into the core idea of this design: a single-writer event log |
| Conviction fusion | Convex V2 engine/ package | Scanners as evidence, one fusion point → the correct model; V2 got this right |
| Central paper book + risk caps | OrderManager, daily-loss guard | One choke point for risk is non-negotiable; keep and harden |
| `AUTO_EXECUTE` gating | env var | Becomes a first-class run-mode enum: `backtest / paper / live` |
| Zero-API scanners | 8 scanners read `iv_history.db` only | Separating data acquisition from computation was the right instinct |
| Broker shim | `UpstoxDhanAdapter` | The instinct (ports & adapters) was right; the execution (Dhan-shaped internal contract) was wrong |
| IST discipline, WAL + busy_timeout | everywhere | Keep, but bury inside infrastructure so strategy code never sees it |

**Diseases the new design must make structurally impossible:**

The 12-container layout turned functions into services — every scanner is a process with its own scheduler, config module, and Telegram notifier class, when each is really a pure function over shared data. Gates and vetoes accreted as scattered ifs (breadth gate, sonar veto, composite gate, Gate-5 exemption, OI-contradiction exit) with no single place where "why did we (not) trade?" is answerable. Config is fragmented per strategy (`ORB[...]`, `BB_RISK[...]`, hardcoded 1.3 volume ratios). The internal data contract is a broker's response shape. And the biggest gap: **no backtest harness** — strategy logic is entangled with `schedule`, live API calls, and wall-clock time, so nothing can be validated before it trades.

---

## 2. Design principles

1. **One event log is the system.** Every market snapshot, signal, decision, veto, order, and fill is an immutable event in an append-only store. Everything else — positions, P&L, dashboards, alerts — is a derived view that can be rebuilt from the log.
2. **Strategies are pure functions.** `decide(features, portfolio, params) -> list[Intent]`. No I/O, no clock reads, no scheduling, no broker types. This single rule is what makes backtest/paper/live use *identical* code.
3. **One decision point, one risk point, one execution point.** Evidence fuses in one engine; every intent passes one risk kernel; every order exits through one execution gateway.
4. **Backtest is replay.** The backtester is not a parallel implementation — it is the live engine fed from the event log with a simulated clock and a fill simulator. Parity by construction, not by discipline.
5. **Research is a pipeline, not a vibe.** A strategy moves research → backtest → paper → live only by passing explicit statistical criteria, and every promotion is an event in the log.
6. **Boring infrastructure.** SQLite for hot state, Parquet + DuckDB for the research lake, 3 containers, systemd-style supervision. Complexity budget is spent on correctness, not distributed systems.

---

## 3. System overview

```
                        ┌──────────────────────────────────────────────┐
                        │                 RESEARCH PLANE                │
                        │  Parquet lake · DuckDB · backtest = replay    │
                        │  walk-forward harness · experiment registry   │
                        └───────────────▲──────────────────────────────┘
                                        │ nightly export
┌───────────────┐   canonical    ┌──────┴───────┐   scored     ┌─────────────┐
│  DATA PLANE   │   snapshots    │ DECISION      │   intents    │ EXECUTION   │
│  (1 process)  ├───────────────▶│ PLANE         ├─────────────▶│ PLANE       │
│               │                │ (1 process)   │              │ (1 process) │
│ Upstox MD     │  event log     │ features →    │  event log   │ risk kernel │
│ adapter,      │  (writer #1)   │ detectors →   │  (writer #2) │ order gw    │
│ bhav/VIX/     │                │ fusion →      │              │ paper/live  │
│ deals feeds   │                │ strategies    │              │ (writer #3) │
└───────────────┘                └──────┬────────┘              └──────┬──────┘
                                        │            derived views     │
                                 ┌──────▼──────────────────────────────▼──────┐
                                 │ OBSERVATION PLANE: notifier (Telegram),    │
                                 │ dashboard, audit — all read-only consumers │
                                 └─────────────────────────────────────────────┘
```

Four planes, three writer processes (one per plane that produces events), each owning disjoint tables — the sole-writer contract generalized. Three Docker containers (`data`, `engine`, `execution`) plus an optional `dashboard`. Not twelve.

---

## 4. The event log

The spine is a single SQLite database (`events.db`, WAL) with one logical schema:

```sql
CREATE TABLE events (
  seq        INTEGER PRIMARY KEY,          -- global order
  ts_utc     TEXT NOT NULL,                -- event time
  kind       TEXT NOT NULL,                -- see taxonomy
  stream     TEXT NOT NULL,                -- e.g. 'md.chain.NIFTY', 'strategy.orb'
  payload    TEXT NOT NULL,                -- JSON, schema-versioned
  schema_ver INTEGER NOT NULL,
  config_hash TEXT                         -- config that produced it (decisions+)
);
```

Event taxonomy (closed set, versioned): `md.snapshot`, `md.candle`, `md.iv`, `md.oi`, `feature.computed`, `evidence.emitted`, `conviction.scored`, `intent.proposed`, `risk.approved` / `risk.vetoed` (with machine-readable reason), `order.submitted` / `order.filled` / `order.rejected`, `position.opened` / `position.closed`, `guard.tripped` (daily-loss lockout, kill switch), `strategy.promoted` / `strategy.demoted`.

Two properties fall out immediately. First, **auditability**: "why didn't we take that trade?" is a query, not an archaeology dig — the veto event names the gate and the values that tripped it. Second, **replayability**: a day's `md.*` events piped through the engine must reproduce the day's `intent.*` events bit-for-bit (this is a CI test, see §10).

Hot tables (positions, open orders, today's features) are materialized views maintained by their owning process, always rebuildable from the log. Nightly, events older than N days are exported to partitioned Parquet (`lake/kind=md.candle/date=2026-07-11/…`) and queried with DuckDB — that is the research lake. SQLite stays small and fast forever.

---

## 5. Data plane

One process. It owns every network call to market-data providers and is the only writer of `md.*` events.

The internal market-data schema is **canonical and broker-neutral** — dataclasses like `OptionChainSnapshot`, `Candle`, `Quote` defined by us, never a provider's JSON shape. Providers plug in behind a `MarketDataPort` protocol:

```python
class MarketDataPort(Protocol):
    def option_chain(self, underlying: str, expiry: date) -> OptionChainSnapshot: ...
    def candles(self, instrument: Instrument, interval: Interval,
                start: datetime, end: datetime) -> list[Candle]: ...
    def expiries(self, underlying: str) -> list[date]: ...
```

`UpstoxAdapter` implements it today; adding a second provider (or a redundant fallback) is a new adapter, zero changes upstream. Rate limiting, retry, token refresh, and instrument-master resolution (today's `ScripMasterLotSizer`, generalized to an `InstrumentService` owning lot sizes, strike gaps, tick sizes, expiry calendars) all live here and nowhere else.

The plane runs a sweep scheduler equivalent to today's iv-collector plus the bhav/deals/VIX collectors, but emitting events instead of writing bespoke tables.

---

## 6. Decision plane — the Conviction Engine

This is the V2 idea, made the *only* way anything trades. One process, one loop: on each tick of its cadence it builds features, runs detectors, fuses evidence, invokes strategies, and emits intents.

**Feature layer.** Pure functions from event-log windows to a typed `FeatureFrame` per instrument: EMAs, ADX, VWAP (computed once, here — not per strategy), opening range, yesterday's levels, IV rank, IV skew, OI deltas, breadth (market and sector), delivery %, regime classification. Features are computed once per tick and shared; every scanner that today recomputes candles becomes a consumer of this layer. Each computed feature set is an event, so research sees *exactly* what live saw.

**Detector layer.** Today's eight scanners become stateless detectors: `detect(frame) -> list[Evidence]` where `Evidence = (source, direction, score 0-100, ttl, details)`. IV-rank, OI-buildup, gap, delivery-surge, smart-money, sonar, composite — each is now ~a file, not a container. New detector = new function registered in a list.

**Fusion layer.** One place converts evidence into per-instrument, per-direction `Conviction` scores. All gates and vetoes live here as **declarative policy rules** evaluated in order, each producing an event when it fires:

```python
POLICIES = [
    RequireBreadthAlignment(min_pct=60),
    SonarVeto(),                       # regime contradiction
    OIContradictionExit(mode="soft"),  # also drives auto-exit intents
    TimeWindow(entry_after="09:30", cutoff="11:30"),
    LiquidityFloor(min_oi=..., min_volume=..., max_spread_pct=...),
]
```

No more Gate-5 exemptions hidden in code paths — exemptions are policy parameters, visible in one file, logged when applied.

**Strategy layer.** Strategies are pure and tiny because everything hard moved down a layer:

```python
class Strategy(Protocol):
    id: str
    def decide(self, frame: FeatureFrame, conviction: Conviction,
               book: PortfolioView, p: StrategyParams) -> list[Intent]: ...
```

ORB, VWAP-reclaim, and Break-and-Bounce each become 50–100 lines of entry logic. An `Intent` says *what and why* (`instrument, direction, conviction, sl_spec, target_spec, tag`), never *how much* — sizing belongs to the risk kernel. Strategy state machines (B&B's breakout→retest lifecycle) are explicit, serialized state objects, rebuilt from the log on restart.

**Clock.** The engine never calls `datetime.now()`. A `Clock` is injected: `LiveClock` in production, `ReplayClock` in backtest. This one seam is what makes §9 possible.

---

## 7. Execution plane — risk kernel and order gateway

One process, the only writer of `risk.*`, `order.*`, `position.*` events, and the only component that knows brokers can execute.

**Risk kernel** (evolution of today's OrderManager + paper-book caps, now the single mandatory gate):

- Sizing: converts an Intent's conviction + SL distance into lots (`floor(risk_budget / (premium × sl_pct × lot_size))`), centrally — no strategy computes its own size.
- Portfolio caps: max exposure, per-symbol/day limits, per-strategy budgets, correlation/sector concentration caps.
- Circuit breakers: daily-loss lockout (today's RISK-1, now always-on architecture with a config threshold), consecutive-loss cool-off, stale-data guard (no fresh `md.*` events → no new entries), and a manual kill switch that is itself an event.
- Every rejection emits `risk.vetoed` with a machine-readable reason.

**Order gateway** behind an `ExecutionPort`:

```python
class ExecutionPort(Protocol):
    def submit(self, order: Order) -> BrokerOrderId: ...
    def cancel(self, id: BrokerOrderId) -> None: ...
    def positions(self) -> list[BrokerPosition]: ...
```

Three adapters: `PaperExecution` (fill simulator with configurable slippage/latency), `DhanExecution` (live, when that day comes), `BacktestExecution` (deterministic fills from replayed quotes). The run mode — `backtest | paper | live` — selects the adapter; it is the honest version of `AUTO_EXECUTE`, and mode is stamped on every event. The bracket-safety pattern survives: entry + protective SL as an atomic pair, emergency flatten + alert if the SL leg fails. Reconciliation loop: broker positions are polled and diffed against the book; any mismatch trips a guard event and halts new entries.

---

## 8. Configuration

One `pydantic-settings` tree, layered: package defaults → `config.yaml` → environment overrides. Strategy parameters are versioned value objects (`StrategyParams`), not module-level dicts; nothing numeric is hardcoded (the 1.3 volume ratio dies here). The full resolved config is hashed at startup and the hash is stamped on every decision event — so any historical trade can be reproduced with the exact parameters that made it. Changing live params is a config commit, not an env fumble.

---

## 9. Research plane — the reason to rebuild

This plane is what "research-driven" means and it is the largest departure from today.

**Backtesting by replay.** `backtest run --from 2026-01-01 --to 2026-06-30 --strategy orb --params v12.yaml` streams historical `md.*` events from the Parquet lake through the *same* engine binary with `ReplayClock` + `BacktestExecution`. There is no second implementation of any indicator, gate, or strategy. Costs are modeled explicitly (brokerage, STT, slippage curves by spread) — the costs-sentinel lesson, built in.

**Walk-forward validation.** Parameter selection runs on rolling in-sample windows and is scored only on the adjacent out-of-sample window. Overfit detection (deflated Sharpe, parameter-sensitivity heatmaps) is part of the standard report, not an afterthought.

**Experiment registry.** Every backtest run is recorded: params hash, data range, code version, metrics (PF, hit rate, max DD, tail stats, per-regime breakdown). Comparing "v12 vs v13 of ORB" is a query.

**Promotion pipeline.** A strategy's lifecycle is a state machine with explicit, logged transitions:

```
research ──(walk-forward passes thresholds)──▶ backtest-approved
   ──(N weeks paper, live-vs-backtest tracking error < ε)──▶ live-eligible
   ──(manual arm + risk budget assignment)──▶ live
   ──(drawdown/decay triggers)──▶ demoted (automatic)
```

Paper trading is not a mode you leave on forever out of caution — it is a measured stage whose exit criteria are defined before entry. Decay monitoring runs continuously on live strategies: rolling live PF vs backtest expectation; sustained divergence auto-demotes to paper and alerts.

---

## 10. Testing and observability

**Testing contract.** Strategies and detectors are pure, so they get exhaustive unit tests with synthetic frames. Adapters get contract tests against recorded fixtures (a `MarketDataPort` conformance suite any new adapter must pass). The crown jewel is the **golden replay test** in CI: a frozen sample week of events must produce byte-identical decision events on every commit — any unintended behavior change fails the build. Risk kernel gets property-based tests (no sequence of intents may ever breach a cap).

**Observability.** The event log *is* the telemetry. One notifier service (replacing N Telegram classes) subscribes to the log with routing rules — signals, fills, and guard events to Telegram; everything to the dashboard. The dashboard is a read-only consumer rendering conviction heatmaps, the book, veto reasons, and strategy-lifecycle status. A daily digest (P&L, veto histogram, decay metrics) is generated from queries, not bespoke code.

---

## 11. Repository and deployment shape

```
platform/
  core/          # events, clock, config, instrument service, schemas
  data/          # MarketDataPort, upstox adapter, sweep scheduler, collectors
  features/      # pure feature computations
  detectors/     # one file per detector (ex-scanners)
  engine/        # fusion, policies, strategy protocol, strategies/
  execution/     # risk kernel, ExecutionPort, paper/live/backtest adapters
  research/      # replay backtester, walk-forward, experiment registry, lake export
  observe/       # notifier, dashboard, digests
  tests/         # unit, contract, golden-replay fixtures
docker-compose.yml   # 3 services: data, engine, execution (+ dashboard)
config.yaml
```

One Python package, one dependency lockfile, one version. Processes communicate only through the event log (SQLite WAL handles single-host multi-process fine at this cadence; all access via one `connect()` helper — the hard-won lesson kept). Deployment is `docker compose up` on one box. State backup is one DB file plus a Parquet directory (`VACUUM INTO` for the live copy — lesson kept).

**10-year escape hatches, all behind existing seams:** SQLite events → Postgres/Timescale by swapping the event-store module; polling sweeps → websocket streams by swapping the data-plane scheduler (event schema unchanged); new brokers → new adapters; ML-based sizing or conviction models → new fusion implementation consuming the same Evidence stream; multi-account → execution-plane concern only. Nothing above the seam moves.

---

## 12. Build order (if this were greenfield today)

Phase 1 — spine: `core/` (events, clock, config), data plane with Upstox adapter, lake export. The system records the market and does nothing else. *Everything after this is testable against recorded reality.*

Phase 2 — replay: backtester over the recorded events, feature layer, one strategy (ORB) end-to-end in backtest. Golden replay test lands in CI here.

Phase 3 — paper: execution plane with `PaperExecution`, risk kernel, notifier. ORB runs live-paper; live-vs-backtest tracking error measured from day one.

Phase 4 — breadth: port remaining strategies and detectors (each is now small), fusion policies, dashboard, promotion pipeline.

Phase 5 — live: Dhan execution adapter, reconciliation, manual arm ceremony.

The current system keeps trading throughout — this is a parallel build fed by the same market, cut over strategy-by-strategy via the promotion pipeline itself.

---

## Appendix: explicit non-goals

No microservices, no message brokers, no cloud-managed anything, no multi-asset ambitions (NSE F&O only until the research plane proves an edge worth generalizing), no HFT latency targets — the platform's cadence is minutes, and the architecture spends its complexity budget on *reproducibility and research velocity*, because for an options-buying system the edge lives in validated strategy quality, not in speed.
