# Architecture & Refactor Plan — Dhan/Upstox F&O Bot

**Goal:** segregate the system into clean layers — **data collectors → one data loader → scanners → order management** — so every scanner runs off a *single* shared data load instead of each file fetching its own.

---

## 1. What exists today (file inventory, segregated)

### Broker / adapter layer
| File | Role |
|---|---|
| `upstox_adapter.py` | `UpstoxDhanAdapter` — the real broker surface (`option_chain`, `expiry_list`, `historical_daily_data`, `intraday_minute_data`, `place_order`, `MARKET`, `SL_M`). The one true broker client. |
| `token_manager.py`, `upstox_token_manager.py`, `upstox_login.py`, `init_upstox_token.py` | Token refresh / login. |
| `instrument_mapper.py`, `load_scrip_master_sqlite.py`, `complete_json_tosqlite.py` | Instrument-key lookups against `complete.db` / scrip master. |

### Data collectors (already centralized → write to `iv_history.db`)
| File | Writes |
|---|---|
| `collectors/iv_collector_service.py` | Orchestrator — sweeps the F&O universe, pulls option chains, persists ATM IV/OI snapshots. Also schedules the three collectors below. |
| `collectors/iv_store.py` | **The DB I/O module** for `iv_history.db` (read + write). |
| `collectors/bhav_collector.py` | Bhavcopy / delivery data (`delivery_daily`). |
| `collectors/deals_collector.py` | Bulk/block deals. |
| `collectors/vix_collector.py` | India VIX. |
| `collectors/notify.py` | Collector Telegram. |

### Scanners — **already decoupled** ("zero-API", read `iv_history.db` only) ✅
| File | Reads from |
|---|---|
| `iv_rank_scanner.py` | `iv_store` + `pd.read_sql` |
| `oi_buildup_scanner.py` | `iv_store.DB_PATH` |
| `gap_scanner.py` | `iv_store.DB_PATH` |
| `delivery_surge_scanner.py` | `iv_store.DB_PATH` (delivery data) |
| `smart_money_scanner.py` | `iv_store.DB_PATH` (deals) |

These are the model. Each has a matching `*_config.py` and `*_runner.py` (thin scheduler). **This is exactly the pattern you want everywhere.**

### Strategies — **the problem area** (each loads its own data live) ⚠️
| File | Loads data via |
|---|---|
| `discount.py` (133 KB) | `DiscountedPremiumScanner` — builds its own adapter, fetches chain + daily candles + IV. |
| `momentum_strategy.py` | Creates its **own** `DiscountedPremiumScanner()`, then `MomentumRegimeFilter.get_daily_candles` + `MomentumScanner.get_intraday_candles` (re-implements candle fetch). |
| `break_bounce_strategy.py` | Creates **another** `DiscountedPremiumScanner()`, reuses momentum's intraday fetcher. |
| `directional_iv_strategy.py` | `DiscountedPremiumScanner(upstox_adapter=…)`. |
| `main.py` | `DiscountedPremiumScanner()` for the discount paper-trading loop. |

### Order management — **duplicated in 3 places** ⚠️
| File | Path |
|---|---|
| `momentum_strategy.py:1039` `_place_order` | market BUY → `SL_M` SELL → emergency exit; reads `AUTO_EXECUTE`. |
| `break_bounce_strategy.py:742` `_place_order` | **near-identical copy** of the above. |
| `paper_trader.py` | Separate paper engine used by `main.py`/discount. |

### Orchestration
`docker-compose.yml` runs **one collector + N strategy/scanner services**, each its own container sharing the `shared-data` volume (`iv_history.db`, scrip master, tokens).

---

## 2. The root problem

`DiscountedPremiumScanner` is a **god-object**. It bundles: broker adapter + option-chain fetch + candle fetch + IV math + F&O list + runtime cache + Telegram config — all in one 133 KB module.

Because every strategy instantiates **its own copy**:

