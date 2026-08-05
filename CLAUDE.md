# Project: NSE F&O Options Trading Bot (Upstox data / Dhan reserved for future execution)

A multi-service trading system for NSE F&O. Each strategy runs in its own
Docker container, sharing IV data through a SQLite volume written by a
single IV collector service. Order placement is gated by `AUTO_EXECUTE`;
when false, strategies fire Telegram alerts only.

**Broker split:** ALL market data (chains, candles, expiries, quotes) comes
from **Upstox** via `upstox_adapter.UpstoxDhanAdapter` (a Dhan-surface shim —
the internal data contract still uses the Dhan response shape). Dhan is NOT
used for data; it is reserved only for possible future live order placement.

## Service layout (docker-compose.yml — current reality, verified 2026-07-30)

**Removed 2026-08-05:** the vol-expansion, directional-IV and convex-engine
strategies were deleted outright (files, services, tests). `engine/` survives
as a candles-only helper — sonar writes `candles_5m` through `engine.candles`,
which `momentum_alpha` reads. Recover any of it from git history if needed.

**MOMENTUM-ONLY MODE (since 2026-08-04).** The gating was inverted: `momentum`
is now the only strategy without a `profiles:` key, and every other strategy
and scanner is profile-gated OFF. A plain `docker-compose up -d` brings up
exactly four services — `iv-collector`, `momentum`, `market-context`,
`dashboard`. Each gated service uses its own name as the profile, so bringing
one back for a session is `docker-compose --profile <name> up -d <name>`;
re-enabling it permanently means deleting its `profiles:` line.

| #  | Service         | Container               | Entry                            | Default up? | Trades? |
|----|-----------------|-------------------------|----------------------------------|-------------|---------|
| 1  | iv-collector    | iv-collector            | `collectors.iv_collector_service`| yes         | No (data only) |
| 2  | momentum        | momentum-strategy       | `momentum_runner.py`             | **yes** (only strategy on) | Alerts + CSV journal + **shared paper book** (since 2026-08-05, `MOMENTUM_PAPER_MODE`); live order only when `AUTO_EXECUTE=true` — see note below |
| 3  | discount        | discount-strategy       | `main.py`                        | no (`profiles: [discount]`) | Blocked — `discount_config.PAPER_TRADING_ENABLED = False` (kill switch off 2026-07-29, back ON 2026-07-30, off again 2026-08-05) |
| 4  | break-bounce    | break-bounce-strategy   | `break_bounce_runner.py`         | no (`profiles: [break-bounce]`) | Paper (+ debit-spread hedge leg, see below) |
| 5  | iv-rank         | iv-rank-scanner         | `iv_rank_runner.py`              | no (`profiles: [iv-rank]`) | No (alerts) |
| 6  | oi-buildup      | oi-buildup-scanner      | `oi_buildup_runner.py`           | no (`profiles: [oi-buildup]`) | No (alerts; feeds auto-exit) |
| 7  | gap-scan        | gap-scanner             | `gap_scanner_runner.py`          | no (`profiles: [gap-scan]`) | No (alerts) |
| 8  | delivery-surge  | delivery-surge-scanner  | `delivery_surge_runner.py`       | no (`profiles: [delivery-surge]`) | No (alerts) |
| 9  | smart-money     | smart-money-scanner     | `smart_money_runner.py`          | no (`profiles: [smart-money]`) | No (alerts) |
| 10 | composite       | composite-scanner       | `composite_runner.py`            | no (`profiles: [composite]`) | No (feeds entry gate) |
| 11 | sonar           | sonar-scanner           | `sonar_laplace_runner.py`        | no (`profiles: [sonar]`) | No (feeds entry veto + risk warnings) |
| 12 | iv-seller       | iv-seller-strategy      | `iv_seller_runner.py`            | no (`profiles: [iv-seller]`) | Blocked by `paper_policy` only — has **no paper flag of its own** |
| 13 | market-context  | market-context          | `market_context.service`         | yes         | No (observational only, Phase 1) |

**Paper trading is momentum-only, enforced at the INSERT (2026-08-05).**
`paper_policy.py` holds `PAPER_STRATEGY_ALLOWLIST` (default `Momentum`,
case-insensitive prefixes; `*` allows all; empty allows none — it fails
closed). It is checked in `PaperTradeBook.open_trade`, the single INSERT into
`paper_trades`, plus early in `book_signal` and `process_signals` so a refused
signal costs no broker calls and fires no "PAPER TRADE TAKEN" alert.

