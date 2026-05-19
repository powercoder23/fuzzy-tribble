# Project: Dhan F&O Options Trading Bot

A multi-service trading system for NSE F&O. Each strategy runs in its own
Docker container, sharing IV data through a SQLite volume written by a
single IV collector service. Order placement is gated by `AUTO_EXECUTE`;
when false, strategies fire Telegram alerts only.

## Service layout (docker-compose)

| # | Service        | Container             | Entry                          | Trades? |
|---|----------------|-----------------------|--------------------------------|---------|
| 1 | iv-collector   | iv-collector          | `iv_collector_service.py`      | No (data only) |
| 2 | momentum       | momentum-strategy     | `momentum_runner.py`           | Yes     |
| 3 | discount       | discount-strategy     | `main.py --auto-loop`          | Yes (paused via `profiles: [discount]`) |
| 4 | break-bounce   | break-bounce-strategy | `break_bounce_runner.py`       | Yes     |

Only `iv-collector` calls option-chain APIs continuously. Strategies read IV
from the shared SQLite (`iv_history.db`) and only hit Dhan for candles +
chain at signal/execution time.

---

## Strategies

This section covers the four `*_strategy.py` modules besides `discount.py`.
Note: only **three distinct trading strategies** exist in the code. Splitting
momentum into ORB and VWAP (both live in `momentum_strategy.py:MomentumScanner`)
yields ORB + VWAP + Break-and-Bounce. **A fourth strategy is not present** —
see the "Missing 4th strategy?" section at the end.

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

### Missing 4th strategy?

You asked for four strategies. The repo has **three trading strategies** plus
the IV collector. After full inspection:

- `iv_collector_service.py` is explicitly self-described as *"Service 1. Sole responsibility: fetch option chain data and persist IV snapshots. Neither strategy service should write IV data."* — it has no entry rules and never places orders.
- `main.py` is a scheduler wrapper that runs `discount.py`'s scanner — not a separate strategy.
- The `*_runner.py` files (`momentum_runner.py`, `break_bounce_runner.py`) are just `schedule`-based service wrappers around their `*_strategy.py` siblings — no extra rules.
- No other `*_strategy.py` or trading-logic file exists at the project root or in `old/` outside of the discount/momentum/break-bounce families.

**If you intended a 4th strategy, it isn't in the tree yet.** Likely
candidates if you want me to look further: a planned strategy described
elsewhere (a doc, screenshot, or branch), or one that lives inside
`discount.py` as a sub-mode I should pull out.

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