- Each service opens its **own** broker connection and reloads the token.
- The same **daily candles for the whole F&O universe** are fetched separately by momentum, break-bounce, and discount.
- Candle-fetch logic is **re-implemented** (`MomentumRegimeFilter` vs `MomentumScanner` vs `discount.historical_daily_data`).
- The class-level `_shared_runtime_state` cache only helps *within one process* — it does nothing across services.

Net: redundant API calls, rate-limit pressure, drift between copies, and no single place to add a new scanner that "just reads the data."

---

## 3. Target architecture (layered)

```
┌──────────────────────────────────────────────────────────────┐
│  L0  BROKER ADAPTER          upstox_adapter.py + token mgmt    │  ← only layer that talks to Upstox
└──────────────────────────────────────────────────────────────┘
                 │ (writes)                       ▲ (read API)
                 ▼                                │
┌──────────────────────────────────────────────────────────────┐
│  L1  DATA COLLECTORS         collectors/*  → iv_history.db     │
│      IV/OI · bhav/delivery · deals · vix · CANDLES · CHAIN     │  ← the ONE place that fetches market data
└──────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────┐
│  L2  DATA LOADER (NEW)       market_data.py  — DataProvider    │  ← the "1 data loader"
│      one read API over the store + in-process per-tick cache   │     fetch-once-serve-all
└──────────────────────────────────────────────────────────────┘
        │                  │                    │
        ▼                  ▼                    ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────────────┐
│ L3 SCANNERS   │  │ L3 STRATEGIES │  │  (pure signal logic,  │
│ iv_rank, oi,  │  │ momentum,     │  │   NO broker / NO DB)  │
│ gap, deliv,   │  │ break_bounce, │  └───────────────────────┘
│ smart_money   │  │ discount, …   │
└───────────────┘  └───────┬───────┘
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  L4  ORDER MANAGEMENT        order_manager.py (live)           │
│      single AUTO_EXECUTE gate · BUY→SL_M→emergency · paper     │
└──────────────────────────────────────────────────────────────┘
                           ▲
┌──────────────────────────────────────────────────────────────┐
│  L5  RUNNERS / SCHEDULERS    *_runner.py, main.py (thin)       │
└──────────────────────────────────────────────────────────────┘
```

**The rule:** only **L1 collectors** call the broker for *market data*; only **L4** calls the broker for *orders*. Everything in L3 reads through the **L2 DataProvider** and emits signals. Adding a scanner = drop a file in L3 that takes a `DataProvider` and returns signals. No new fetch code, ever.

---

## 4. The "one data loader" — `market_data.py` (L2)

A single `DataProvider` class is the only thing scanners/strategies import for data. Two backends behind one interface:

```python
class DataProvider:
    """Single read API for all market data. Scanners never touch the broker or DB."""
    def __init__(self, store=iv_store, adapter=None, cache=True): ...

    # --- served from iv_history.db (collector-written) ---
    def latest_iv(self, security_id): ...
    def iv_history(self, security_id, days): ...
    def delivery(self, symbol): ...
    def deals(self, symbol): ...
    def vix(self): ...

    # --- candles & chain: cached-per-tick, fetched once ---
    def daily_candles(self, security_id, segment, days=60): ...   # @lru/tick-cache
    def intraday_candles(self, security_id, segment, interval=15): ...
    def option_chain(self, security_id, segment, expiry): ...
    def expiry(self, security_id, segment): ...

    def begin_tick(self): self._cache.clear()   # one fetch serves every scanner this tick
```

Two ways to make "all scanners run off one load" real — pick per deployment:

**(A) Shared-process (simplest win):** run scanners that share a cadence in **one** process with **one** `DataProvider`. `begin_tick()` clears the cache each scan; the first scanner that asks for `daily_candles(X)` fetches it, the rest get the cached frame. Collapses N fetches → 1.