This exists because `profiles:` did not work as a kill switch: on 2026-08-05,
with every strategy nominally gated off, vol-expansion (46), convex-engine (4,
−₹11,617) and break-and-bounce (1) still booked 51 trades from containers that
had never been stopped. Break & Bounce, directional-IV and IV-seller have no
paper flag of their own, so there was also nothing per-strategy to switch off.
Per-strategy flags are still set where they exist, but the allowlist is the
guarantee. Tests: [test_paper_policy.py](test_paper_policy.py) (20).

**Momentum writes to BOTH books (since 2026-08-05).** It was the only strategy
that never called `OrderManager.submit_external_signal`, so it was invisible on
the dashboard and absent from every shared analytic. Now:

- `data/momentum_trades.csv` (`MomentumTradeJournal`) — unchanged schema, still
  written **first and unconditionally**, so momentum's own record never depends
  on the shared book accepting the signal;
- `paper_trades.db` via `submit_external_signal`, tagged
  `Momentum-<TRIGGER>` (`Momentum-ORB`, `Momentum-VWAP_RECLAIM` — the Convex
  tag idiom, so the two momentum strategies stay separable in EOD analytics);
- Telegram alert, and `_place_order` only under `AUTO_EXECUTE=true` — untouched.

Gated by `PAPER["mode"]` (`MOMENTUM_PAPER_MODE`, default `paper`; `off`
restores the exact pre-2026-08-05 behaviour). Booking is wrapped so a failure
in the shared book cannot break the CSV journal or the alert.

Two consequences worth knowing:

- **`submit_external_signal` applies the SHARED gates** (pre-market IVR/IV-HV/
  OTM%/PCR, breadth, concentration, exposure, daily-loss lockout) on top of
  momentum's own selection, so it can legitimately refuse a signal that the CSV
  journal records. A divergence between the two is expected, not a bug — the
  refusal reason is logged and written to `scan_log`.
