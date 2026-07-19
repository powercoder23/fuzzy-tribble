# Project: NSE F&O Options Trading Bot (Upstox data / Dhan reserved for future execution)

A multi-service trading system for NSE F&O. Each strategy runs in its own
Docker container, sharing IV data through a SQLite volume written by a
single IV collector service. Order placement is gated by `AUTO_EXECUTE`;
when false, strategies fire Telegram alerts only.

**Broker split:** ALL market data (chains, candles, expiries, quotes) comes
from **Upstox** via `upstox_adapter.UpstoxDhanAdapter` (a Dhan-surface shim —
the internal data contract still uses the Dhan response shape). Dhan is NOT
used for data; it is reserved only for possible future live order placement.

## Service layout (docker-compose.prod.yml — current reality)

| #  | Service         | Container               | Entry                            | Default up? | Trades? |
|----|-----------------|-------------------------|----------------------------------|-------------|---------|
| 1  | iv-collector    | iv-collector            | `collectors.iv_collector_service`| yes         | No (data only) |
| 2  | momentum        | momentum-strategy       | `momentum_runner.py`             | no (`profiles: [momentum]`, discontinued) | Yes |
| 3  | discount        | discount-strategy       | `main.py`                        | **yes**     | Paper |
| 4  | break-bounce    | break-bounce-strategy   | `break_bounce_runner.py`         | yes         | Yes |
| 5  | directional-iv  | directional-iv-strategy | `directional_iv_runner.py`       | no (profile, discontinued) | Yes |
| 6  | iv-rank         | iv-rank-scanner         | `iv_rank_runner.py`              | yes         | No (alerts) |
| 7  | oi-buildup      | oi-buildup-scanner      | `oi_buildup_runner.py`           | yes         | No (alerts; feeds auto-exit) |
| 8  | gap-scan        | gap-scanner             | `gap_scanner_runner.py`          | yes         | No (alerts) |
| 9  | delivery-surge  | delivery-surge-scanner  | `delivery_surge_runner.py`       | yes         | No (alerts) |
| 10 | smart-money     | smart-money-scanner     | `smart_money_runner.py`          | yes         | No (alerts) |
| 11 | composite       | composite-scanner       | `composite_runner.py`            | yes         | No (feeds entry gate) |
| 12 | sonar           | sonar-scanner           | `sonar_laplace_runner.py`        | yes         | No (feeds entry veto + risk warnings) |
| 13 | vol-expansion   | vol-expansion-strategy  | `vol_expansion_runner.py`        | yes         | Paper |

API callers: `iv-collector` sweeps option chains continuously; the `discount`
service also fetches chains + candles during its 15-min scans, and `sonar`
fetches 5-min candles. All other scanners are zero-API (read `iv_history.db`
only).

**Sole-writer contract:** only `iv-collector` writes `iv_history` rows
(`iv_store.save_snapshot`); scanner services write their own `*_history`
tables. All SQLite access must go through `iv_store.connect()` (WAL +
busy_timeout) — see ARCHITECTURE_REVIEW_P0.md §0 for why.

---

## Strategies

This section covers the `*_strategy.py` modules besides `discount.py`.
Splitting momentum into ORB and VWAP (both live in
`momentum_strategy.py:MomentumScanner`) yields four distinct trading
strategies: ORB + VWAP + Break-and-Bounce + Volatility-Expansion.

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

### Strategy: Volatility-Expansion (IV buy-zone)

- **Files:** [vol_expansion_strategy.py](vol_expansion_strategy.py), runner [vol_expansion_runner.py](vol_expansion_runner.py), config [vol_expansion_config.py](vol_expansion_config.py).
- **Signal source:** the dashboard's "II · Volatility Expansion — 4-day IV slope" leaderboard (`iv_analytics.buy_zone_leaderboard`) — names whose daily ATM IV is **climbing** (slope ≥ `MIN_SLOPE`) while **still cheap** on 52-wk history (IVP in the buy zone), ranked by `slope × (1 - IVP/100)`. Vega signal, not directional by nature.
- **Direction:** since the signal itself is direction-agnostic, the strategy picks CE/PE from the underlying's recent daily spot trend (`underlying_bias`, `TREND_LOOKBACK` days, `MIN_MOVE_PCT` threshold). `REQUIRE_TREND=true` skips names with no clear lean rather than forcing a directional bet on a pure-vega setup.
- **Universe / gates:** `BUY_ZONE_ONLY` restricts to buy-zone names (else trades top-expanding by slope regardless of price, i.e. includes rich chases); `MAX_SCAN` candidates considered; liquidity floor (`LIQ_MIN_OI`, `LIQ_MIN_VOLUME`, `LIQ_MAX_SPREAD`); `MIN_PREMIUM`, `MIN_DTE`.
- **Sizing & risk:** single long-premium leg, ATM ± `STRIKE_OTM_OFFSET`. SL at `entry × (1 - SL_PCT)` (30% default). T1/T2 at `T1_MULT`/`T2_MULT` on premium, `T1_BOOK_FRACTION` booked at T1.
- **Booking:** goes through `OrderManager.submit_external_signal` into the **shared paper book** — same monitor/fill-alerts/auto-exit/EOD/analytics pipeline as discount and Break-and-Bounce. `MODE` (`off`/`alert`/`paper`, env `VOL_EXP_MODE`, default `paper`) gates whether candidates are scanned-only-and-alerted or actually booked; paper only, no real orders regardless of mode.
- **Schedule:** scans at `SCAN_TIMES` (default 09:45, 11:00, 13:00 — the daily-IV signal changes slowly, so a few scans are enough to catch freshly-qualifying names), entry cutoff `ENTRY_CUTOFF` (13:30), monitor every `MONITOR_INTERVAL_MIN` min until `MONITOR_UNTIL`, square-off at `SQUARE_OFF`, EOD summary at `EOD_SUMMARY_AT` (15:20/15:25).
- **Cap:** `MAX_TRADES_PER_DAY` (default 3).

---

## Shared infrastructure

- **IV store:** [iv_store.py](iv_store.py) — SQLite (`iv_history.db`) with intraday + daily ATM IV snapshots. Read by every strategy for affordability estimates.
- **Lot sizing:** `momentum_strategy.py:ScripMasterLotSizer` ([lines 39-150](momentum_strategy.py#L39-L150)) reads `data/api-scrip-master.db`. Same class is reused by Break-and-Bounce.
- **Tokens:** [token_manager.py](token_manager.py) handles Dhan access-token refresh.
- **Telegram:** Each strategy has its own `*TelegramNotifier` class; bot token + chat id are pulled from the `DiscountedPremiumScanner` config.
- **Order safety:** All strategies place a market BUY then immediately follow with an SL_M SELL; if the SL order fails, an emergency market SELL is placed and a Telegram alert is fired (see `_place_order` in both runners).

## Operational notes

- All strategies require `iv-collector` to be running first (the docker-compose `depends_on` enforces this).
- `discount` service is paused by default (`profiles: ["discount"]`); start it explicitly with `docker-compose --profile discount up`.
- `AUTO_EXECUTE=true` env var is required for live order placement; otherwise alerts are sent without orders.
- All times are IST (`Asia/Kolkata`); container TZ is set explicitly.