**(B) Collector-fed (fully decoupled, recommended end-state):** move candle + chain fetching **into the collector tier** so `daily_candles`/`intraday_candles`/`option_chain` read from `iv_history.db` like the IV scanners already do. Then every strategy becomes "zero-API" too, and multi-container deployment keeps working with no cross-service API duplication.

Start with (A) for an immediate ~3× reduction in candle/chain calls; migrate hot paths to (B) over time.

---

## 5. Order management consolidation (L4)

Extract the duplicated logic into one module:

```python
# order_manager.py
class OrderManager:
    def __init__(self, adapter, auto_execute=None): ...
    def enter(self, contract, lots, sl_price, targets): ...   # BUY → SL_M → emergency-exit
    def square_off(self, position): ...

class PaperOrderManager(OrderManager):   # same interface, writes to paper_trader book
    ...
```

`momentum_strategy._place_order` and `break_bounce_strategy._place_order` both call `OrderManager.enter(...)`. The `AUTO_EXECUTE` check, the BUY→`SL_M`→emergency-sell sequence, and Telegram-on-failure live **once**. `paper_trader.py` becomes the `PaperOrderManager` backend so live vs. paper is a single swap, not separate code paths.

---

## 6. Migration plan (incremental, low-risk)

Each phase is shippable on its own; nothing is a big-bang rewrite.

**Phase 1 — Carve the data API out of the god-object.**
Create `market_data.py` with `DataProvider`. Move `get_option_chain` / `get_expiry` / candle fetchers out of `DiscountedPremiumScanner` (or wrap them). Keep `DiscountedPremiumScanner` working by delegating to `DataProvider` internally — no behaviour change yet.

**Phase 2 — Point strategies at the shared provider.**
Replace each `DiscountedPremiumScanner()` instantiation (momentum, break_bounce, directional_iv, main) with an **injected** `DataProvider`. Delete `MomentumRegimeFilter.get_daily_candles` / `MomentumScanner.get_intraday_candles` and call `provider.daily_candles` / `provider.intraday_candles`. One candle-fetch implementation remains.

**Phase 3 — Add per-tick caching (pattern A).**
Add `begin_tick()` + cache to `DataProvider`. Where runners scan multiple strategies, share one provider and call `begin_tick()` once per cycle.

**Phase 4 — Unify order management.**
Introduce `order_manager.py`; route both `_place_order` copies and `paper_trader` through it.

**Phase 5 — (optional) push candles/chain into the collector tier (pattern B).**
Add a `candle_collector` / chain snapshot to `collectors/` writing to `iv_history.db`; flip `DataProvider.daily_candles` etc. to read the store. Strategies become zero-API.

**Phase 6 — Cleanup.**
Shrink `discount.py`: signal logic stays, data + order plumbing moves to L2/L4. Archive `old/` (already a graveyard) and dead configs.

---

## 7. Target directory shape

```
broker/        upstox_adapter.py, token_manager.py, instrument_mapper.py
collectors/    iv_collector_service.py, iv_store.py, bhav/deals/vix, (candle_collector)
data_layer/    market_data.py   ← DataProvider (the 1 loader)
scanners/      iv_rank, oi_buildup, gap, delivery_surge, smart_money   (+ new ones drop in here)
strategies/    discount, momentum, break_bounce, directional_iv        (pure signal logic)
execution/     order_manager.py, paper_trader.py
runners/       *_runner.py, main.py   (thin schedulers)
config/        *_config.py
```

---

## 8. Composite Conviction Scanner (the "one scanner over all data")

Built and shipped: `composite_scanner.py` + `composite_config.py` + `composite_runner.py` + `test_composite.py` (6/6 pass), plus a `composite` service in `docker-compose.yml`.

It fuses the five zero-API scanners into **one ranked, direction-aware conviction score** per stock. It reads only the persisted `*_history` tables the others already write — **zero broker calls** — and is fail-open (a missing/broken factor degrades gracefully).

**Factors & roles:**