- **Momentum declares its own risk budget.** It sizes lots to
  `RISK_CONFIG["max_risk_pct"] × CAPITAL` (₹4,000), but the shared book's
  default 1-lot cap is ₹1,500 — it would have rejected nearly every momentum
  signal. The signal therefore carries `max_risk_rupees`, a per-signal override
  in `book_signal` (same idiom as B&B's `min_premium`). This is not a
  loosening: `calculate_lots` needs `premium × sl_pct × lot_size ≤ max_risk()`
  for `lots ≥ 1`, and the book caps `(entry − sl) × lot_size` against the same
  number, so the two gates are the *same inequality* — anything the override
  admits, momentum had already sized; anything it rejects, momentum would have
  refused as unaffordable. Pinned by a parametrized invariant test.

**Momentum now runs the shared lifecycle too.** Booking without monitoring
would strand every position, since scans stop at 11:30 but trades live to
square-off. `momentum_runner.py` schedules `run_monitor` every
`MOMENTUM_MONITOR_INTERVAL_MIN` from 09:35 to 15:10, `run_square_off` at 15:15
and `run_paper_eod` at 15:20 — all skipped when `MOMENTUM_PAPER_MODE=off`.
Tests: [test_momentum_paper.py](test_momentum_paper.py) (21).

API callers: `iv-collector` sweeps option chains continuously, and `momentum`
fetches daily candles at premarket plus intraday candles + option chains
during its 09:30–11:30 scans. The other strategies/scanners that also hit the
API (`discount`, `sonar`) are currently gated off.

**Sole-writer contract:** only `iv-collector` writes `iv_history` rows
(`iv_store.save_snapshot`); scanner services write their own `*_history`
tables. All SQLite access must go through `iv_store.connect()` (WAL +
busy_timeout) — see ARCHITECTURE_REVIEW_P0.md §0 for why.

---

## Strategies

This section covers the `*_strategy.py` modules besides `discount.py`.
Splitting momentum into ORB and VWAP (both live in
`momentum_strategy.py:MomentumScanner`) yields three distinct trading
strategies: ORB + VWAP + Break-and-Bounce.

### Strategy: Momentum — Opening Range Breakout (ORB)

- **File:** [momentum_strategy.py](momentum_strategy.py) — `MomentumScanner.check_orb_signal` ([lines 480-525](momentum_strategy.py#L480-L525))
- **Universe:** F&O stocks that pass affordability (lots ≥ 1 within `RISK_CONFIG["max_risk_pct"]` of capital) and the daily regime filter (price > EMA20 > EMA50 with ADX ≥ `REGIME["adx_min"]` for CE; mirrored for PE).
- **Entry rule:** On the latest *completed* intraday candle (interval = 15 min by default), if `close > opening-range high` AND volume ratio (`last.volume / mean(prev 5 candles)`) ≥ `ORB["volume_mult"]` → **CE**. If `close < opening-range low` AND same volume gate → **PE**. Opening range is the first `ORB["range_candles"]` bars of the session.
- **Time gates:** No new entries after `ORB["entry_cutoff_hour"]:ORB["entry_cutoff_min"]`.
- **Sizing & risk:** Lots = `floor(max_risk / (premium × sl_pct × lot_size))`. SL at `entry × (1 - RISK_CONFIG["sl_pct"])` (default 30% premium drawdown). Two targets `target1_mult` and `target2_mult` on premium.
- **Liquidity gate:** OI ≥ `LIQUIDITY["min_oi"]`, volume ≥ `LIQUIDITY["min_volume"]`, spread ≤ `LIQUIDITY["max_spread_pct"]`.
- **Strike:** ATM + `STRIKE["intraday_otm_offset"]` strike-gaps in the trade direction.
- **Ranking:** `MomentumSignalRanker` scores aligned signals (+40 STRONG / +20 WEAK regime, +30 direction-aligned, +10 if trigger=ORB, +5 if vol ratio ≥ 2). Only top `max_trades_per_day` are taken.
- **Schedule:** premarket 09:00 (regime + affordability scan), intraday scan every 5 min between 09:30–11:30, EOD summary at 15:15.

### Momentum alpha engine ([momentum_alpha.py](momentum_alpha.py), added 2026-08-05)

Signal-quality layer for ORB/VWAP. Zero broker calls — every factor comes from
data already on disk. Risk, sizing, journal, Telegram and execution are
untouched.

- **Selection precedes signal.** `DailyUniverseRanker` ranks all ~210 F&O names
  by 20-day return percentile (`rs_pct`) plus ATR% and ATR expansion, from
  `delivery_daily`. Premarket keeps the top `RS["shortlist"]` by *distance from
  the median* — leaders are CE candidates, laggards PE candidates, mid-pack
  names have no edge either way. Previously candidates were taken in dict order.
- **`RelativeVolume`** — cumulative volume vs the same clock time on prior
  sessions (`candles_5m`), replacing the time-of-day-biased "previous 5 candles"
  baseline.
- **`IntradayRelativeStrength`** — stock vs NIFTY since the open. NIFTY 5-min
  closes are in `candles_5m` under `security_id='13'` (the `symbol` column is
  mislabeled there — key on the id).
- **`breakout_quality` / `vwap_quality`** — continuous 0–1 structure scores
  (coil contraction, volume expansion, close location, follow-through; VWAP
  slope, acceptance, signed separation) replacing binary pass/fail.
- **`MomentumConvictionScorer`** — weighted 0–100 confidence from
  `WEIGHTS`, normalised over *available* factors so a missing input dilutes
  rather than scoring zero. Trades require `CONFIDENCE["min_score"]`.
  `observe_only=True` scores and journals without blocking anything.
- Breadth/sector gates reuse `breadth.py`; OI reuses `oi_buildup_scanner`.

**Sonar must stay running** — it is the sole writer of `candles_5m`, which the
RVOL, intraday-RS and quality factors read.

Set any weight in `WEIGHTS` to 0 to journal a factor without letting it
influence trades — the observe-then-enable protocol that produced engine
formula v2.1. The factor trace for every trade is written to the existing
`notes` column of `momentum_trades.csv`, so the journal schema is unchanged.
Tests: [test_momentum_alpha.py](test_momentum_alpha.py) (22, no broker needed).

### Strategy: Momentum — VWAP Reclaim / Break

- **File:** [momentum_strategy.py](momentum_strategy.py) — `MomentumScanner.check_vwap_signal` ([lines 527-570](momentum_strategy.py#L527-L570))
- **Same orchestration** as ORB — runs in the same `MomentumStrategyRunner.run_intraday_scan` loop, ranked together with ORB signals.
- **Entry rule:** On the latest completed candle vs. the prior completed candle:
  - **CE (vwap_reclaim):** `prev.close < prev.vwap` AND `last.close > last.vwap` AND volume ratio ≥ 1.3 (hardcoded, not in config).
  - **PE (vwap_break):** `prev.close > prev.vwap` AND `last.close < last.vwap` AND same volume gate.
- **VWAP** is computed locally as a cumulative `Σ(typical_price × volume) / Σ(volume)` from the candles fetched (see `get_intraday_candles` [lines 452-454](momentum_strategy.py#L452-L454)) — not pulled from broker.
- **Same regime gate, sizing, SL/T1/T2, liquidity, and strike-selection** as ORB.

### Strategy: Break and Bounce (Strategy 4)

- **Files:** [break_bounce_strategy.py](break_bounce_strategy.py), runner [break_bounce_runner.py](break_bounce_runner.py), config [break_bounce_config.py](break_bounce_config.py).
- **Universe:** All F&O stocks with valid yesterday daily candle (no affordability pre-filter — affordability is checked only at signal time).
- **Three-step entry:**
  1. **Premarket (09:00):** Cache yesterday's daily high/low for every F&O stock as `yesterday_high` / `yesterday_low` ([`get_yesterday_levels`](break_bounce_strategy.py#L205-L225)).
  2. **15-min breakout (09:15–11:45 window):** A *completed* 15-min candle with `close > yesterday_high` → BULLISH; `close < yesterday_low` → BEARISH. Past 11:45 the setup is voided ([`check_15min_breakout`](break_bounce_strategy.py#L229-L298)).
  3. **5-min retest entry:** After breakout is confirmed, on the most recent completed 5-min candle ([`check_5min_entry`](break_bounce_strategy.py#L302-L403)):
     - **BULLISH side** — `last.low` within `BB_BREAKOUT["retest_tol_pct"]` of yesterday's high, AND either:
       - **Hammer:** lower wick ≥ `hammer_wick_ratio` × body, upper wick ≤ `max_counter_wick` × body, **and** preceded by ≥2 red candles falling into the level. Entry = `last.close`, SL = `last.low`.
       - **Bullish engulfing:** `curr.low < prev.low` AND `curr.high > prev.high` AND curr is bullish. Entry = `prev.high`, SL = `last.low`.
     - **BEARISH side** — mirror: `last.high` within tolerance of yesterday's low; inverted hammer (with ≥2 prior green candles) or bearish engulfing.
- **Risk:** option SL at `entry × (1 - BB_RISK["sl_pct"])`, target = entry + (sl_amount × `BB_RISK["target_ratio"]`) — i.e. **fixed 2.5×** per the docstring (versus momentum's two-target T1/T2 split).
- **Strike:** ATM + `BB_STRIKE["otm_offset"]` strike-gaps (separate config from momentum).
- **Lifecycle:** one trade per stock per day (`state["trade_placed"]`). Setup is voided once breakout window expires without a breakout. EOD reset at 15:15.

---

## Shared infrastructure

- **IV store:** [iv_store.py](iv_store.py) — SQLite (`iv_history.db`) with intraday + daily ATM IV snapshots. Read by every strategy for affordability estimates.
- **Lot sizing:** `momentum_strategy.py:ScripMasterLotSizer` ([lines 39-150](momentum_strategy.py#L39-L150)) reads `data/api-scrip-master.db`. Same class is reused by Break-and-Bounce.
- **Tokens:** [token_manager.py](token_manager.py) handles Dhan access-token refresh.
- **Telegram:** Each strategy has its own `*TelegramNotifier` class; bot token + chat id are pulled from the `DiscountedPremiumScanner` config.
- **Order safety:** All strategies place a market BUY then immediately follow with an SL_M SELL; if the SL order fails, an emergency market SELL is placed and a Telegram alert is fired (see `_place_order` in both runners).

## Operational notes

- All strategies require `iv-collector` to be running first (the docker-compose `depends_on` enforces this).
- Every strategy/scanner except `momentum` is paused by default; start one explicitly with `docker-compose --profile <service-name> up -d <service-name>`.
- Adding a `profiles:` key does **not** stop an already-running container. After changing the gating, run `docker-compose down` (or `docker stop`/`docker rm` the specific containers) before `docker-compose up -d`, otherwise the old containers keep running with `restart: unless-stopped`.
- `AUTO_EXECUTE=true` env var is required for live order placement; otherwise alerts are sent without orders.
- All times are IST (`Asia/Kolkata`); container TZ is set explicitly.