- Directional votes (each emits CE/PE × strength × weight): OI buildup `0.30`, smart-money deals `0.25`, delivery surge `0.20`, gap `0.15`.
- IV rank = cost gate (CHEAP boosts ×1.20, EXPENSIVE penalises ×0.75) — not a vote.
- VIX = market regime (ELEVATED ×0.85, CALM ×1.10).
- Confluence bonus: +10% if 3 factors agree, +20% if all 4 — the real edge.

**Score** = normalised net directional weight → 0–100, graded STRONG / MODERATE / WEAK. Needs ≥ `MIN_FACTORS` (2) aligned votes to rank.

**Cadence (forced by the data, not preference):** `delivery_daily` and `deals` only exist after the close, so the authoritative run is **EOD ~20:15 / 22:45** (`CMP_SCAN_TIMES`) — once all factors are fresh — producing a next-session conviction list. An optional intraday read (`CMP_INTRADAY_TIMES`) fuses only the live factors (IV/OI/gap) with yesterday's delivery/deals as a static overlay.

**Run it:** `docker compose --profile composite up -d composite` (or `python composite_runner.py` locally). Any strategy can gate on it via `composite_scanner.get_latest_composite(security_id)`.

> Data-depth caveat: `deals`/`vix_daily` currently hold ~1 day; percentile-style factors need a few weeks of accumulation before weights are well-calibrated. The wiring is correct now; the signal sharpens as history builds.

---

## 9. Composite entry gate — IMPLEMENTED (default off)

Built: `entry_gate.py` + `entry_gate_config.py` + `test_entry_gate.py` (6/6).
`passes(security_id, side)` with `GATE_MODE` = off (default, no behaviour change) /
soft (annotate) / hard (block direction-mismatch or weak/low-score). Wired opt-in
into `OrderManager.submit_signals` (`_apply_entry_gate`, active only in hard mode),
and callable from any strategy. Fail-open when no composite row exists.

Original design below.

Goal: turn the six alert-only scanners from standalone notifications into the
*filter* that decides which strategy triggers are worth taking. The three-question
pipeline: **what/which-way** (scanners) → **when** (strategy trigger) → **how to
exit/size** (OrderManager).

**Where it plugs in.** Each trade-picker already has a single booking call:
`paper_trader.process_signals` (discount) and `_place_order` (momentum / break-bounce).
The gate is one check *before* booking, reusing the existing zero-API helper:

```python
from composite_scanner import get_latest_composite

def passes_composite_gate(security_id, trade_side, cfg):
    c = get_latest_composite(security_id)        # {} if no composite row yet
    if not c:
        return cfg.ALLOW_IF_NO_COMPOSITE          # fail-open vs fail-closed (config)
    if c["grade"] == "WEAK":
        return False
    if c["score"] < cfg.MIN_GATE_SCORE:           # e.g. 45
        return False
    return c["direction"] == trade_side           # CE setup needs CE conviction
```

**Rule:** book only if `trigger ∧ composite.direction == trigger.side ∧
composite.grade ≥ MODERATE ∧ score ≥ MIN_GATE_SCORE`.

**Modes (config-driven, so it's reversible):**

- `GATE_MODE = "off"` — current behaviour, no gating (default until you trust it).
- `GATE_MODE = "soft"` — don't block; just annotate the alert with the composite
  score and add it to the rank (composite becomes a tie-breaker / sizing input).
- `GATE_MODE = "hard"` — block trades that fail the gate.

**Important caveats before turning it on:**

1. **Cadence mismatch.** Composite is EOD-primary; intraday triggers would gate on
   *yesterday's* composite. Either run the intraday composite slots, or treat the
   EOD composite as a directional *bias* only (soft mode) for next-day entries.
2. **Data depth.** `deals`/`vix` need a few weeks of history before the score is
   trustworthy — start in `soft` mode, watch the alerts, then graduate to `hard`.
3. **Fail-open default.** `ALLOW_IF_NO_COMPOSITE = True` so a missing composite row
   never silently halts all trading.

No strategy code is changed yet — this is the spec for your review.

---

## 10. Discount cadence + OrderManager split (IMPLEMENTED)

- `discount_config.py`: `scan_interval_min` 5 → **15**; added `monitor_interval_min = 5`.
- `order_manager.py` (new): `OrderManager` owns the trade book + open-position
  lifecycle. `submit_signals()` intakes booked trades; `track()` re-prices and
  exit-manages **all** open positions; `square_off_all()` / `eod()` for close.
  Delegates to the unit-tested `paper_trader` engine — no exit logic duplicated.
- `main.py`: the fused 5-min cycle is split into a **15-min scan** (`run_scan_cycle`
  → find + submit to OrderManager) and an independent **5-min track**
  (`run_monitor_cycle` → OrderManager manages open positions). Square-off and EOD
  now route through the OrderManager.

Net effect: the scanner finds trades every 15 min; once booked they're handed to
the OrderManager, which watches every open position every 5 min regardless of the
scan clock. This is the small-scale seed of the L4 OrderManager — momentum and
break-bounce can later register their positions with the same object to retire
their duplicated `_place_order` paths.

---

## 11. Intraday Trade Suggester (IMPLEMENTED, soft/rank-only)

`trade_suggester.py` + `trade_suggester_config.py` + `test_trade_suggester.py` (5/5 pass).

The alert scanners give a *bias on the underlying* but no contract; discount gives
a *contract* but ignores the other scans. The suggester fuses them: discount's
orderable setups are the candidates, and each is re-ranked by how strongly gap +
oi_buildup + smart_money + delivery_surge agree with the trade's direction (iv_rank
and VIX as cost/regime modifiers).

`suggestion_score = discount_score * (1 + agree_sum * CONFLUENCE_GAIN)`, where
`agree_sum ∈ [-1,1]`. **Soft:** nothing is filtered — agreement only re-orders and
scales, so an unsupported discount trade still appears, just lower. Zero broker
calls (reads the persisted `*_history` tables, fail-open).

Wired into `main.py.run_scan_cycle`: after each discount scan books to the
OrderManager, it emits a ranked suggestion alert + `data/trade_suggestions.csv`.

> Refinement requested (see §13): fire the suggester **once per completed scan
> cycle**, not on every discount tick.

---

## 12. Continuous candle pollers — IMPLEMENTED

Built: `data_provider.py` (CandleCache, CandlePoller, DataProvider) +
`test_data_provider.py` (5/5). 5-min and 15-min pollers fetch each subscribed
instrument once per interval; `subscribe`/`unsubscribe`/`move` manage membership
(B&B's 15m→5m move covered by a test). Break-and-Bounce now reads candles through
the DataProvider with a direct-fetch fallback, so behaviour is unchanged until the
pollers are started (`provider.start()`), which flips it to fetch-once mode.
Deployment model (in-process threads vs a shared candle service) is still open.

Original design below.

Your insight: the L2 data loader shouldn't fetch on demand per strategy — it
should run **continuous background pollers** that own candle fetching, and
strategies just **subscribe/unsubscribe instruments**.

**Two pollers, keyed by interval:**

- **5-min poller** — wakes ~30s after each 5-min candle close, fetches the latest
  5-min candle for every *subscribed* instrument once, writes to the candle cache.
- **15-min poller** — same, on the 15-min boundary.

Both fetch **once per instrument per interval**, regardless of how many strategies
need it — this is the single-fetch goal made continuous.

```python
class CandlePoller:
    interval_min: int
    subscribers: dict[str, set[str]]      # instrument -> {strategy names}
    def subscribe(self, instrument, who): ...
    def unsubscribe(self, instrument, who): ...
    def _tick(self):                       # after candle close
        for inst in self.subscribers:
            cache.put(inst, self.interval_min, fetch_one(inst, self.interval_min))

class DataProvider:                        # L2, now poller-backed
    poll_5m: CandlePoller
    poll_15m: CandlePoller
    def candles(self, inst, interval):     return cache.get(inst, interval)
```

**Dynamic membership (your B&B example):** Break-and-Bounce watches a stock for a
15-min breakout, then for a 5-min retest. So:

1. Premarket: B&B `subscribe(stock, "bb")` on the **15-min** poller (breakout watch).
2. On confirmed 15-min breakout: `poll_15m.unsubscribe(stock,"bb")` →
   `poll_5m.subscribe(stock,"bb")` — the instrument **moves between pollers**.
3. On trade close / window void: unsubscribe entirely.

An instrument stays in a poller only while *some* strategy needs it; the union of
subscribers defines the fetch set, so there's never a duplicate pull.

**Cache:** in-memory dict for a single-process deployment, or a `candles` table in
the shared store for multi-container (then strategies/scanners become zero-API for
candles too — the end-state in §4 pattern B).

**Migration:** wrap the existing `get_daily_candles`/`get_intraday_candles` so they
read the cache; start the two pollers in a background thread (single process) or a
new `candle-poller` service (Docker). Strategies swap their direct fetch calls for
`subscribe`/`candles`. Incremental — one strategy at a time.

This is a **larger change to the data layer than anything shipped so far** and it
touches each strategy's fetch path, so it needs its own build pass.

---

## 13. Suggest after a full scan cycle — IMPLEMENTED

Built: `cycle_gate.py` + `test_cycle_gate.py` (4/4). `CycleGate` watches the
`gap_history` / `oi_buildup_history` / `iv_rank_history` timestamps and fires once
per completed cycle (paced by the slowest scan, missing tables ignored). Wired into
`main.py.run_scan_cycle`: the suggester now runs via `cycle_gate.ready_and_mark()`
instead of every discount tick.

Original design below.

Right now the suggester fires every discount tick. You want it to fire **once per
completed cycle** of the repeated intraday scans (gap, oi_buildup, iv_rank,
discount).

**Cycle-complete gate:** each repeated scan already stamps a timestamp in its
`*_history` table. A coordinator records the last-seen timestamp per scan; a cycle
is "complete" when **every** participating scan has produced a row newer than the
previous suggestion. Only then does the suggester run (and it marks the new
watermark, so it fires once per cycle, not per tick).

```python
def cycle_complete(last_marks) -> bool:
    fresh = {name: latest_ts(table) for name, table in PARTICIPATING.items()}
    return all(fresh[n] > last_marks.get(n, 0) for n in fresh)
```

Pragmatic alternative (no coordinator): trigger the suggester at a fixed time a few
minutes after each scan cluster (e.g. the scans bunch at :45 → suggest at :50).
Simpler, but brittle if scan times drift.

---

## 14. Alerts & EOD detail — IMPLEMENTED

Paper-trading is now entry-only + EOD (no mid-day fill pings):

- Mid-day T1/T2/SL/BE fill alerts suppressed in `paper_trader.monitor` (fills still
  persisted). The Trade Suggester writes its CSV every cycle but only Telegrams if
  `TS_ALERT=true`.
- Entry alert enriched: entry time, expiry/DTE, lot size, qty, risk & reward ₹.
- EOD summary per trade: lot, entry/exit time, entry/exit price, %/₹, and a
  plain-language **why** (e.g. "ran to T2", "hit stop-loss", "flat at square-off —
  premium barely moved").
- Liberalised entry (it's paper): `max_signals_per_day` 5→15, `no_entry_after`
  14:00→15:00, discount `min_discount_score` 55→45.

---

## 15. What you get

- **One fetch per data point per cycle** instead of one-per-strategy → fewer API calls, less rate-limit risk.
- **Add a scanner in one file** that takes a `DataProvider` — the IV-rank/OI/gap suite already proves the pattern.
- **One candle-fetch implementation** and **one order path** instead of 2–3 drifting copies.
- **Live ↔ paper** and **DB ↔ broker** are single swaps behind interfaces.
