# Strategy Specification

**System:** NSE F&O Multi-Strategy Options Research Platform
**Repository:** `fuzzy-tribble`
**Specification date:** 2026-08-03
**Specification method:** Code-grounded (parameters read from source), operator-verified (intent, objectives, regime beliefs), data-derived (performance, weaknesses, and assumptions computed from `data/paper_trades.db` and `logs/`).

---

## 0. How to Read This Document

### 0.1 Authority convention

Per operator instruction, **conflicts between the as-built code and the documented/stated intent are recorded in parallel**, with no ruling on which wins. Every such conflict appears in a block of this form:

> **⚠ DIVERGENCE — `<topic>`**
> **As-built:** what the code does today.
> **Intent:** what the documentation, code comment, or operator states it should do.
> **Impact:** the behavioural consequence of the gap.

An implementer reproducing this system must choose one side of every divergence explicitly.

### 0.2 Provenance tags

Every factual claim carries one of:

| Tag | Meaning |
|---|---|
| `[CODE]` | Read directly from source. Authoritative for as-built behaviour. |
| `[DATA]` | Computed from `data/paper_trades.db` (573 rows) or `logs/*.log`. |
| `[OPERATOR]` | Stated by the system owner during specification interview. |
| `[DERIVED]` | Inferred by combining `[CODE]` and `[DATA]`. Reasoning shown. |
| `[NOT SPECIFIED]` | Genuinely unknown. Requires operator input before implementation. |

### 0.3 Strategy identifiers

Eight distinct trade-generating strategies exist. Seven were in scope; an eighth (**S8 Convex**) was discovered during specification to be actively booking trades and is documented because it materially affects Sections 19–20.

| ID | Name | Module | Service | Books trades today? |
|---|---|---|---|---|
| **S1** | Momentum — Opening Range Breakout | `momentum_strategy.py` | `momentum` | No — service disabled |
| **S2** | Momentum — VWAP Reclaim/Break | `momentum_strategy.py` | `momentum` | No — service disabled |
| **S3** | Break and Bounce | `break_bounce_strategy.py` | `break-bounce` | **Yes** |
| **S4** | Volatility Expansion (IV slope) | `vol_expansion_strategy.py` | `vol-expansion` | No — starved by gates |
| **S5** | Discounted Premium ("Discount") | `discount.py` | `discount` | **Yes** |
| **S6** | Directional IV | `directional_iv_strategy.py` | `directional-iv` | No — starved by gates |
| **S7** | IV Seller (strangle / straddle / 0DTE) | `iv_seller_strategy.py` | `iv-seller` | No — blocked by risk cap |
| **S8** | Convex Engine | `engine/paper.py` | `convex-engine` | **Yes** |
| **H** | Hedge overlay (not standalone) | `hedge.py` | n/a — applies to S3/S5/S6 | **Yes** |

---

## 1. Strategy Overview

### 1.1 System name

NSE F&O Multi-Strategy Options Research Platform. No shorter formal name exists. `[CODE]`

### 1.2 One-sentence summary

A containerised multi-service system that runs eight independent intraday options strategies in parallel against a shared, cost-accurate paper-trading book, recording a full factor snapshot at every entry so that the source of edge can be attributed after the fact. `[DERIVED]`

### 1.3 Primary objective

Verbatim from the operator: `[OPERATOR]`

> "Build a quantitative options research platform that systematically discovers, validates, and monitors statistically robust intraday trading edges in NSE F&O. The platform's primary purpose is to measure edge quality through realistic paper trading, feature attribution, and out-of-sample validation. Consistent profitability is the eventual objective, but no strategy graduates to live trading until it demonstrates positive expectancy, robustness across market regimes, and acceptable risk-adjusted performance."

This objective is **structurally reflected in the code**, not merely aspirational: `[DERIVED]`

- `paper_trader.collect_factor_snapshot()` persists a JSON blob of every factor visible at entry (score, IV, HV, IV rank, half-spread, expected-move ratio, PCR, trade type, Sonar read, OI buildup, composite, market breadth) into the `factors_json` column of every booked trade. `[CODE]`
- `paper_trader._finalize()` applies the full NSE fee schedule (`costs.py`: brokerage, STT, exchange transaction charge, SEBI fee, stamp duty, IPFT, GST) plus a two-sided spread-crossing slippage model to every trade. Paper P&L is net, not gross. `[CODE]`
- `scan_log.record_decision()` writes a row for every *rejected* candidate with the rejecting gate and reason — 49,951 rows to date — so the counterfactual population is preserved, not just the traded one. `[DATA]`

### 1.4 Trading style

**Intraday, without exception.** No strategy holds a position overnight. `[CODE]`

| Strategy | Style | Mechanism enforcing it |
|---|---|---|
| S1, S2 | Intraday | `ORB["force_exit_hour/min"]` = 15:15 |
| S3 | Intraday | `BB_BREAKOUT["force_exit_hour/min"]` = 15:15 |
| S4 | Intraday | `SQUARE_OFF` = 15:20 |
| S5 | Intraday | `INTRADAY["square_off"]` = 15:20 |
| S6 | Intraday | Managed by the shared square-off |
| S7 | Intraday | EOD close at 15:15 (`iv_seller_runner`) |
| S8 | Intraday | Shared monitor square-off |

An earlier investigation into BTST/swing extension was conducted and rejected. `[OPERATOR]`

### 1.5 Typical holding time

Measured from the closed-trade population (372 trades): `[DATA]`

| Exit type | Count | Share | Implied holding time |
|---|---|---|---|
| `Time 15:20` (forced square-off) | 232 | 62.4% | Entry to 15:20 — up to ~5h45m |
| `SL` | 78 | 21.0% | Variable, minutes to hours |
| `Target` | 57 | 15.3% | Variable, minutes to hours |
| `OI contradiction` (auto-exit) | 5 | 1.3% | Variable |

**The modal outcome is the clock, not the thesis.** 62.4% of all trades are closed by forced square-off rather than by hitting either a stop or a target. `[DATA]`

### 1.6 Asset classes traded

Exclusively **NSE Equity Derivatives (F&O)**. No commodity, no currency, no cash equity, no international. `[CODE]`

### 1.7 Instruments traded

**Stock options only, in practice.** `[DATA]`

- Index option support exists in code — `vol_expansion_strategy.INDEX_SYMBOLS = {NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX}` routes to the `IDX_I` exchange segment, and `iv_seller_strategy` routes `{NIFTY, BANKNIFTY}` likewise. `[CODE]`
- Zero index options appear in the traded book. All 573 rows are single-stock options. `[DATA]`
- Stock futures are **read** for open-interest confirmation (`oi_validator`, gated off by default) but never traded. `[CODE]`

Position types booked:

| Type | Direction field | Used by |
|---|---|---|
| Long call (CE) / long put (PE) | `long` | S1–S6, S8 |
| Short call / short put | `short` | S7 (both legs), H (hedge leg) |
| Bull call spread / bear put spread | paired `long` + `short` sharing `combo_id` | S3, S5, S6 via H |
| Short strangle | two `short` legs | S7 |
| Short straddle | two `short` legs at ATM | S7 |

---

## 2. Trading Universe

### 2.1 Eligible instruments

| Instrument class | Eligible? | Notes |
|---|---|---|
| NIFTY | Code-supported, never traded | `IDX_I` segment |
| BANKNIFTY | Code-supported, never traded | `IDX_I` segment |
| FINNIFTY | Code-supported (S4 only), never traded | |
| MIDCPNIFTY | Code-supported (S4 only), never traded | |
| SENSEX / BANKEX | Listed in S4's index set, never traded | BSE names in an NSE system — see divergence below |
| **Stock options** | **Yes — the entire live universe** | `NSE_FNO` segment |
| Stock futures | No — read-only for OI confirmation | |
| Commodity | No | |
| Currency | No | |

> **⚠ DIVERGENCE — index symbol set**
> **As-built:** `vol_expansion_strategy.INDEX_SYMBOLS` includes `SENSEX` and `BANKEX`, which are BSE indices.
> **Intent:** The system is documented throughout as NSE-only, sourcing all data from Upstox's NSE endpoints.
> **Impact:** Dormant. Neither symbol can be resolved by the NSE F&O universe loader, so the branch is unreachable in practice.

### 2.2 Base universe construction

`[CODE]` — `f_o_stocks_list.py` → `discount.DiscountedPremiumScanner`:

1. Fetch the NSE F&O symbol list; cache to `data/fno_cache/` keyed by date.
2. Cross-reference against the local scrip master (`data/api-scrip-master.db`, refreshed daily, 101 MB) to resolve each symbol to an Upstox `security_id`.
3. Result on 2026-08-03: **208 stock F&O symbols** loaded, 209 entries after index inclusion. `[DATA]` (log: `discount | Loaded 208 stock F&O symbols from NSE + scrip master`)

### 2.3 Prefilter — liquid universe trim

Applies to **S5 (Discount) only**. `[CODE]`

```
discount_config.LIQUID_UNIVERSE_ONLY = True
discount_config.LIQUID_UNIVERSE_SIZE = 120
```

Ranking metric: latest `OI × volume` read from the local `iv_history.db` — zero additional API calls. Observed on 2026-08-03: `Liquid-universe trim: 209 -> 120 (top 120 by OI x volume)`. `[DATA]`

This trim explicitly does **not** apply to S1, S2, S3, S4, S6 or S7; each keeps its own universe. `[CODE]`

### 2.4 Per-strategy universe rules

| Strategy | Universe | Size cap | Selection rule |
|---|---|---|---|
| S1 / S2 | F&O stocks passing affordability + daily regime filter | `max_trades_per_day` = 3 | Affordability: ≥1 lot within `RISK_CONFIG["max_risk_pct"]` (2%) of ₹200,000 capital |
| S3 | All F&O stocks with a valid prior-day daily candle | none | No affordability prefilter; affordability checked at signal time only |
| S4 | Buy-zone IV-slope leaderboard | `MAX_SCAN` = 40 | `iv_analytics.buy_zone_leaderboard()` — see §10 |
| S5 | Liquid-trimmed F&O | 120 | Top 120 by OI × volume |
| S6 | First N of the F&O dict | `DEFAULT_UNIVERSE_SIZE` = 30 | **Positional slice, not ranked** — see divergence below |
| S7 | All symbols with ≥15 daily ATM IV samples in `iv_history` | `DEFAULT_UNIVERSE_SIZE` = 30 (declared, not applied in `scan_all_underlyings`) | Ranked by IV percentile descending |
| S8 | Engine watchlist from `engine_decisions` | `PAPER_MAX_TRADES` = 5/day | Grades A+ and A only |

> **⚠ DIVERGENCE — S6 universe selection**
> **As-built:** `DirectionalIVScanner._build_universe()` returns `dict(symbols[:DEFAULT_UNIVERSE_SIZE])` — the first 30 entries of the F&O dictionary in insertion order.
> **Intent:** A universe cap implies selecting the *most suitable* 30 names, as S5 does by liquidity.
> **Impact:** S6's universe is effectively arbitrary and unstable — it depends on dictionary insertion order from the scrip-master load, not on any tradeability criterion.

### 2.5 Fixed or dynamic

**Dynamic, refreshed daily.** `[CODE]` The NSE F&O list is re-fetched and the scrip master re-loaded each morning (`data/.instruments_refreshed_date`, last 2026-08-03 08:46). The liquidity ranking that drives S5's trim recomputes each scan from live `iv_history` data.

### 2.6 Maximum symbols monitored

- **208** stock symbols have IV collected continuously by `iv-collector`. `[DATA]`
- **120** is the largest per-scan trading universe (S5). `[CODE]`
- **30** for S6 and S7. `[CODE]`

### 2.7 Lot sizing source

`momentum_strategy.ScripMasterLotSizer` reads `data/api-scrip-master.db`. A hardcoded fallback table covers four symbols whose names do not match the scrip-master regex, plus four index fallbacks: `[CODE]`

```python
LOT_SIZE_FALLBACK = {
    "PPLPHARMA": 1800, "TORNTPOWER": 750, "TATATECH": 475, "HUDCO": 2000,
    "NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65, "MIDCPNIFTY": 75,
}
```

If neither source resolves, lot size degrades to `1`. `paper_trader._finalize()` treats `lot_size is not None` (not `lot > 1`) as the real-trade sentinel, so a degraded lot of 1 still incurs the full fee model. `[CODE]`

---

## 3. Market Data

### 3.1 Broker split

**All market data comes from Upstox** via `upstox_adapter.UpstoxDhanAdapter` — a shim that presents Upstox responses in the Dhan response shape, because the internal data contract was originally written against Dhan. Dhan itself is **not** used for data. `[CODE]`

Upstox is accessed via **raw REST**, not the Upstox Python SDK — the SDK was found to hang. `[OPERATOR]`

### 3.2 Datasets consumed

| Dataset | Source | Update frequency | History stored | Storage format |
|---|---|---|---|---|
| Option chain (full, per expiry) | Upstox `/v2/option/chain` | Continuous sweep (iv-collector); on-demand every 15 min (S5); on-demand per scan (S4, S6, S7) | Not stored raw | Transient |
| ATM IV — intraday | Derived from chain by `iv-collector` | ~every 15 min per symbol | Unbounded, no purge | SQLite `iv_history.db` → `iv_history` table, `data_type='intraday'` |
| ATM IV — daily | Derived from chain by `iv-collector` | 1 snapshot/day/symbol | Unbounded, no purge | SQLite `iv_history`, `data_type='daily'` |
| Per-strike CE/PE IV (skew) | `iv-collector`, ±7 strikes around ATM | Every collector pass | From first skew-capable run only — **no backfill exists** | SQLite `skew_snapshots.strikes_json` |
| Spot / LTP (underlying) | Chain payload `last_price` | Per chain fetch | Stored alongside IV rows (`spot_price`) | SQLite `iv_history` |
| Option LTP / bid / ask | Upstox quote via `get_current_option_premium` | Every 5 min per open position | Only latest, per trade (`last_price`) | SQLite `paper_trades.db` |
| Open interest (option, per strike) | Chain payload | Per chain fetch | Not stored per strike | Transient |
| Volume (option, per strike) | Chain payload | Per chain fetch | Not stored per strike | Transient |
| Greeks (delta, vega) | Chain payload `greeks` object | Per chain fetch | Not stored | Transient |
| 15-min candles (underlying) | Upstox historical/intraday candles | Per scan | Cached | `data/backtest_candles.db` for backtest; transient live |
| 5-min candles (underlying) | Upstox | Per scan (S3 retest, Sonar) | Cached | `candles_5m` table |
| Daily candles (underlying) | Upstox | Once per day per stock (`CACHE_DAILY_CANDLES=True`) | Cached | Transient/cached |
| Aggregate OI buildup | `oi_buildup_scanner` from chain data | Per scan | Unbounded | SQLite `oi_buildup_history` |
| IV rank / IV percentile | `iv_rank_scanner` | 09:45, 12:30, 15:20 | Unbounded | SQLite `iv_rank_history` |
| Composite conviction | `composite_scanner` | Nightly + intraday | Unbounded | SQLite `composite_history` + `data/composite_conviction.csv` |
| Sonar (Laplace S/R) | `sonar_laplace_scanner` from 5-min candles | Per scan | Unbounded | SQLite table + `data/sonar_laplace_opportunities.csv` |
| Market breadth | `breadth.compute()` from `iv_history` spot snapshots | Per gate evaluation | Not persisted | Computed on demand |
| Sector map | `data/sector_mapping.db` | Static (last modified 2025-07-30) | n/a | SQLite |
| Delivery % / surge | `delivery_surge_scanner` | Daily | Unbounded | SQLite + CSV |
| Bulk/block deals | `deals_collector` | Daily (EOD) | Unbounded | SQLite |
| Gap data | `gap_scanner` | 09:45 | Unbounded | SQLite + CSV |
| India VIX | `collectors/vix_collector.py` | Periodic | Stored | SQLite |
| Bhavcopy | `collectors/bhav_collector.py` | Daily EOD | Stored | SQLite |

### 3.3 Datasets explicitly NOT consumed

`[CODE]` / `[DERIVED]` — confirmed absent from the codebase:

- **News.** No news collector exists.
- **Corporate actions.** No collector.
- **Economic / event calendar.** Explicitly absent. `iv_analytics.py` documents this: *"NO economic-calendar collector exists in this system, so event dates are NOT known."* The IV-slope signal is described as a *proxy* for pre-event positioning, detected from IV data rather than from a calendar.
- **Order-book depth beyond L1.** Only top bid / top ask are read.
- **Tick data.** The finest granularity consumed is the 5-minute monitor sample.
- **Fundamentals / market cap.** No fundamental filters anywhere.

### 3.4 Storage sizes (2026-08-03)

`[DATA]`

| File | Size | Purpose |
|---|---|---|
| `data/iv_history.db` | 315 MB (+5.2 MB WAL) | Central IV + scanner history store |
| `data/api-scrip-master.db` | 101 MB | Instrument master |
| `data/complete.db` | 40.6 MB | Full instrument dump |
| `data/paper_trades.db` | 7.0 MB | Trade book + scan log |
| `data/backtest_candles.db` | 155 KB | Backtest candle cache |
| `data/backtest_results.db` | 41 KB | Backtest outputs |

### 3.5 Sole-writer contract

Only `iv-collector` writes `iv_history` rows via `iv_store.save_snapshot`. Each scanner service writes only its own `*_history` table. All SQLite access must route through `iv_store.connect()`, which sets WAL journal mode and `busy_timeout=30000`. `[CODE]`

### 3.6 API rate limiting

`[CODE]` — `discount_config.py`:

```
UPSTOX_MAX_REQ_PER_SEC       = 7      # Upstox limits: 50/s, 500/min, 2000/30min
UPSTOX_MIN_REQ_INTERVAL_SEC  = 1/7
CHAIN_CALLS_30MIN_BUDGET     = 1500   # soft budget; leaves room for iv-collector
CHAIN_API_MAX_RETRIES        = 3
CHAIN_API_RETRY_BACKOFF_SEC  = 4.0
CHAIN_API_BACKOFF_MULTIPLIER = 2      # → 4s, 8s, 16s
```

Retries fire on rate-limit responses, empty option chains, empty expiry lists, and transient network errors. Legacy Dhan throttle constants (`CHAIN_API_MIN_INTERVAL_SEC = 3.1`) remain in the file but are superseded. `[CODE]`

Observed failure mode in production: `RemoteDisconnected('Remote end closed connection without response')` on `/v2/option/chain`, recovered by retry. `[DATA]`

---

## 4. Indicators / Features

### 4.1 EMA (Exponential Moving Average)

| Field | Value |
|---|---|
| **Why** | Trend/regime classification — establishes whether a directional bet is with or against the prevailing daily trend |
| **Inputs** | Daily close series of the underlying |
| **Parameters** | S1/S2: `ema_fast`=20, `ema_slow`=50. S6: `ema_fast`=9, `ema_mid`=20, `ema_slow`=50, `ema_long`=200 |
| **Timeframe** | Daily |
| **Formula** | `pandas.Series.ewm(span=N, adjust=False).mean()` — standard EMA, no customisation |
| **Minimum history** | S6 requires ≥200 bars (`ema_long`); returns `neutral` below that |

S6 two-tier classification: `[CODE]`
```
strong_bullish = close > ema9 > ema20 > ema50 > ema200
weak_bullish   = (not strong_bullish) and close > ema20 and ema9 > ema20
strong_bearish = close < ema9 < ema20 < ema50 < ema200
weak_bearish   = (not strong_bearish) and close < ema20 and ema9 < ema20
else           = neutral
```
Additional conviction knob `min_trend_gap_pct = 0.4` (minimum gap between price and EMAs). `[CODE]`

### 4.2 ADX (Average Directional Index)

| Field | Value |
|---|---|
| **Why** | Confirms the EMA-stack trend is *strong* enough to trade, not merely ordered |
| **Parameters** | `adx_min` = 25 (minimum to confirm trend), `adx_strong` = 30 (above → regime tagged STRONG) |
| **Timeframe** | Daily |
| **Used by** | S1, S2 only |
| **Formula** | `[NOT SPECIFIED]` — the ADX period is not exposed in `momentum_config.py`. The implementation lives in `momentum_strategy.py`; the lookback used is not a configurable constant. An implementer must confirm the period (standard is 14). |

### 4.3 VWAP (Volume Weighted Average Price)

| Field | Value |
|---|---|
| **Why** | Intraday fair-value reference; reclaim/loss is the S2 trigger |
| **Inputs** | Intraday candles fetched for the session |
| **Timeframe** | 15-min (default intraday interval) |
| **Formula** | Cumulative `Σ(typical_price × volume) / Σ(volume)`, where typical price = (H+L+C)/3 |
| **Source** | **Computed locally** from fetched candles — *not* pulled from the broker. Anchored at the first candle in the fetched window, which is the session open. |

### 4.4 Volume ratio

| Field | Value |
|---|---|
| **Why** | Participation confirmation — a breakout on thin volume is treated as untrustworthy |
| **Formula** | `last_candle.volume / mean(previous 5 candles' volume)` |
| **Threshold** | S1 (ORB): `ORB["volume_mult"]` = **1.5**. S2 (VWAP): **1.3**, hardcoded in `check_vwap_signal`, *not* in config |
| **Timeframe** | 15-min |

> **⚠ DIVERGENCE — S2 volume threshold**
> **As-built:** The 1.3 volume multiple for the VWAP signal is a literal in `momentum_strategy.check_vwap_signal`.
> **Intent:** `momentum_config.py` is documented as *"All numeric constants for the momentum strategy. No business logic."*
> **Impact:** The VWAP volume gate cannot be tuned or environment-overridden without a code edit, unlike every sibling parameter.

### 4.5 Historical Volatility (HV)

| Field | Value |
|---|---|
| **Why** | The denominator of the core cheapness test — an option is "cheap" when its IV sits below the underlying's own realised volatility |
| **Inputs** | Daily close series |
| **Parameters** | Multi-window; combined into a `weighted_hv` |
| **Used by** | S5 (`hv_score`), S6 (`_iv_edge_score`) |
| **Formula (S5 `hv_score`)** | `hv_edge_pct = ((weighted_hv − current_iv) / weighted_hv) × 100`, then `hv_score = clip(50 + hv_edge_pct × 2, 0, 100)` |
| **Formula (S6 `_iv_edge_score`)** | `diff = ((weighted_hv − current_iv) / weighted_hv) × 100`, then `clip(diff × 1.5, 0, 100)` |
| **Window composition** | `[NOT SPECIFIED]` — the individual HV lookback windows and their blend weights are computed inside `discount.py`'s HV metrics helper and are not surfaced as named constants. An implementer must extract them from source. |

### 4.6 IV Rank

| Field | Value |
|---|---|
| **Why** | Cheapness of current IV against the symbol's own recent range |
| **Inputs** | Daily ATM IV series from `iv_history` |
| **Lookback** | `LOOKBACK_DAYS` = 252 (adaptive — uses whatever exists up to the cap) |
| **Minimum samples** | S5: `MIN_IV_SAMPLES` = 30 (below this the scanner falls back to skew-only mode). S7/iv-rank scanner: `MIN_HISTORY_DAYS` = 15 |
| **Formula** | `(current_IV − min_IV) / (max_IV − min_IV) × 100` |
| **Full-baseline flag** | ≥240 days → labelled "IV Rank"; below → "IV %ile (adaptive)" |

### 4.7 IV Percentile

| Field | Value |
|---|---|
| **Why** | More robust than IV Rank on short history — rank is hostage to a single min/max outlier |
| **Formula** | `count(historical IV < current IV) / count(history) × 100` |
| **Primary metric** | `iv_rank_config.PRIMARY_METRIC = "percentile"` — percentile drives zone classification and ranking, **not** rank |
| **Zones** | `BUY_ZONE_MAX` = 30 (≤30 → CHEAP), `SELECTIVE_MAX` = 55 (30–55 → FAIR), >55 → EXPENSIVE |
| **Display rule (separate)** | `iv_analytics.IVP_BUY_BELOW` = 20, `IVP_AVOID_ABOVE` = 80 |

> **⚠ DIVERGENCE — two different IV-percentile buy thresholds**
> **As-built:** `iv_rank_config.BUY_ZONE_MAX = 30` governs the iv-rank scanner's alerts and zone labels, while `iv_analytics.IVP_BUY_BELOW = 20` governs the `verdict` field. **S4 gates on the latter**, because `buy_zone_leaderboard()` filters on `verdict == "BUY"`.
> **Intent:** `iv_analytics.py`'s own docstring states the display thresholds are *"analytics/display only — the iv-rank scanner's own zone config drives alerts/composite, not these."*
> **Impact:** S4's live trading gate is 20, not the documented 30. This makes S4's candidate pool roughly a third narrower than the stated design and is a direct contributor to S4 never booking a trade (§19.4).

### 4.8 IV Slope (custom) — the volatility-expansion metric

Fully specified in §10. Summary: ordinary-least-squares slope of the last 4 daily ATM IV points, in IV points per day. `[CODE]`

### 4.9 Skew discount (custom)

| Field | Value |
|---|---|
| **Why** | Cross-sectional cheapness — is this strike's IV low relative to *its own chain's* ATM and neighbours, independent of time-series history |
| **Inputs** | Per-strike IV from the live chain, ATM reference IV, neighbouring-strike IVs |
| **Score formula** | `skew_score = clip(50 + skew_discount × 8, 0, 100)`; defaults to 50.0 when `skew_discount` is `None` |
| **Weight** | 0.40 in S5's volatility-trade composite — **the single heaviest component** |
| **Stability check** | `is_iv_stable()` — a strike's IV must sit within a **10%** band of the mean of its immediate neighbours, else flagged unstable |

### 4.10 Put–Call IV skew tilt (custom)

| Field | Value |
|---|---|
| **Why** | Downside-fear gauge |
| **Formula** | `mean(put IV at ATM−1..−wing) − mean(call IV at ATM+1..+wing)`, default `wing`=3 |
| **Panic flag** | latest tilt − day's mean tilt ≥ `TILT_PANIC_JUMP` (2.0 IV points) **and** tilt > 0 |
| **Data source** | `skew_snapshots` table; **no historical backfill exists** |
| **Used for trading?** | No — dashboard analytics only |

### 4.11 Expected move

| Field | Value |
|---|---|
| **Why** | Sanity-bounds strike selection — a strike further than the market's own implied move is a lottery ticket |
| **Formula (S8)** | Brenner–Subrahmanyam approximation: ATM premium ≈ `0.4 × spot × IV × √(T)`, i.e. ~0.4 × 1-sigma, scaled to contract DTE |
| **Derived gate — S5** | `STRIKE["max_expected_move_ratio"]` = 1.5 — reject strikes more than 1.5× the expected move away |
| **Derived gate — S6** | `IV_FILTER["max_expected_move_ratio"]` = 1.2 |
| **Derived gate — S8** | `MIN_EXPECTED_MOVE_PCT` = 0.8 — reject "dead-vol" names whose 1-day 1-sigma move is below 0.8% |
| **S5 relevance score** | Piecewise, peaking near ratio 0.75: <br>`ratio ≤ 1.0` → `clip(92 − (\|ratio−0.75\|/0.55)×42)` <br>`≤ 1.5` → `clip(78 − ((ratio−1.0)/0.5)×18)` <br>`≤ 2.5` → `clip(60 − ((ratio−1.5)/1.0)×22)` <br>`> 2.5` → `30.0` |

### 4.12 Delta

| Field | Value |
|---|---|
| **Source** | Broker-supplied `greeks.delta` from the option chain — **not** computed locally |
| **S5 tiering** | `0.15 ≤ \|Δ\| ≤ 0.40` → 100; `0.10–0.15` or `0.40–0.55` → 70; else → 25 |
| **S6 tiering** | `0.15 ≤ Δ ≤ 0.35` → 100; `0.10–0.15` or `0.35–0.50` → 80; else → 50 |
| **S5 hard floor** | `STRIKE["min_abs_delta"]` = 0.10 (waived in hedging mode) |
| **S6 hard band** | `min_delta`=0.18, `max_delta`=0.40 |
| **S7 target** | `STRANGLE_DELTA_TARGET` = 0.175 per leg (abs); straddle uses ATM (Δ≈0.5) with no delta knob |

### 4.13 Liquidity score

| Field | Value |
|---|---|
| **S5 formula** | `clip(log1p(max(OI,0)) × 12 + log1p(max(volume,0)) × 8, 0, 100)` |
| **S6 formula** | `min(100, log1p(OI) × 10 + log1p(volume) × 6)`; returns 0 if either OI or volume ≤ 0 |
| **Why logarithmic** | Compresses the enormous OI range so a mega-liquid name does not saturate the composite purely on size |

### 4.14 Sonar-Laplace support/resistance (custom)

| Field | Value |
|---|---|
| **Why** | Structural veto — do not buy calls into a fresh breakdown |
| **Inputs** | 5-minute candles of the underlying |
| **Method** | SuperSmoother-filtered price with Laplace-based level detection; emits `signal` ∈ {`BREAKOUT_UP`, `BREAKDOWN`, `REVERSAL_UP`, `REVERSAL_DOWN`, `FLAT`}, plus `trend`, `bias`, `support`, `resistance`, `slope_pct` |
| **Role** | Entry **veto** (never a side flip) and open-position risk warning; optional auto-exit |
| **Staleness rule** | Only same-calendar-day rows may veto; `get_latest_sonar` returns the latest row *ever*, so a date check is applied at every call site |

### 4.15 Composite conviction (custom)

| Field | Value |
|---|---|
| **Why** | The system's designated direction-aware read; fuses several independent scanners |
| **Inputs** | OI-buildup, smart-money, delivery-surge, gap |
| **Output** | `direction` ∈ {CE, PE}, `grade` ∈ {MODERATE, STRONG}, `score` |
| **Storage rule** | `composite_history` stores **only** MODERATE/STRONG directional rows; WEAK/NONE are dropped at scan time |
| **Consumer** | S4 — sole source of CE/PE for a direction-agnostic vega signal |

### 4.16 Convex engine conviction (custom)

Weighted factor sum, version-tagged `FORMULA_VER = "v2.1"`. `[CODE]`

| Factor | Weight (v2.1) | Weight (v2.0) | Reason for change |
|---|---|---|---|
| `W_TRIGGER` | 30.0 | 30.0 | — |
| `W_OI_FLOW` | 20.0 | 20.0 | — |
| `W_TREND` | 15.0 | 15.0 | — |
| `W_SECTOR_RS` | 10.0 | 10.0 | — |
| `W_INST_FLOW` | **0.0** | 10.0 | Replay: EOD bulk/block is a BTST-horizon signal used for 60-min bets; edge −0.52 when present vs −0.10 absent |
| `W_PREMIUM_VALUE` | **0.0** | 10.0 | Direction-neutral cheap-IV bonus was inflating a *directional* score |
| `W_GAP` | **0.0** | 5.0 | Continuation vote, but intraday gaps fade |

Modifiers: `CONFLUENCE_BONUS` = +10% when ≥3 factors agree (`CONFLUENCE_MIN_AGREE` = 3); `VIX_ELEVATED_PENALTY` = −15%. Grades: A+ ≥75, A ≥60, B ≥45; size multipliers A+ 1.0, A 1.0, B 0.5. `[CODE]`

This is the only component of the system with a **documented out-of-sample validation**: replay over 38,000 labelled decisions, train 2026-07-03 to 07-16, validate 07-17 to 07-23. Result: monotone grade ladder on train, top-grade positive on validation. Zero-weighted factors still journal their votes so evidence continues accruing for possible re-inclusion. `[CODE]`

### 4.17 Market breadth

| Field | Value |
|---|---|
| **Source** | `breadth.compute()` over `iv_history` spot snapshots + `sector_mapping.db` |
| **Cost** | Zero broker calls |
| **Outputs** | `market_pct` (% advancing), per-sector breadth |
| **Engine thresholds** | `BREADTH_BULL` = 55.0 (% advancers for a CE lean), `BREADTH_BEAR` = 45.0 |

### 4.18 India VIX

| Field | Value |
|---|---|
| **S1/S2** | `REGIME["vix_max"]` = 22 — skip all trades above |
| **S8** | `VIX_RED` = 22.0 (no-trade), `VIX_ELEVATED` = 18.0 (amber), `VIX_CALM` = 13.0; size multipliers GREEN 1.0 / AMBER 0.5 / RED 0.0 |

### 4.19 Indicators explicitly NOT used

`[DERIVED]` — searched and confirmed absent: RSI, ATR, MACD, GMMA, Bollinger Bands, Keltner Channels, Anchored VWAP (beyond session VWAP), Volume Profile, Ichimoku, Supertrend, Fibonacci levels.

---

## 5. Timeframes

| Role | Timeframe | Used by | Notes |
|---|---|---|---|
| **Primary (signal)** | 15-minute | S1, S2, S3 (breakout leg) | `check_orb_signal`, `check_vwap_signal`, `check_15min_breakout` |
| **Primary (signal)** | Daily ATM IV | S4, S5, S6, S7 | Signals derive from the daily IV series, not from price bars |
| **Confirmation** | 5-minute | S3 (retest entry), Sonar | `check_5min_entry`, `sonar_laplace_scanner` |
| **Higher timeframe** | Daily | S1, S2 (EMA/ADX regime), S3 (prior-day H/L), S6 (EMA stack) | |
| **Lower timeframe** | 5-minute | All — position monitoring | `INTRADAY["monitor_interval_min"]` = 5 |
| **Scan cadence** | 15-minute | S5 | `INTRADAY["scan_interval_min"]` = 15 |
| **Scan cadence** | Discrete times | S4 (09:45, 11:00, 13:00), iv-rank (09:45, 12:30, 15:20) | |

### 5.1 Resampling method

**None.** All timeframes are requested natively from the Upstox candle API at the required interval. No local resampling or aggregation of a finer series into a coarser one occurs anywhere in the live path. `[CODE]`

### 5.2 Completed-candle rule

Every price-based signal evaluates the **latest completed candle**, never the forming one. `[CODE]` Explicitly documented in `check_orb_signal`, `check_vwap_signal`, `check_15min_breakout`, and `check_5min_entry`.

### 5.3 Timezone

All times are IST (`Asia/Kolkata`). Container `TZ` is set explicitly in `docker-compose.yml` for every service. `[CODE]`

---

## 6. Market Regime

### 6.1 Operator's stated regime hypothesis

Verbatim: `[OPERATOR]`

> "The system is hypothesized to perform best during directional intraday expansion regimes where price escapes a period of compression with increasing participation (volume/liquidity) and realized volatility expands after entry. The system is expected to underperform during low-range, mean-reverting, and highly noisy intraday markets. These are research hypotheses only and have not yet been statistically validated across sufficient historical data or market regimes."

The operator's explicit qualification — *"research hypotheses only... not yet been statistically validated"* — is a load-bearing part of this specification and must not be dropped by an implementer.

### 6.2 Regime detection — as implemented

| Mechanism | Definition | Where | Active? |
|---|---|---|---|
| **Daily EMA + ADX regime** | `price > EMA20 > EMA50` and `ADX ≥ 25` → STRONG bullish (mirrored for bearish) | S1/S2 | No (service off) |
| **India VIX ceiling** | Skip all trades if VIX > 22 | S1/S2 | No (service off) |
| **Engine regime state machine** | GREEN / AMBER / RED from VIX + breadth + NIFTY SuperSmoother index slope, with size multipliers 1.0 / 0.5 / 0.0 | S8 | Yes |
| **Breadth gate** | Block CE into a broadly-red tape, PE into green | Shared (`_apply_breadth_gate`) | **Off** (`BREADTH_GATE_MODE` default) |
| **Sonar signal** | Per-symbol structural state | Shared veto | Yes (veto), off (auto-exit) |
| **Trade-type classifier** | Routes a candidate to "directional" vs "volatility" scoring | S5 | Yes |

### 6.3 S5 trade-type classification (regime proxy)

`[CODE]` — `classify_trade_type()`:

```
is_strong_directional = (trend != "neutral")
                        AND (0.15 <= |delta| <= 0.45)
                        AND (iv_rank is None OR iv_rank >= 40)     # NOT cheap IV
if is_strong_directional:  return "directional"

is_volatility_trade   = (iv_rank is not None AND iv_rank < 40)
                        OR (skew_discount is not None AND skew_discount > 0.1)
return "volatility" if is_volatility_trade else None                # None → skipped
```

A candidate that is neither is dropped entirely.

> **⚠ DIVERGENCE — removal of the `iv_trend` gate**
> **As-built:** On 2026-07-30 the requirement `iv_trend <= 0.05` was removed from the volatility branch. Cheap-vs-peers (skew) or cheap-vs-own-history (iv_rank < 40) is now sufficient regardless of which way IV is trending.
> **Intent:** The original design deliberately reserved expanding-IV names for S4 (`vol_expansion_strategy.py`) to keep the two edges separable and independently measurable.
> **Impact:** S5 and S4 now overlap on expanding-IV names. Since S4 books nothing (§19.4), no double-booking occurs today — but the separation the measurement design depended on is gone, and if S4 is ever unblocked the two will compete for the same candidates. The stated reason for the change was that S4's liquidity floor was so tight the edge was going unexploited; that floor was independently loosened the same day (§7.4), so the justification no longer holds.

### 6.4 Expiry / non-expiry handling

| Rule | Value | Strategy |
|---|---|---|
| Minimum DTE (calendar days) | 5 | S5 (`MIN_DTE_DAYS`) |
| Minimum DTE (trading days) | 4 | S4 (`MIN_DTE`) |
| DTE band | 7–35 | S6 (`DTE_FILTER`) |
| DTE band | 7–15 | S7 swing (`DTE_MIN`/`DTE_MAX`) |
| DTE = 0 exactly | 0DTE mode | S7 (`ZERO_DTE_ENABLED` = true) |
| Minimum DTE | 1 | S8 (`PAPER_MIN_DTE`) |

S5's `MIN_DTE_DAYS = 5` interacts with a scanner limitation: **only the nearest expiry is examined**, so within 5 days of a monthly expiry the stock universe returns few or no ideas until the next expiry becomes nearest. This is documented in the config as a known effect, not a bug. `[CODE]`

**S7 0DTE mode** is the only regime-specific variant with its own parameter set: `[CODE]`

| Parameter | Swing | 0DTE | Rationale in source |
|---|---|---|---|
| Sell-zone minimum IVP | 65 | **70** | Stricter bar |
| SL credit multiple | 2.0 (−100% of credit) | **1.5** (−50%) | 0DTE gamma moves far faster |
| Target credit multiple | 0.35 (+65%) | **0.40** (+60%) | |
| Entry time | any | **≥ 10:30** | 0DTE gamma is worst right at the open |
| Expiry fallback | nearest ≥ DTE_MIN | **none** — must be today | If it isn't expiry day for a name, there is no 0DTE trade |

### 6.5 Trading-holiday calendar

`discount_config.NSE_HOLIDAYS = []` — **empty**. `get_actual_trading_days_to_expiry()` degrades gracefully to weekend-only logic when the list is empty. `[CODE]`

> **⚠ DIVERGENCE — holiday calendar**
> **As-built:** Empty list; trading-day DTE counts weekends only and treats every NSE holiday as a trading day.
> **Intent:** The config comment instructs *"Populate with the NSE equity-derivatives holiday list for the current year."*
> **Impact:** Every trading-day DTE calculation over-counts by the number of holidays in the window. S4's `MIN_DTE = 4` trading days and S5's holiday-aware helper are both affected — contracts are treated as having more time remaining than they do, systematically understating theta exposure.

### 6.6 Regimes NOT handled

`[DERIVED]` — no code path exists for:

- **Earnings.** No earnings-date collector; no earnings blackout. A strategy can and will buy premium into an earnings print without knowing it.
- **Special events** (RBI policy, Union Budget, election results). `iv_analytics` explicitly instructs the human to *"cross-check known event dates (RBI, budget, earnings) manually — no economic-calendar collector exists."*
- **Gap days.** A gap scanner exists and alerts, but no strategy conditions its behaviour on gap state. The Convex engine zeroed its gap weight entirely (§4.16).

---

## 7. Stock Selection Logic

### 7.1 Liquidity gates (per strike, per side)

`[CODE]` — these are the most divergent parameters in the system:

| Strategy | `min_oi` | `min_volume` | `max_spread_pct` | `min_atm_oi` |
|---|---|---|---|---|
| S1 / S2 | 500 | 200 | 0.05 (5%) | — |
| S3 | 500 | 200 | 0.05 (5%) | — |
| **S4** | **2,500** | **500** | **0.20 (20%)** | — |
| **S5 (strict, default)** | **5,000** | **100** | **0.12 (12%)** | — |
| S5 (loose, opt-in) | 1,000 | 1 | 0.12 | — |
| S6 | 2,500 | 500 | 0.20 (20%) | 500 |
| S7 | 2,500 | 500 | 0.20 (20%) | 500 |

S5 additionally requires a **live two-sided quote** — both `bid > 0` and `ask > 0`. Strikes quoted one-sided are skipped because they cannot be entered cleanly. `[CODE]`

The loose tier is opt-in via `discount_config.ALLOW_LOOSE_LIQUIDITY` (default `False`) or the `ALLOW_LOOSE_LIQUIDITY` environment variable. `[CODE]`

**Cosmetic annotation only** (drives alert wording, gates nothing): `STRONG_LIQUIDITY = {"oi": 10000, "volume": 1000}`. `[CODE]`

> **⚠ DIVERGENCE — liquidity floors are not harmonised**
> **As-built:** Spread tolerance ranges from 5% (S1/S2/S3) to 20% (S4/S6/S7) — a 4× spread. OI floors range 500 to 5,000 — a 10× spread.
> **Intent:** No design document justifies per-strategy liquidity differentiation; the S4 config comment describing its own former floor as *"a 20x-tighter floor than every sibling strategy... with no documented reason for the gap"* indicates harmonisation was the intent.
> **Impact:** The same contract can be tradeable for S4 and untradeable for S3 on the same tick. Cross-strategy performance comparison — the platform's stated purpose — is confounded by this, because differences in results partly reflect differences in what each strategy was allowed to touch.

### 7.2 Affordability filter

S1 / S2 only, at universe-construction time: a symbol qualifies if **at least 1 lot** can be bought within `RISK_CONFIG["max_risk_pct"]` (2%) of ₹200,000 capital. `[CODE]`

S3 performs no affordability prefilter; affordability is checked at signal time only. `[CODE]`

### 7.3 Premium floors

| Gate | Value | Scope |
|---|---|---|
| `INTRADAY["min_premium"]` | ₹5.00 | **Global default** — applies to every strategy landing in `book_signal` |
| `BB_PAPER["min_premium"]` | ₹0.50 | S3 per-signal override |
| `CFG.MIN_PREMIUM` (S4) | ₹5.00 | S4 per-signal override (same as default) |
| Hedge leg | ₹0.00 | Hedge legs set `min_premium: 0.0` to bypass the floor |

S3's override exists because a ₹1.80 NHPC option with a 6,950 lot is a valid B&B trade that the ₹5 floor would wrongly reject. `[CODE]`

### 7.4 Historical change record — S4 liquidity floor

`[CODE]` — recorded verbatim in `vol_expansion_config.py` because it is a documented, dated calibration decision:

> 2026-07-30: was `min_oi=50000` / `min_volume=1000` — a 20× tighter floor than every sibling strategy (iv_seller/directional_iv both use 2500/500) with no documented reason for the gap. Confirmed via live check that this was starving the strategy: that day's leaderboard had 2 genuine buy-zone names (TCS, VEDL) with fresh directional reads, and 0 trades booked across the first two daily scans. Aligned to the same floor the rest of the system uses.

**This fix did not resolve the starvation.** S4 has booked zero trades in the four sessions since. `[DATA]` The binding constraint was elsewhere — see §19.4.

### 7.5 Selection criteria NOT used

`[DERIVED]` — confirmed absent from all strategies: market capitalisation, any fundamental filter (P/E, revenue, debt), sector strength as a *selection* input (used only as a concentration *cap*), relative strength versus index, ATR-based selection, average turnover in rupee terms, delivery percentage as a *selection* input (a delivery-surge scanner exists and alerts, and feeds the composite, but does not select directly).

---

## 8. Trade Setup

### 8.1 S1 — Momentum ORB

| Aspect | Specification |
|---|---|
| **Prerequisite** | 09:00 premarket scan establishes daily regime (EMA/ADX) and affordability for every F&O name |
| **Setup formation** | The first `ORB["range_candles"]` = **2** completed 15-min candles (09:15–09:45) define the opening range high and low |
| **Sequence** | Regime → opening range → breakout candle → entry. Order is strict |
| **Validity window** | Until `entry_cutoff` 11:30 |
| **Invalidation** | Time (past 11:30); regime flip; VIX > 22; affordability failure; liquidity failure at the selected strike |

### 8.2 S2 — Momentum VWAP

| Aspect | Specification |
|---|---|
| **Prerequisite** | Same 09:00 premarket regime scan as S1 |
| **Setup formation** | No pre-formed structure. The setup *is* the two-candle transition across VWAP |
| **Sequence** | Prior completed candle on one side of VWAP → current completed candle closes on the other side |
| **Validity window** | Evaluated fresh every 5 min between 09:30 and 11:30; no carry-over state |
| **Invalidation** | The setup cannot go stale — it exists only on the tick it is detected |

### 8.3 S3 — Break and Bounce (three-step, stateful)

The only strategy with a genuinely multi-stage, persisted setup. `[CODE]`

**Step 1 — Premarket 09:00.** Cache yesterday's daily high and low for every F&O stock as `yesterday_high` / `yesterday_low` (`get_yesterday_levels`).

**Step 2 — 15-min breakout, window 09:15–11:45.** A *completed* 15-min candle closing above `yesterday_high` → state BULLISH; closing below `yesterday_low` → state BEARISH. Past `window_end` 11:45 the setup is voided and never revisited.

**Step 3 — 5-min retest entry.** After breakout confirmation, evaluated on the most recent completed 5-min candle.

**Setup lifetime:** `retest_expiry_minutes` = **60**. Retest monitoring expires 60 minutes after breakout confirmation. Introduced to prevent a stock staying in the 5-min scan loop all day when the retest never arrives or every validation gate keeps rejecting it. `[CODE]`

**Invalidation before entry:**
1. Breakout window (11:45) expires without a breakout → setup voided.
2. 60 minutes elapse post-breakout without a valid retest → setup expires.
3. `state["trade_placed"]` is already true for that symbol today → one trade per stock per day.
4. Liquidity or affordability failure at the selected strike.
5. EOD reset at 15:15 clears all state.

### 8.4 S4 — Volatility Expansion

| Aspect | Specification |
|---|---|
| **Prerequisite** | ≥3 daily ATM IV points in the last 7 calendar days; an IVP read with verdict `BUY`; a fresh composite direction row (or momentum fallback) |
| **Setup formation** | No intraday structure at all. The setup is a *state* of the daily IV series |
| **Sequence** | Leaderboard → direction resolution → expiry ≥4 trading days → ATM strike → liquidity → book |
| **Validity window** | Evaluated at 09:45, 11:00, 13:00; cutoff 13:30 |
| **Invalidation** | No directional lean and `REQUIRE_TREND=true`; per-symbol dedup via `_traded_today`; entry cutoff |

### 8.5 S5 — Discounted Premium

| Aspect | Specification |
|---|---|
| **Prerequisite** | Liquid universe trim; nearest expiry ≥5 calendar days out |
| **Setup formation** | Stateless. Every 15-min scan re-evaluates the full chain from scratch |
| **Sequence** | Universe → chain fetch → per-strike scoring → threshold → gates → book |
| **Validity window** | The scan tick only. Nothing persists between scans |
| **Invalidation** | `no_entry_after` 15:00; per symbol+strike+side dedup; per-symbol/day cap; all shared gates |

### 8.6 S6 / S7 / S8

- **S6:** Stateless per scan. Trend context → IV filter → delta band → DTE band → score ≥65 → book.
- **S7:** Stateless. IV-percentile candidate list → structure choice (strangle vs straddle by IVP) → expiry → both legs → book as a pair. A combo requires **both** legs; if either fails, neither is booked.
- **S8:** Engine emits a graded decision to `engine_decisions`; `engine/paper.py` books grades A+/A, highest score first, up to 5/day, entry cutoff 14:30.

---

## 9. Entry Rules

Every condition is listed separately. `M` = mandatory, `O` = optional/configurable.

### 9.1 S1 — Momentum ORB

| # | Condition | M/O |
|---|---|---|
| 1 | Symbol passes affordability (≥1 lot within 2% of ₹200,000) | M |
| 2 | Daily regime is bullish (`price > EMA20 > EMA50`) for CE, mirrored for PE | M |
| 3 | `ADX ≥ 25` | M |
| 4 | India VIX ≤ 22 | M |
| 5 | Current time ≥ 09:30 AND ≤ 11:30 | M |
| 6 | Latest **completed** 15-min candle `close > opening_range_high` → CE | M (CE branch) |
| 7 | Latest **completed** 15-min candle `close < opening_range_low` → PE | M (PE branch) |
| 8 | `last.volume / mean(prev 5 candles) ≥ 1.5` | M |
| 9 | Selected strike passes OI ≥ 500, volume ≥ 200, spread ≤ 5% | M |
| 10 | Signal ranks within `max_trades_per_day` = 3 after `MomentumSignalRanker` scoring | M |
| 11 | Open positions < `max_open_positions` = 2 | M |

**Ranking (when multiple signals fire):** `[CODE]`
```
+40  regime STRONG
+20  regime WEAK
+30  direction aligned with regime
+10  trigger == ORB
+ 5  volume ratio >= 2.0
```
Only the top `max_trades_per_day` signals are taken.

### 9.2 S2 — Momentum VWAP

Conditions 1–5, 9–11 identical to S1. Trigger differs:

| # | Condition | M/O |
|---|---|---|
| 6a | **CE (vwap_reclaim):** `prev.close < prev.vwap` AND `last.close > last.vwap` | M |
| 6b | **PE (vwap_break):** `prev.close > prev.vwap` AND `last.close < last.vwap` | M |
| 7 | `last.volume / mean(prev 5) ≥ 1.3` (hardcoded) | M |

### 9.3 S3 — Break and Bounce

| # | Condition | M/O |
|---|---|---|
| 1 | Yesterday's daily candle exists and is valid | M |
| 2 | Time within 09:15–11:45 for the breakout leg | M |
| 3 | A completed 15-min candle closed beyond yesterday's H (bullish) or L (bearish) | M |
| 4 | ≤60 minutes have elapsed since breakout confirmation | M |
| 5 | On the latest completed 5-min candle, `last.low` is within `retest_tol_pct` = **0.3%** of yesterday's high (bullish); mirrored for bearish | M |
| 6a | **Hammer:** lower wick ≥ `hammer_wick_ratio` × body (**2.0**) AND upper wick ≤ `max_counter_wick` × body (**0.5**) AND preceded by ≥2 red candles falling into the level. Entry = `last.close`, SL = `last.low` | M (one of 6a/6b) |
| 6b | **Bullish engulfing:** `curr.low < prev.low` AND `curr.high > prev.high` AND curr is bullish. Entry = `prev.high`, SL = `last.low` | M (one of 6a/6b) |
| 7 | Bearish mirror: inverted hammer with ≥2 prior green candles, or bearish engulfing | M (bearish branch) |
| 8 | `state["trade_placed"]` is False for this symbol today | M |
| 9 | Strike passes OI ≥ 500, volume ≥ 200, spread ≤ 5% | M |
| 10 | Premium ≥ ₹0.50 | M |
| 11 | 1-lot risk ≤ ₹1,500 | M |

### 9.4 S4 — Volatility Expansion

| # | Condition | M/O |
|---|---|---|
| 1 | `MODE != "off"` (currently `paper`) | M |
| 2 | Time < `ENTRY_CUTOFF` 13:30 | M |
| 3 | Symbol appears in `buy_zone_leaderboard()`: IV slope ≥ **0.5** IV pts/day AND IVP verdict == `BUY` (IVP < 20) | M when `BUY_ZONE_ONLY=true` |
| 4 | Symbol not already in `_traded_today` | M |
| 5 | A `security_id` resolves for the symbol | M |
| 6 | Direction resolves: composite row within **4 days** with grade ≥ MODERATE | M (primary) |
| 7 | Fallback: spot drift over **6** deduplicated daily samples exceeds ±**2.0%** | O (`COMPOSITE_FALLBACK_MOMENTUM=true`) |
| 8 | If no direction and `REQUIRE_TREND=true` → skip | M |
| 9 | An expiry exists with ≥**4** trading days | M |
| 10 | ATM strike (`STRIKE_OTM_OFFSET` = 0) resolves in the chain | M |
| 11 | Entry premium ≥ ₹5.00 | M |
| 12 | OI ≥ 2,500 AND volume ≥ 500 AND spread ≤ 20% of mid | M |
| 13 | All shared gates pass (§15) | M |

`MAX_TRADES_PER_DAY` = **0** (unlimited). `[CODE]`

### 9.5 S5 — Discounted Premium

| # | Condition | M/O |
|---|---|---|
| 1 | `PAPER_TRADING_ENABLED` is True | M |
| 2 | Time ≥ 09:30 AND < `no_entry_after` 15:00 | M |
| 3 | Symbol in the top-120 liquid universe | M |
| 4 | Nearest expiry ≥ 5 calendar days | M |
| 5 | Strike has a live two-sided quote (bid > 0 AND ask > 0) | M |
| 6 | OI ≥ 5,000 AND volume ≥ 100 AND spread ≤ 12% | M |
| 7 | `\|delta\| ≥ 0.10` | M |
| 8 | Expected-move ratio ≤ 1.5 | M |
| 9 | IV is stable — within 10% of the neighbouring-strike mean | M |
| 10 | `classify_trade_type()` returns "directional" or "volatility", not `None` | M |
| 11 | Composite score ≥ `MIN_SCORE` = **55** | M |
| 12 | **`iv_rank ≥ MIN_IV_RANK` = 25** | M |
| 13 | Entry premium ≥ ₹5.00 | M |
| 14 | 1-lot risk ≤ ₹1,500 | M |
| 15 | Not already booked: same symbol+strike+side today | M |
| 16 | `count_symbol_today(symbol) < max_per_symbol_per_day` = **1** | M |
| 17 | Sonar is not `FLAT` for the symbol (same-day rows only) | M |
| 18 | Sonar does not contradict: no bullish signal on a PUT, no bearish signal on a CALL | M |
| 19 | All shared gates pass | M |

`max_signals_per_day` = **0** (unlimited). `[CODE]`

**Scoring — volatility trade type:** `[CODE]`
```
raw = hv_score×0.30 + skew_score×0.40 + delta_score×0.10
    + liquidity_score×0.10 + relevance_score×0.20
```
**Scoring — directional trade type:**
```
raw = hv_score×0.25 + delta_score×0.35 + liquidity_score×0.10
    + skew_score×0.15 + relevance_score×0.25
```
**Directional confirmation blend** (`ENABLE_DIRECTIONAL_CONFIRMATION=True`, `DIRECTIONAL_WEIGHT=0.15`):
```
final_score = base_score × 0.85 + directional_score × 0.15
```
**Futures OI confirmation:** `ENABLE_FUTURES_OI_CONFIRMATION = False`. When enabled, adds `FUTURES_OI_BONUS` = 10 to `directional_score` (then clipped 0–100) on a confirming futures buildup. Fail-open. `[CODE]`

### 9.6 S6 — Directional IV

| # | Condition | M/O |
|---|---|---|
| 1 | Symbol within the first 30 of the F&O dict | M |
| 2 | Trend context is not `neutral` (requires ≥200 daily bars) | M |
| 3 | Expiry within DTE 7–35 | M |
| 4 | ATM IV ≤ **45.0** | M |
| 5 | IV rank ≤ **65** | M |
| 6 | Expected-move ratio ≤ **1.2** | M |
| 7 | Moneyness ≤ **2.5%** | M |
| 8 | `0.18 ≤ delta ≤ 0.40` | M |
| 9 | OI ≥ 2,500, volume ≥ 500, ATM OI ≥ 500, spread ≤ 20% | M |
| 10 | Composite score ≥ `MIN_SCORE` = **65** | M |
| 11 | IV rank ≤ `buy_zone_max_ivr` = 35 | O (`buy_zone_only` = **false**) |
| 12 | Trades/day < 2, open positions < 2 | M |

**Scoring — weighted, capped at 100:** `[CODE]`
```
trend_alignment × 1.3  +  delta_score    × 1.1  +  iv_edge_score × 1.0
+ liquidity_score × 0.9  +  iv_rank_score × 0.9  +  moneyness_score × 0.7
+ expiry_score    × 0.5
```
`trend_alignment` is binary: 100.0 if (bullish AND CALL) or (bearish AND PUT), else 0.0. `[CODE]`

### 9.7 S7 — IV Seller

| # | Condition | M/O |
|---|---|---|
| 1 | ≥15 daily ATM IV samples (`MIN_HISTORY_DAYS`) | M |
| 2 | IV percentile ≥ `SELL_ZONE_MIN` = **65** (swing) or **70** (0DTE) | M |
| 3 | Structure: IVP ≥ `STRADDLE_MIN_PCT` = **85** → ATM straddle; 65–85 → OTM strangle | M |
| 4 | Expiry within DTE 7–15 (swing) or DTE == 0 exactly (0DTE, no fallback) | M |
| 5 | ATM call OI ≥ 500 **OR** ATM put OI ≥ 500 | M |
| 6 | Each leg passes OI ≥ 2,500, volume ≥ 500 | M |
| 7 | Each leg's spread ≤ 20% of mid | M |
| 8 | Strangle: strike chosen by `min\|delta − 0.175\|` among liquid strikes with delta > 0 | M (strangle) |
| 9 | Both CE and PE legs must fill — a one-legged combo is discarded | M |
| 10 | Entry price (= **bid**, what selling actually fills at) > 0 | M |
| 11 | 0DTE only: current time ≥ **10:30** | M (0DTE) |
| 12 | Each leg: premium ≥ ₹5.00 | M |
| 13 | Each leg: 1-lot risk ≤ ₹1,500 | M — **this is the blocking condition, see §19.4** |

### 9.8 S8 — Convex

| # | Condition | M/O |
|---|---|---|
| 1 | `ENGINE_PAPER_MODE == "paper"` | M |
| 2 | Decision grade ∈ `PAPER_GRADES` = {A+, A} | M |
| 3 | Regime not RED (VIX ≤ 22) | M |
| 4 | 1-day expected ATM move ≥ `MIN_EXPECTED_MOVE_PCT` = 0.8% | M |
| 5 | DTE ≥ 1 | M |
| 6 | Target gain ≥ `PAPER_MIN_RR` = 1.0 × the SL's rupee risk | M |
| 7 | Trades today < `PAPER_MAX_TRADES` = 5 | M |
| 8 | Time < `ENTRY_CUTOFF` 14:30 | M |
| 9 | Concurrent positions < `MAX_CONCURRENT` = 3 | M |
| 10 | SL hits today < `MAX_SL_HITS_PER_DAY` = 2 | M |

S8 sets `skip_risk_cap=True` and bypasses the pre-market / breadth / concentration gates deliberately, "so the engine's own conviction is what gets measured." `[CODE]`

---

## 10. Volatility Expansion

This section is specified exhaustively because it is the system's most distinctive custom metric and the operator flagged it as requiring maximum detail.

### 10.1 Exact definition

**Volatility expansion is defined as a positive ordinary-least-squares slope of the underlying's daily ATM implied volatility series over a trailing 4-day window, measured in IV points per day.** `[CODE]`

It is a pure **implied**-volatility construct. It is explicitly **not**:

| Not this | Confirmation |
|---|---|
| Realised / historical volatility expansion | HV is used elsewhere (§4.5) but not in this metric |
| ATR expansion | ATR is not computed anywhere in the codebase |
| Price range expansion | No range metric enters the slope |
| Bollinger/Keltner squeeze release | Neither indicator exists |
| IV percentile alone | IVP is a *separate, additional* filter layered on top |
| Event-driven, calendar-derived | No event calendar exists — see §10.6 |

### 10.2 Measurement — exact algorithm

`[CODE]` — `iv_analytics._expansion_rows()`:

**Step 1 — Data pull.**
```sql
SELECT symbol, date(timestamp) AS d, atm_iv
FROM   iv_history
WHERE  data_type = 'daily'
  AND  atm_iv BETWEEN 1 AND 200
  AND  date(timestamp) >= date('now', '-{lookback_days + 3} days')
ORDER  BY symbol, d ASC
```
With `lookback_days = 4`, the SQL window is **7 calendar days** — three days of slack so that four *trading* days survive a weekend.

**Step 2 — Truncate.** Per symbol, keep the last `lookback_days` = 4 values: `ivs = ivs[-4:]`.

**Step 3 — Minimum sample.** If `n = len(ivs) < 3`, the symbol is dropped entirely. Minimum is 3 points, not 4.

**Step 4 — OLS slope against an integer index.**
```python
n     = len(ivs)
xm    = (n - 1) / 2                                    # mean of [0..n-1]
ym    = sum(ivs) / n
denom = sum((i - xm)**2 for i in range(n))
slope = sum((i - xm) * (ivs[i] - ym) for i in range(n)) / denom   if denom else 0.0
```
This is textbook OLS `Σ(x−x̄)(y−ȳ) / Σ(x−x̄)²`. **The x-axis is the integer position `0,1,2,…`, not the calendar date.** A four-day window spanning a weekend is treated as four evenly spaced points.

**Step 5 — Auxiliary fields.**
```python
chg_pct   = (ivs[-1] - ivs[0]) / ivs[0] * 100   if ivs[0] else 0.0
expanding = slope > 0.5
```

**Step 6 — Sort** descending by `slope_iv_pts_per_day`.

**Output record:**
```json
{
  "symbol": "TCS",
  "slope_iv_pts_per_day": 1.24,
  "iv_start": 18.2,
  "iv_now": 22.1,
  "change_pct": 21.4,
  "n_days": 4,
  "expanding": true
}
```

### 10.3 Thresholds

| Threshold | Value | Where | Meaning |
|---|---|---|---|
| `expanding` flag | slope > **0.5** | `_expansion_rows` (hardcoded literal) | Display/leaderboard flag |
| `MIN_SLOPE` | **0.5** | `vol_expansion_config` (env `VOL_EXP_MIN_SLOPE`) | S4 trading gate |
| `LOOKBACK_DAYS` | **4** | `vol_expansion_config` (env `VOL_EXP_LOOKBACK_DAYS`) | Window |
| `MAX_SCAN` | **40** | `vol_expansion_config` | Names considered per scan |
| IVP buy verdict | IVP < **20** | `iv_analytics.IVP_BUY_BELOW` | The *binding* cheapness gate |

> **⚠ DIVERGENCE — duplicated expansion threshold**
> **As-built:** `0.5` appears twice — as a hardcoded literal `expanding = slope > 0.5` inside `_expansion_rows()`, and as the env-overridable `MIN_SLOPE` in `vol_expansion_config`.
> **Intent:** Single source of truth; every other S4 parameter is env-overridable.
> **Impact:** Setting `VOL_EXP_MIN_SLOPE` changes the trading gate but *not* the `expanding` flag shown on the dashboard and used by the non-buy-zone code path (`BUY_ZONE_ONLY=false`, which filters on `r.get("expanding")`). The two can disagree silently.

### 10.4 The combined buy-zone rule

`[CODE]` — `iv_analytics.buy_zone_leaderboard()`:

```
1. rows = [r for r in _expansion_rows(4) if r.slope >= min_slope(0.5)][:scan_n(40)]
2. for each row: look up iv_percentile(symbol)
3. keep ONLY rows where verdict == "BUY"        # i.e. IVP < IVP_BUY_BELOW (20)
4. buy_score = slope * (1.0 - IVP/100.0)
5. sort by buy_score descending, return top `limit` (40)
```

**Rationale, verbatim from source:** `[CODE]`

> "The tradeable pattern is the COMBINATION: IV climbing (slope) while STILL cheap on 52-week history (IVP in the buy zone) → long premium can win on Vega before the event even resolves. EXPANDING but already-rich names are chase entries — vol crush eats the edge."

> "`buy_score = slope * (1 - IVP/100)`: rewards a steeper climb AND a lower percentile, so a cheap name that is climbing outranks a rich name climbing faster (the latter is a vol-crush chase, not a buy)."

### 10.5 Why this definition was chosen

`[CODE]` — the source states the reasoning directly:

> "NO economic-calendar collector exists in this system, so event dates are NOT known. What IS in hand: the 3-4 day slope of daily ATM IV per symbol. A steep positive slope IS the 'climbing IV into an event' signature — detected from data, not from a calendar. Labelled honestly as such."

The definition is therefore a **deliberate proxy chosen under a data constraint**, not a first-choice formulation. The intended construct is "IV rising into a known catalyst"; the implemented construct is "IV rising, cause unknown."

The source additionally instructs the human operator: *"Still cross-check event dates (RBI, budget, earnings) manually — no economic-calendar collector exists."* This manual step is a documented, unautomated part of the strategy. `[CODE]`

### 10.6 Direction resolution — because the signal is direction-agnostic

A rising-IV signal is a **vega** signal and says nothing about which way price will go. S4 nonetheless books a **single directional leg**, so a side must be imported from elsewhere. `[CODE]`

**Priority 1 — Composite conviction** (`DIRECTION_SOURCE = "composite"`, the default):
```sql
SELECT direction, grade FROM composite_history
WHERE symbol = ? AND direction IN ('CE','PE')
  AND timestamp >= datetime('now','localtime','-4 days')
ORDER BY timestamp DESC LIMIT 1
```
Accepted grades: `COMPOSITE_MIN_GRADE = "MODERATE"` admits {MODERATE, STRONG}; setting it to `"STRONG"` admits only STRONG. `COMPOSITE_MAX_AGE_DAYS = 4` covers the overnight and weekend gap between the evening composite scan and the next morning's entry.

**Priority 2 — Momentum fallback** (`COMPOSITE_FALLBACK_MOMENTUM = true`):
```sql
SELECT spot_price FROM iv_history AS h
WHERE h.symbol = ? AND h.data_type='daily' AND h.spot_price > 0
  AND h.rowid = (SELECT MAX(i2.rowid) FROM iv_history i2
                 WHERE i2.symbol=h.symbol AND i2.data_type='daily'
                   AND DATE(i2.timestamp)=DATE(h.timestamp))
ORDER BY h.timestamp DESC LIMIT 6
```
The `MAX(rowid)` subquery **deduplicates to one spot per calendar day** (last row of the day). This hardening exists because a polluted `iv_history` with multiple 'daily' rows per day would otherwise silently collapse the intended 6-day window into a handful of intraday points and turn the direction into noise. `[CODE]`

Then: requires ≥3 values; `change_pct = (newest − oldest)/oldest × 100`; `≥ +2.0%` → CE, `≤ −2.0%` → PE, else `None`.

`MIN_MOVE_PCT` was raised from 1.0 to **2.0** with the recorded reason: *"a ≤1% drift over ~6 sessions is noise, not a trend, and would force a near-random side on a signal with no directional edge."* `[CODE]`

**Priority 3 — No lean.** `REQUIRE_TREND = true` → skip the name entirely rather than force a directional bet on a pure-vega setup.

### 10.7 Known measurement fragility

`[DERIVED]`

1. **Slope is computed on an integer index, not elapsed calendar time.** A Friday→Monday step is weighted identically to a Tuesday→Wednesday step, so a weekend-spanning window overstates the per-day rate of climb.
2. **`n_days` may be 3, not 4.** With only 3 points, a single outlier IV reading dominates the fit.
3. **Daily-IV coverage is incomplete.** A prior backtest found daily IV present for roughly **119 of 211** symbols. `[OPERATOR]` Symbols missing daily rows are invisible to the leaderboard entirely — they cannot appear, and their absence is silent.
4. **Bounds filter `atm_iv BETWEEN 1 AND 200`** silently drops rows outside that band rather than flagging them.
5. **The IVP gate is the true binding constraint, at 20 not 30** (§4.7).

---

## 11. Option Selection

### 11.1 Strike selection by strategy

| Strategy | Rule | Constant | Effective strike |
|---|---|---|---|
| S1 / S2 | ATM + N strike-gaps in the trade direction | `STRIKE["intraday_otm_offset"]` = 1 | 1 strike OTM |
| S1 / S2 (swing, unused) | `STRIKE["swing_otm_offset"]` = 0 | | ATM |
| S3 | ATM + N | `BB_STRIKE["otm_offset"]` = **0** | **ATM** — "tighter to the level for break & bounce" |
| S4 | ATM + N | `STRIKE_OTM_OFFSET` = **0** | **ATM** |
| S5 | Every strike in the chain is scored; selection is by score subject to delta and expected-move constraints | — | Variable |
| S6 | By delta band 0.18–0.40 and moneyness ≤ 2.5% | — | Near-ATM OTM |
| S7 strangle | Nearest \|delta − 0.175\| among liquid strikes | `STRANGLE_DELTA_TARGET` = 0.175 | ~17.5-delta OTM, both sides |
| S7 straddle | ATM strike from `extract_atm_reference_ivs` | — | ATM, Δ≈0.5 |
| S8 | ATM strike from the latest intraday `iv_history` snapshot | — | ATM |
| **Hedge leg** | Primary strike ± N strikes further OTM | `HEDGE_STRIKES_OTM` = **3** | 3 strikes OTM of primary |

### 11.2 ATM determination

Two independent implementations exist. `[CODE]`

**S4 (`select_atm_option`):** infers the strike interval as the **modal** gap among the first 5 consecutive strike differences, then `atm = round(spot / gap) * gap`, then picks the chain strike closest to `atm + offset × gap` (CE) or `atm − offset × gap` (PE). Falls back to `gap = 50.0` if no gaps can be computed.

**Hedge (`find_hedge_leg`):** uses `strike_interval = strikes[1] - strikes[0]` — the **first** gap, not the modal one. Validates that the primary strike is within `0.6 × strike_interval` of the nearest chain strike, else aborts with a reason.

> **⚠ DIVERGENCE — two strike-interval algorithms**
> **As-built:** S4 uses the modal gap over five samples; the hedge module uses the single first gap.
> **Intent:** Consistent chain geometry handling.
> **Impact:** On a chain with irregular strike spacing near the low end (common where a chain includes both ₹2.50 and ₹5.00 increments), the two disagree. The hedge module's derived `strike_interval` also feeds its `0.6 ×` primary-strike-match tolerance and its `0.5 ×` hedge-collapse check, so an incorrect interval produces a spurious rejection. This is the mechanism behind the **23 recorded `"hedge strike collapsed onto primary (chain too short past primary)"` failures**. `[DATA]`

### 11.3 Expiry selection

| Strategy | Rule | Fallback |
|---|---|---|
| S4 | First expiry with ≥`MIN_DTE`=4 **trading** days | None — skip the name |
| S5 | **Nearest expiry only**, must be ≥`MIN_DTE_DAYS`=5 **calendar** days | None |
| S6 | Lowest DTE within 7–35 | Nearest expiry with DTE ≥ 7; else `expiries[0]` |
| S7 swing | Lowest DTE within 7–15 | Nearest with DTE ≥ 7; else `expiries[0]` |
| S7 0DTE | DTE == 0 exactly | **None by design** — no 0DTE trade for that name |
| S8 | From scrip master, DTE ≥ 1 | — |
| Hedge | **Same expiry as the primary** — inherited via `signal_template` | n/a |

Weekly vs monthly is not explicitly selected anywhere; the DTE band implicitly determines it. `[DERIVED]`

### 11.4 Premium constraints

| Constraint | Value | Scope |
|---|---|---|
| Global minimum premium | ₹5.00 | `INTRADAY["min_premium"]` |
| S3 override | ₹0.50 | Large-lot cheap options |
| Hedge minimum credit | ₹0.50 | `HEDGE_MIN_CREDIT` |
| Hedge maximum credit ratio | 60% of primary premium | `HEDGE_MAX_CREDIT_RATIO` |

### 11.5 Greeks considerations

| Greek | Treatment |
|---|---|
| **Delta** | Explicit, gated, and scored — see §4.12. The primary strike-selection variable for S6 and S7 |
| **Vega** | Passed into `score_option()` as a parameter but **carries zero weight in every scoring branch**. The vega *thesis* (IV expansion) is expressed via IV slope and IV rank, never via the vega number itself |
| **Theta** | Never computed or read. Managed **structurally** — via DTE floors (§6.4) and forced same-day square-off. `iv_analytics.intraday_decay_curve()` identifies the midday IV lull as a window to avoid, but this is dashboard advice with no code enforcement |
| **Gamma** | Never computed or read. Acknowledged only qualitatively, in the 0DTE parameter set ("0DTE gamma moves far faster than a 7-15 DTE short") |
| **Rho** | Not used |

> **⚠ DIVERGENCE — vega**
> **As-built:** `score_option(self, current_iv, weighted_hv, delta, vega, oi, volume, ...)` accepts `vega` and never references it in any weighted term.
> **Intent:** The parameter's presence implies it was meant to contribute.
> **Impact:** Dead parameter. On strategies whose entire thesis is vega exposure (S4, S5-volatility), position vega is neither measured nor controlled.

### 11.6 IV considerations at selection

| Strategy | IV rule at strike level |
|---|---|
| S5 | Strike IV must be within **10%** of the neighbouring-strike mean (`is_iv_stable`) — rejects single-strike IV prints |
| S5 | `skew_discount` (strike IV vs chain reference) carries the **heaviest weight, 0.40** |
| S6 | ATM IV ≤ 45.0 absolute; IV rank ≤ 65 |
| S7 | IV percentile ≥ 65 drives both candidate selection and structure choice |

---

## 12. Position Sizing

### 12.1 The operative model

**One lot per signal, flat, with no capital-based scaling.** `[OPERATOR]` + `[CODE]`

Operator's ruling, verbatim: *"₹200k is notional; 1-lot is the truth."* The `CAPITAL` constants and `max_risk_pct` sizing formulas are vestigial — nothing in the paper path sizes off them.

Confirmed in code: `paper_trader` books `qty_frac = 1.0` of a single lot; `format_signal_alert` prints `"Qty {lot} (1 lot)"`; all P&L is `realized_points × lot_size − costs`. `[CODE]`

> **⚠ DIVERGENCE — position sizing**
> **As-built:** Every trade is exactly 1 lot. The only size-related control is a **rejection** gate (`max_risk_rupees`), not a sizing calculation.
> **Intent:** `CAPITAL = 200_000` and `RISK_CONFIG["max_risk_pct"] = 0.02` appear in four separate config modules, implying `lots = floor(max_risk / (premium × sl_pct × lot_size))`. `momentum_strategy` contains that formula. `engine/config.py` defines `GRADE_SIZE_MULT` (A+ 1.0, A 1.0, B 0.5) and `SIZE_MULT` (GREEN 1.0, AMBER 0.5, RED 0.0), neither of which reaches the paper path.
> **Impact:** Substantial and measurable. Because every trade is 1 lot, rupee risk varies with `lot_size` across symbols by more than an order of magnitude. A 6,950-lot NHPC option and a 250-lot option are the same "1 lot" to the sizer. The ₹1,500 risk cap is the only thing preventing extreme dispersion — and it operates by **discarding** signals rather than resizing them, which biases the traded population toward small-lot names. This is a systematic selection bias on the very measurements the platform exists to produce.

### 12.2 Per-trade risk cap

`[CODE]` — the single most consequential risk parameter:

```python
INTRADAY["max_risk_rupees"] = 1500.0
```

Computation (`paper_trader._risk_rupees`), direction-aware:
```python
diff = (sl - entry) if direction == "short" else (entry - sl)
risk = max(diff, 0.0) * lot_size
```

- Enforcement point: `paper_trader.book_signal()` and `process_signals()`.
- Override precedence: settings-DB flag `MAX_RISK_RUPEES` > `discount_config` default.
- `0` or `None` disables the cap.
- A signal may opt out with `skip_risk_cap=True` — used by hedge legs and S8.
- **Applies to every strategy**, not just S5.

### 12.3 Declared-but-inactive sizing parameters

| Parameter | Value | Module | Status |
|---|---|---|---|
| `CAPITAL` | 200,000 | momentum, break_bounce, directional_iv, iv_seller | Inactive |
| `RISK_CONFIG["max_risk_pct"]` | 0.02 | S1/S2, S6 | Inactive in paper path |
| `BB_RISK["max_risk_pct"]` | 0.02 | S3 | Inactive in paper path |
| `GRADE_SIZE_MULT` | A+ 1.0 / A 1.0 / B 0.5 | engine | Does not reach paper path |
| `SIZE_MULT` | GREEN 1.0 / AMBER 0.5 / RED 0.0 | engine | Does not reach paper path |
| `MAX_RISK_RUPEES_PER_LEG` (S7) | 3,000 | iv_seller | **Never consulted** — S7 signals don't set `skip_risk_cap`, so the global ₹1,500 applies. The config comment acknowledges this |

### 12.4 Trade-count limits

`[CODE]` — **`0` means unlimited**, and most caps are currently set to 0:

| Cap | Value | Scope |
|---|---|---|
| `INTRADAY["max_signals_per_day"]` | **0** (unlimited) | S5 shared daily cap |
| `INTRADAY["max_per_symbol_per_day"]` | **1** | Global, all strategies, counts across all strikes/sides |
| `BB_RISK["max_trades_per_day"]` | **0** | S3 |
| `BB_RISK["max_open_positions"]` | **0** | S3 |
| `VOL_EXP_MAX_TRADES` | **0** | S4 |
| `MAX_COMBOS_PER_DAY` | **0** | S7 |
| `MAX_COMBOS_PER_SYMBOL_PER_DAY` | **0** | S7 |
| `RISK_CONFIG["max_trades_per_day"]` | 3 | S1/S2 (service off) |
| `RISK_CONFIG["max_open_positions"]` | 2 | S1/S2 (service off) |
| `RISK_CONFIG` (S6) | 2 / 2 | S6 |
| `PAPER_MAX_TRADES` (S8) | 5 | S8 |
| `MAX_CONCURRENT` (S8) | 3 | S8 |

The zeros are labelled *"0 = unlimited (testing phase)"* — a deliberate, dated decision to remove all trade-count limits for data collection. `[CODE]`

### 12.5 Historical record — the per-symbol cap

`[CODE]` + `[DATA]` — `max_per_symbol_per_day` defaulted to 0 (unlimited) until 2026-07-31, letting one underlying consume up to 8 slots/day via different strikes. `count_symbol_today()` already counted across all strikes and sides, so the cap was correctly wired but toothless at 0.

Measured effect:

| Date | Worst symbol | Trades that day |
|---|---|---|
| 2026-07-31 | HINDUNILVR | **12** |
| 2026-07-31 | LT | 10 |
| 2026-07-31 | M&M | 10 |
| 2026-07-30 | HINDUNILVR | 10 |
| 2026-07-30 | ICICIGI | 9 |

After the cap took effect, `scan_log` records **739** `per_symbol_cap` rejections. `[DATA]`

### 12.6 Maximum exposure

Two independent book-level caps exist, both **currently disabled**: `[CODE]`

```python
exposure_config.MODE                    = "off"
exposure_config.MAX_OPEN_POSITIONS      = 0     # 0 = unlimited
exposure_config.MAX_OPEN_PREMIUM_RUPEES = 0.0   # 0 = unlimited
```

`MAX_OPEN_PREMIUM_RUPEES` counts `Σ(entry × lot_size)` across every open leg. Short legs count too — "they carry real margin/assignment risk even though they're a credit, not a debit." It is a raw capital-at-risk proxy, not a net-debit calculation. `[CODE]`

### 12.7 Weekly / monthly loss limits

**None exist.** `[DERIVED]` The only book-level loss control is the daily gate (§15.1). No weekly, monthly, or rolling-drawdown limit is implemented anywhere.

### 12.8 Scaling rules

**None.** No pyramiding, no averaging, no scale-in, no scale-out beyond the (now-inactive) T1 partial-book fraction. `[DERIVED]`

---

## 13. Stop Loss

### 13.1 Initial stop by strategy

| Strategy | Rule | Formula | Value |
|---|---|---|---|
| S1 / S2 | % of premium | `entry × (1 − 0.30)` | −30% |
| S3 | % of premium | `entry × (1 − 0.30)` | −30% |
| S4 | % of premium | `entry × (1 − 0.30)` | −30% |
| **S5** | % of premium | `entry × 0.85` | **−15%** |
| S6 | % of premium | `entry × (1 − 0.30)` | −30% |
| S7 | Credit multiple (short) | `entry × 2.0` | −100% of credit |
| S7 0DTE | Credit multiple (short) | `entry × 1.5` | −50% of credit |
| S8 | % capped by rupees | `min(entry × 0.30, PAPER_MAX_LOSS_RUPEES / lot)` | −30% or ₹700, whichever binds |
| Hedge leg | Credit multiple (short) | `entry × 2.5` | Leg moves 150% against |
| Spread combo | % of net debit | `spread_value ≤ entry_debit × (1 − 0.40)` | −40% of debit |

**S5's −15% is half every other long strategy's −30%.** The config states it is *"Calibrated for same-day, single-leg Volatility Expansion Plays on ~5-DTE options."* `[CODE]`

**S8's rupee ceiling** (`PAPER_MAX_LOSS_RUPEES = 700.0`) was added specifically to close a hole where a large `lot_size` turned a routine 30%-premium stop into a five-figure rupee loss — the config names the incident: *"LAURUSLABS lot=850 on 2026-08-03."* `[CODE]`

### 13.2 Structure stops

**S3 only.** The stop is the candle low (bullish) or high (bearish) of the retest candle — a genuine structure stop — but it is used to define the *underlying* invalidation level. The booked option position still carries the −30% premium stop. `[CODE]`

### 13.3 Trailing stop

`[CODE]` — `trailing_config.py`. **Currently `ENABLED = False`.**

Mechanism when enabled (`_apply_trailing`):

```python
peak = min(peak, last) if short else max(peak, last)      # ratchet the extreme
unrealized_pct = ((entry - peak) if short else (peak - entry)) / entry
if unrealized_pct < ACTIVATION_PCT:  return                # 0.20 → +20% on premium

giveback  = abs(peak - entry) * GIVEBACK_PCT               # 0.15
candidate = max(peak + giveback, target) if short else min(peak - giveback, target)
sl        = min(sl, candidate) if short else max(sl, candidate)   # only ever tightens
```

Properties: activates at +20% on premium; allows 15% giveback of the entry-to-peak gain; **only ever tightens**, never loosens; never crosses the fixed target; fail-soft (any exception leaves the trade untouched).

**Scope limitation:** applies only to trades routed through `apply_tick()` — naked longs from non-hedging strategies and S7's short legs. Two-leg debit-spread combos are evaluated via `apply_combo_tick`'s combined levels and are **never** trailed. `[CODE]`

Persistence: `sl` and `peak_price` are in `_RUNTIME_FIELDS` and written every tick — without this the next `monitor()` re-read would discard the ratchet. `[CODE]`

### 13.4 Time stop

**Universal and mandatory: 15:20 forced square-off** (15:15 for S1/S2/S3/S7). Fires regardless of P&L. This is the dominant exit — 62.4% of all closes. `[DATA]`

### 13.5 Volatility stop

**None.** No ATR-based, IV-based, or realised-vol-based stop exists. `[DERIVED]`

### 13.6 Underlying stop

**None for the option position.** No strategy exits an option because the *underlying* crossed a price level. The closest analogue is the Sonar auto-exit (§14.5), which is structural and currently off. `[DERIVED]`

### 13.7 Hard vs soft

All stops are **hard** in the paper engine — a level touch closes the position on that tick with no discretion, confirmation delay, or re-entry. `[CODE]`

### 13.8 Fill model for stops

`[CODE]` — `apply_tick` docstring, verbatim:

- **Stops fill at `min(level, observed_price)` for longs** (`max` for shorts) — an option premium that **gaps through** the SL fills at the gapped price, not the level. Filling at the level *"systematically overstated paper P&L versus live."*
- **Targets fill AT the level** — conservative; a gap above books the target, not the gapped price.
- **Prices are 5-min sampled LTPs**, so intrabar touches between samples are missed. The source states: *"treat paper results as an estimate, not ground truth."*

Combo exits differ: when a combined level triggers, each leg fills at its **live LTP**, with no per-leg gap-fill logic — the trigger is the combined value. `[CODE]`

---

## 14. Exit Rules

### 14.1 The exit state machine

`[CODE]` — `paper_trader.apply_tick()`. Evaluation order is fixed and matters:

```
1. if status != "open":                                  return []
2. record last_price;  target = trade["t1"]
3. if not square_off:  _apply_trailing(trade, last_price)
4. SL check    → book 100% at gap-aware fill, finalize "SL",     return ["SL"]
5. TARGET check→ book 100% AT the level,     finalize "Target",  return ["TARGET"]
6. if square_off → book remainder at last,   finalize "Time 15:20", return ["TIME"]
```

**Single SL + single target. The first level touched closes the entire position.** There is no runner and no partial book in the live path.

### 14.2 Targets

| Strategy | T1 | T2 | Live target |
|---|---|---|---|
| S1 / S2 | `entry × 1.8` | `entry × 3.0` | T1 |
| S3 | `entry + (sl_amount × 2.5)` | same | T1 |
| S4 | `entry × 1.5` | `entry × 2.0` | T1 |
| **S5** | `entry × 1.25` | `entry × 1.45` | **T1 (+25%)** |
| S6 | `entry × 1.8`, or `entry + \|Δ\| × expected_move` when both available | — | T1 |
| S7 | `entry × 0.35` (book at +65% of credit) | same | T1 |
| S7 0DTE | `entry × 0.40` (+60% of credit) | same | T1 |
| S8 | `entry + 0.75 × the stock's own 1-day expected ATM-premium move` | same | T1 |
| Hedge leg | `entry × 0.15` (85% decay captured), floored at ₹0.05 | same | T1 |
| Spread combo | `entry_debit + max_profit_potential × 0.55` | — | combined |

S8's target is the only one scaled to the individual stock's own implied move rather than a flat multiplier — the config records that it *"replaces the old flat entry × 1.6 multiplier so different stocks get different targets instead of one number for everyone."* `[CODE]`

### 14.3 Partial exits

> **⚠ DIVERGENCE — partial exits / runner**
> **As-built:** `apply_tick()` books **100%** at the first level touched. `t2` and `runner_stop` are persisted but never read live. The docstring is explicit: *"there is no runner to leak the gain."*
> **Intent:** Rich partial-exit machinery is configured throughout: `TRADE_PLAN["t1_book_fraction"] = 0.70` (book 70% at T1, trail 30%), `TRADE_PLAN["runner_stop_to_breakeven"] = True`, S1/S2's two-target 1.8×/3.0× split, S4's `T1_BOOK_FRACTION = 0.5`.
> **Impact:** Every `t1_book_fraction` and `t2` value in every config is inert. The change is documented as a deliberate fix for *"target hit but position stayed open and profit was given back at square-off."* Legacy exit reasons `Target full`, `T1`, `T2`, `Runner BE` survive on pre-change rows and are handled by `_why()`.

### 14.4 Combined spread exit

`[CODE]` — `apply_combo_tick`. Applies when two legs share a `combo_id`, one `long` + one `short`, **both open**.

```python
entry_debit          = long_entry - short_entry          # net cost paid = max loss
strike_width         = |long_strike - short_strike|
max_profit_potential = max(strike_width - entry_debit, 0)
spread_value(t)      = long_price(t) - short_price(t)

SL     when spread_value <= entry_debit * (1 - 0.40)
TARGET when spread_value >= entry_debit + max_profit_potential * 0.55
```

Both legs close together at their own live LTPs. Returns `None` (no action) if `entry_debit <= 0` — not a real debit spread.

**Design rationale, verbatim:** *"each leg's own individual SL/T1 describes that leg in isolation and is wrong for a hedged position: e.g. the short leg's own target fires on pure theta decay regardless of what the long leg is doing, closing half the spread while the other half rides alone."* `[CODE]`

**⚠ This logic has never executed.** See §20.2 — zero `SPREAD_SL` or `SPREAD_TARGET` exits exist across 573 trades. `[DATA]`

### 14.5 Risk-driven auto-exits

Evaluated by `OrderManager.track()` after the ordinary state machine, in this order: `[CODE]`

**1. Daily-loss flatten** — force-close every open position once, the first tick the floor is breached. Requires `MODE == "hard"` **and** `FLATTEN_ON_BREACH == True`. Currently `MODE = "off"`, `FLATTEN_ON_BREACH = False`.

**2. OI-contradiction auto-exit** — `auto_exit_config.MODE = "hard"`. **This is the only auto-exit currently active.**

```python
def oi_contradicts(side, bias, strength, oi_chg_pct, pnl_pct, ...):
    side = "CE" if side in ("CE","CALL") else "PE"
    if bias not in ("CE","PE"):                    return False
    if bias == side:                               return False   # OI agrees, hold
    if require_strong and strength != "strong":    return False
    if abs(oi_chg_pct) < min_oi_chg_pct:           return False
    if pnl_pct is not None and pnl_pct > max_profit_pct: return False  # let winners run
    return True
```

Parameters: `MIN_OI_CHG_PCT = 1.0`, `REQUIRE_STRONG = False`, `MAX_PROFIT_PCT = 10`. Reads `oi_buildup_history` only — zero broker calls. Same-day rows only.

The 1.0% floor (rather than 50) is a recorded operator decision: *"the POSITION RISK warning fires on ANY opposite-side bias, and the operator decision (2026-07-09) is to ACT on those same reads — e.g. APOLLOHOSP CE vs SHORT_BUILDUP OI +1.6%, JUBLFOOD PE vs SHORT_COVERING OI −1.0%. The 1% floor only mutes pure zero-drift noise."* `REQUIRE_STRONG=False` for the same reason. `[CODE]`

**3. Sonar-reversal auto-exit** — `sonar_exit_config.MODE = "off"`.
```python
bearish = {"BREAKDOWN","REVERSAL_DOWN"};  bullish = {"BREAKOUT_UP","REVERSAL_UP"}
contradicts = (side=="CE" and sig in bearish) or (side=="PE" and sig in bullish)
```
Skips positions already up more than `MAX_PROFIT_PCT` = 20%.

**Combo safety:** both auto-exits route through `_close_trade_and_partner()`, which closes a combo's partner leg too, so a risk exit judging one leg's directional thesis cannot split a hedge apart. `[CODE]`

### 14.6 Indicator, VWAP and expiry exits

- **VWAP exits:** none. VWAP is entry-only.
- **Indicator exits:** only the two auto-exits above (OI buildup, Sonar).
- **Expiry exits:** none needed — every position is closed same-day.
- **Manual exits:** no code path. The dashboard is read-only for positions.

### 14.7 Maximum holding time

One trading session. Absolute ceiling ≈ 5h50m (09:30 entry → 15:20 square-off). `[DERIVED]`

---

## 15. Risk Management

### 15.1 Daily loss limit

`[CODE]` — `daily_loss_config.py`. **Currently `MODE = "off"`.**

| Parameter | Value | Meaning |
|---|---|---|
| `MODE` | `"off"` | off / soft (log only) / hard (block new entries) |
| `LIMIT_RUPEES` | 5,000 | New entries stop when day P&L ≤ −₹5,000 |
| `INCLUDE_OPEN` | `True` | Includes marked-to-market unrealized P&L of open positions |
| `FLATTEN_ON_BREACH` | `False` | When True + hard, force-closes everything once |

P&L computation (`book_day_pnl_rupees`): closed trades contribute net `realized_rupees`; open trades contribute `(booked_points + (last − entry) × qty_frac) × lot_size` **gross**, since costs only realize on close. `[CODE]`

Checked **first** in `submit_signals()` and `submit_external_signal()`, before any other gate. Fires one Telegram alert per day on engagement. Dedup via `_loss_alerted_date` / `_flattened_date`. `[CODE]`

Per-strategy `daily_loss_limit_pct = 0.03` constants exist in S1/S2, S3 and S6 configs but do **not** reach the shared book. `[DERIVED]`

### 15.2 Circuit breaker

`FLATTEN_ON_BREACH` is the only true circuit breaker. Currently off, and requires `MODE == "hard"` as a precondition, so it is doubly gated. `[CODE]`

### 15.3 Kill switches

| Switch | Current value | Scope |
|---|---|---|
| `discount_config.PAPER_TRADING_ENABLED` | **`True`** | S5 paper hand-off (scanning + alerts were never gated by this) |
| `hedge_config.ENABLED` | `True` | Hedge overlay |
| `vol_expansion_config.MODE` | `"paper"` | S4: off / alert / paper |
| `engine.PAPER_MODE` | env-driven | S8 |
| `AUTO_EXECUTE` env var | never true | Live order placement |
| Settings-DB flags | various | UI override for every gate mode |

> **⚠ DIVERGENCE — S5 kill switch**
> **As-built:** `PAPER_TRADING_ENABLED = True` since 2026-07-30.
> **Intent:** `CLAUDE.md` states *"`discount_config.PAPER_TRADING_ENABLED = False` (hardcoded kill switch since 2026-07-29), so it currently scans/alerts only and never books."*
> **Impact:** The project's own primary documentation is wrong about the largest trade source in the book. Anyone reasoning from `CLAUDE.md` would conclude S5 has booked nothing, when it has booked 356 of 573 rows. The re-enable is justified in the config on the grounds that discount now routes through `submit_with_hedge`, making every buy a capped debit spread — but §20.3 shows only **41%** of primaries actually receive a hedge leg.

### 15.4 Correlation and concentration limits

`[CODE]` — `order_manager.py`:

```python
PORTFOLIO_MAX_SAME_DIRECTION = 0      # 0 = unlimited
PORTFOLIO_MAX_PER_SECTOR     = 0      # 0 = unlimited
PORTFOLIO_GATE_MODE          = "hard"
```

Counts open positions plus already-accepted candidates in the same batch. Sector mapping from `data/sector_mapping.db` via `breadth.load_sector_map()`; unmapped symbols are direction-capped only. Short legs are excluded from the direction cap as risk-reducing. `[CODE]`

> **⚠ DIVERGENCE — concentration gate is armed but empty**
> **As-built:** `PORTFOLIO_GATE_MODE = "hard"` (will block), but **both caps are 0**, which the code reads as unlimited. The gate executes, loads the sector map, counts everything, and blocks nothing.
> **Intent:** The gate exists because *"Dedup alone allows 5 same-sector same-direction CEs — one correlated bet at 5× intended risk."*
> **Impact:** The stated correlated-risk problem is entirely unmitigated. Identical for `exposure_config` (mode off, both caps 0). The 12 HINDUNILVR positions on 2026-07-31 are exactly the failure mode described. `[DATA]`

### 15.5 Maximum drawdown

**No drawdown limit exists.** No rolling-equity monitor, no drawdown-based throttle, no recovery rule. `[DERIVED]` Measured max drawdown to date: **−₹97,507** (§19.2).

### 15.6 Recovery rules

**None.** After a loss day the system starts the next session with identical parameters. No cooldown, no size reduction, no strategy suspension. `[DERIVED]`

### 15.7 Fail-open convention

Every gate fails **open** — an exception passes candidates through unfiltered. This is deliberate but is made loud: `_alert_gate_failure()` fires one Telegram alert per gate per day. `[CODE]`

Rationale, verbatim: *"A gate that crashes fails OPEN — candidates pass unfiltered. That is deliberate, but it must be LOUD: with a broken shared DB every gate silently no-ops while trading continues."* `[CODE]`

### 15.8 Complete gate inventory and live state

`[CODE]` — verified by importing each config module:

| Gate | Config | Mode | Effective? |
|---|---|---|---|
| Daily-loss lockout | `daily_loss_config` | **off** | No |
| Daily-loss flatten | `daily_loss_config` | **off** | No |
| Pre-market quality (5 sub-gates) | `pre_market_gate_config` | env-driven | Partially |
| Breadth | `breadth_config` | **off** (default) | No |
| Composite entry | `entry_gate_config` | **off** | No |
| Concentration | `order_manager` | hard, caps 0 | **No-op** |
| Exposure | `exposure_config` | **off**, caps 0 | No |
| Sonar veto (entry) | `paper_trader.process_signals` | always on | **Yes** |
| OI-contradiction auto-exit | `auto_exit_config` | **hard** | **Yes** |
| Sonar-reversal auto-exit | `sonar_exit_config` | **off** | No |
| Trailing SL | `trailing_config` | **off** | No |
| Hedge overlay | `hedge_config` | **on** | **Yes** (41% success) |
| Per-trade risk cap | `discount_config` | ₹1,500 | **Yes** |
| Per-symbol/day cap | `discount_config` | 1 | **Yes** |
| Min-premium floor | `discount_config` | ₹5 | **Yes** |

**Six of the sixteen controls are active.** Of the four risk controls added on 2026-08-02 (trailing SL, daily-loss flatten, Sonar auto-exit, exposure gate), **all four remain off by default and none has ever executed.** `[DERIVED]` The operator's own note records these as *"all off by default, 51 tests green, NOT committed."*

The pre-market gate applies five sub-gates — IVR cap, IV/HV ratio, OTM% cap, PCR direction, position cap — and is a **buyer** filter. Short legs (`direction == "short"`) and signals carrying `skip_pre_market_gate` bypass it entirely, because Gate 1 rejects high IVR, Gate 2 rejects IV/HV > 1.0 and Gate 3 rejects far-OTM — the exact opposite of what a premium seller wants. `[CODE]`

---

## 16. Trade Management

| Question | Answer | Detail |
|---|---|---|
| **Can positions be added to?** | **No** | No pyramiding, averaging, or scale-in anywhere |
| **Can positions be reduced?** | **No** (live path) | `_book(trade, frac, price)` supports fractional booking and `t1_book_fraction` is stored, but `apply_tick` always passes `1.0`. The capability exists and is unused |
| **Can positions be hedged?** | **Yes, at entry only** | The hedge leg is booked in the same transaction as the primary. A position cannot be hedged later |
| **Can a stop move?** | **Yes, one direction only** | Via `_apply_trailing` — tightens only, never loosens, never crosses the target. Currently disabled |
| **Can a target move?** | **No** | `t1` is set at entry and never mutated |
| **Can a side be flipped?** | **No — explicitly forbidden** | See below |
| **Manual intervention?** | **No code path** | Dashboard is read-only for positions |

### 16.1 The side-flip prohibition

`[CODE]` — a recorded bug fix, preserved verbatim because it defines a hard invariant:

> "NOTE: the old behaviour FLIPPED the side ('force CALL') while keeping the row's entry/sl/t1/t2 — computed from the OTHER option's premium. Flipped trades booked with the wrong price plan and fired phantom SL/T1 events (review §3.2). **Sides are never mutated any more.**"

The Sonar gate is therefore a **veto only**: FLAT → skip; bullish signal + PUT setup → skip; bearish signal + CALL setup → skip; agrees / soft / no data → keep the scanner's original side. `[CODE]`

### 16.2 Combo integrity

Both legs of a `combo_id` pair enter together and exit together, enforced at three points: `apply_combo_tick` (combined levels), `_close_trade_and_partner` (auto-exits), and `monitor(square_off=True)` (both legs close in the same pass). `[CODE]`

### 16.3 Monitoring cadence

`[CODE]` — deliberately decoupled from scanning:

| Activity | Interval | Rationale |
|---|---|---|
| S5 scan (find new) | 15 min | Chain fetches are API-expensive |
| Monitor (re-price + exit) | **5 min** | *"The discount scanner scans every 15 min, but the OrderManager re-prices and exit-manages open trades every 5 min"* |
| S4 monitor | 5 min until 15:20 | |
| Position-risk warnings | Every monitor tick, deduped per `(trade_id, risk_type)` per day | |

---

## 17. Execution

### 17.1 Current state

**Paper only. No live order has ever been placed.** `[OPERATOR]`

`AUTO_EXECUTE` has never been true. The live-order path is complete, tested-by-inspection, and dead. `[OPERATOR]` + `[CODE]`

### 17.2 Brokers

| Role | Broker | Status |
|---|---|---|
| **Market data** | **Upstox** | Live — all chains, candles, expiries, quotes |
| **Order execution** | Dhan | Reserved, never used |
| Data (legacy) | Dhan | Not used. The internal contract still uses Dhan's response *shape* via `UpstoxDhanAdapter` |

Dhan MCP is not subscribed for data. `[OPERATOR]`

### 17.3 Designed live-order sequence

`[CODE]` — `order_manager.place_bracket_order()`, the single deduplicated copy of a sequence S1/S2 and S3 each previously duplicated:

```
1. Resolve option_security_id;  abort if missing → {"status": "no_option_security_id"}
2. qty = lots × lot_size
3. MARKET BUY   (exchange NSE_FNO, product INTRA, price 0)
4. if buy status != "success" → abort {"status": "buy_failed"}
5. SL_M SELL    (product INTRA, trigger_price = sl_price)
6. if SL status != "success":
       emergency MARKET SELL of the full qty
       Telegram alert
       return {"status": "sl_failed_emergency_exit"}
7. return {"status": "ok", buy_order_id, sl_order_id}
```

**Invariant:** the system must never hold an unprotected long option position. If the protective stop cannot be established, the position is liquidated immediately rather than left naked.

### 17.4 Order types

| Type | Live design | Paper simulation |
|---|---|---|
| MARKET | Entry, emergency exit | Assumed fill at bid/ask-derived entry |
| SL_M (stop-loss market) | Protective stop | `apply_tick` gap-aware fill |
| LIMIT | **Not used** | — |
| SL-LIMIT | **Not used** | — |
| IOC | **Not used** | — |
| Product | `INTRA` (intraday margin) | n/a |

### 17.5 Retry logic

| Layer | Behaviour |
|---|---|
| Option-chain API | 3 retries, exponential backoff 4s → 8s → 16s. Triggers: rate-limit, empty chain, empty expiry list, transient network error |
| Order placement | **None.** A failed buy aborts. A failed SL triggers emergency liquidation, not a retry |
| Quote re-pricing | None per tick. `_requote` returning `None` causes the tick to be skipped; the next 5-min tick retries |
| Telegram/Discord | Falls back Telegram → Discord. Discord 429 rate limits observed and tolerated |

### 17.6 Slippage assumptions

`[CODE]` — `_half_spread_from_row` and `_finalize`:

```python
half_spread = (ask - bid) / 2          if ask > 0 and bid > 0 and ask >= bid
            = entry * 0.02 / 2         otherwise   (PAPER_FALLBACK_SPREAD_PCT=0.02)

slippage_points = 2.0 * half_spread    # entry crosses the spread once, exit once
```

Partial exits are approximated as a single cross. Measured impact: **₹48,373** of slippage across 372 closed trades. `[DATA]`

### 17.7 Transaction costs

Full NSE fee schedule via `costs.py`: brokerage, STT, exchange transaction charge, SEBI fee, stamp duty, IPFT, GST. Applied as exactly 2 orders per trade (one buy + one sell — no partial book, no runner). Direction-aware: for a short leg, entry is the SELL and exit the BUY. `[CODE]`

Measured: **₹21,734** across 372 closed trades. `[DATA]`

### 17.8 Latency assumptions

**None modelled.** No latency, queue position, or partial-fill simulation exists. An order is assumed to fill instantly at the modelled price. `[DERIVED]`

### 17.9 Margin estimation

`[CODE]` — `hedge.py`, explicitly labelled approximate:

```python
naked_short_margin ≈ spot × lot_size × NAKED_SPAN_PCT_OF_NOTIONAL(0.15) + premium × lot_size
spread_margin      ≈ max(long_entry - short_entry, 0) × lot_size          # = net debit = max loss
```

The config is unambiguous that this is *"an industry-standard retail rule of thumb... NOT a substitute for the broker's live margin calculator before any real money is ever involved."* Real SPAN needs the exchange's daily risk-parameter files, to which this system has no access. `[CODE]`

---

## 18. Logging

### 18.1 Per-trade record — `paper_trades` schema

`[CODE]` — 42 columns. Every field persisted for every trade:

| Group | Columns |
|---|---|
| Identity | `id`, `date`, `opened_at`, `closed_at`, `combo_id` |
| Instrument | `symbol`, `security_id`, `exchange_segment`, `side`, `strike`, `expiry`, `lot_size` |
| Plan | `entry`, `sl`, `t1`, `t2`, `t1_book_fraction`, `runner_stop` |
| Context at entry | `score`, `iv`, `hv`, `iv_rank`, `dte`, `spot`, `strategy`, `half_spread` |
| Runtime | `status`, `t1_done`, `qty_frac`, `booked_points`, `last_price`, `peak_price`, `direction` |
| Outcome | `exit_reason`, `realized_points`, `realized_pct`, `realized_rupees` |
| Honest economics | `gross_points`, `slippage_points`, `costs_rupees` |
| Attribution | `factors_json` |

### 18.2 `factors_json` — the attribution payload

`[CODE]` — `collect_factor_snapshot()`. Rationale, verbatim: *"without that, even 500 paper trades won't tell you WHICH component carries the edge."*

```json
{
  "score": 95.0, "iv": 24.3, "hv": 28.1, "iv_rank": 31.2,
  "spread_half": 0.42, "expected_move_ratio": 0.88,
  "pcr": 0.93, "trade_type": "volatility",
  "sonar":      {"signal": "...", "trend": "...", "bias": "...", "slope_pct": 0.0},
  "oi_buildup": {"classification": "...", "bias": "...", "strength": "...", "oi_chg_pct": 0.0},
  "composite":  {"score": 0.0, "direction": "CE", "grade": "MODERATE"},
  "breadth_market_pct": 54.2,
  "hedge_attempt": {"booked": false, "reason": "credit ₹4.97 is 61% of primary (max 60%) — spread too narrow"}
}
```

Every sub-lookup is individually try/except-wrapped and fails to `null` — one unavailable scanner cannot block a booking.

The `hedge_attempt` field is written **before** the primary is inserted, deliberately: *"this system's containers don't reliably persist stdout logs, so a silent, systemic hedge-booking failure must be diagnosable from the DB, not require guesswork."* `[CODE]`

### 18.3 Rejection logging — `scan_log`

`[CODE]` — `scan_log.record_decision()` preserves the counterfactual population. Fields: `strategy`, `symbol`, `side`, `strike`, `iv_rank`, `score`, `decision`, `reason`, `extra`.

Measured distribution (49,951 rows): `[DATA]`

| Decision | Count |
|---|---|
| `iv_rank_floor` | 47,232 |
| `score_below_min` | 1,168 |
| `per_symbol_cap` | 739 |
| `min_premium` | 561 |
| `sonar_contradiction` | 143 |
| `risk_cap` | 54 |
| `booked` | 51 |
| `sonar_flat` | 3 |

### 18.4 Fields NOT logged

`[DERIVED]` — significant gaps for a platform whose stated purpose is edge measurement:

| Missing | Consequence |
|---|---|
| **MAE (Maximum Adverse Excursion)** | Cannot answer "how much heat did winners take?" — the primary input to stop-width optimisation |
| **MFE (Maximum Favourable Excursion)** | Cannot answer "how much did we leave on the table?" — the primary input to target optimisation. `peak_price` exists but is only populated when trailing is enabled, which it is not |
| **Greeks at entry** | `delta` is in some signal dicts but has **no column**. Vega, gamma, theta never recorded |
| **OI / volume at entry** | Present in the signal dict, **not persisted** to any column |
| **Bid/ask at entry** | Only `half_spread` survives; raw bid and ask are discarded |
| **Underlying price path** | Only entry `spot`. No exit spot, no intraday path |
| **Per-tick equity curve** | Only entry and exit; no intermediate marks |
| **Screenshots / chart images** | None |
| **Regime label at entry** | `breadth_market_pct` only; no VIX, no regime state |
| **Slippage realised vs modelled** | Modelled only; there is no live fill to compare against |

### 18.5 Log files

`logs/` — one file per service, root-logger `FileHandler` + stream. `[DATA]`

| File | Size | Last write | Note |
|---|---|---|---|
| `directional_iv.log` | 24.1 MB | 2026-08-03 15:25 | Contains **S5 discount output** too — see below |
| `sonar.log` | 1.6 MB | 15:00 | |
| `break_bounce.log` | 400 KB | 15:15 | |
| `oi_buildup.log` | 353 KB | 15:15 | |
| `iv_rank.log` | 267 KB | 15:20 | |
| `vol_expansion.log` | 18.6 KB | 15:25 | Tiny — S4 does almost nothing |
| `momentum.log` | **0 bytes** | **2026-05-12** | Service disabled |
| `scheduler.log` | **0 bytes** | **2026-03-31** | **S5's own log — never written** |

**Cross-contamination:** `directional_iv_runner.py` calls `logging.basicConfig()` with a `FileHandler` on the **root** logger. Since `DirectionalIVScanner` composes `DiscountedPremiumScanner`, all of `discount.py`'s output propagates into `directional_iv.log`. Not misrouting — root-logger capture of an imported module. `[DERIVED]`

**S5 log loss:** `main.py` (the discount service entrypoint) directs its `FileHandler` to `logs/scheduler.log`, which is 0 bytes and last modified 2026-03-31 — despite S5 having booked 356 trades since. **The single largest trade source in the system produces no persistent log.** `[DERIVED]`

### 18.6 Alerts

`[CODE]` — Telegram primary, Discord fallback (`notifications.notify`).

| Alert | Trigger | Content |
|---|---|---|
| `PAPER TRADE TAKEN` | Every booking | Symbol, side, strike, entry time, expiry, DTE, score, spot, IVR, IV/HV, entry, SL, target (+%), lot size, risk ≈, reward ≈, OI, volume, square-off time |
| Fill update | SL / TARGET / TIME / RISK_EXIT / SPREAD_SL / SPREAD_TARGET | Instrument, price, realized %, realized ₹/lot, reason |
| `POSITION RISK` | OI contradiction or Sonar reversal on an open position | Deduped per `(trade_id, risk_type)` per day |
| `DAILY-LOSS LOCKOUT` | Floor breached | Once per day |
| `DAILY-LOSS FLATTEN` | Circuit breaker fires | Once per day |
| `GATE FAILURE (fail-open)` | Any gate raises | Once per gate per day |
| EOD summary | 15:25 | Trade count, W/L/F, hit rate, net ₹, frictions breakdown, per-strategy breakdown, per-trade detail with plain-language `_why()` reason |

---

## 19. Current Performance

### 19.1 Data basis and caveats

`[DATA]` — `data/paper_trades.db`, queried 2026-08-03.

- **Mode:** Paper only. Never live. `[OPERATOR]`
- **Total rows:** 573
- **Closed:** 372 — the statistical basis below
- **Open (orphaned):** 201, all dated 2026-07-31 — **excluded**, per operator ruling, and documented as a defect (§20.1)
- **Sessions:** 3 (2026-07-30, 07-31, 08-03)
- **Basis:** 1 lot per trade, net of modelled slippage and full NSE charges

> **Sample-size warning.** Three sessions and 372 trades is far below any threshold for statistical significance, and the trades are not independent — many share the same underlying, the same session, and the same market regime. Every figure below is descriptive, not inferential. The operator's framing that these are *"research hypotheses only... not yet been statistically validated"* applies in full.

### 19.2 Aggregate — all closed trades

| Metric | Value |
|---|---|
| Trades | 372 |
| Wins / Losses / Flat | 136 / 236 / 0 |
| **Win rate** | **36.6%** |
| **Net P&L** | **−₹92,953** |
| Average winner | +₹884 |
| Average loser | −₹903 |
| **Average RR (win/loss magnitude)** | **0.98 : 1** |
| **Profit factor** | **0.56** |
| **Expectancy per trade** | **−₹250** |
| Average realized % on premium | −2.92% |
| **Max drawdown** | **−₹97,507** |
| Final equity | −₹92,953 |
| **Max consecutive losses** | **12** |
| **Max consecutive wins** | **9** |
| Sharpe (per-trade, no risk-free) | **−0.173** |
| Sortino (per-trade, downside dev) | **−0.183** |

Sharpe and Sortino are computed per-trade (`mean / stdev`), **not annualised** — three sessions is far too short to annualise meaningfully.

### 19.3 Per-strategy

| Strategy | n | Win rate | Net ₹ | Avg win | Avg loss | PF | Expectancy |
|---|---|---|---|---|---|---|---|
| **S5 Discount** (`Volatility Expansion Play`) | 253 | 40.3% | −23,914 | +1,024 | −850 | 0.81 | −95 |
| **H Hedge legs** (Discount) | 103 | 28.2% | −26,948 | +415 | −527 | 0.31 | −262 |
| **S3 Break & Bounce** | 10 | 30.0% | −13,008 | +790 | −2,197 | 0.15 | −1,301 |
| **H Hedge legs** (B&B) | 2 | 50.0% | +337 | +932 | −595 | 1.57 | +168 |
| **S8 Convex** | 4 | 25.0% | −29,420 | +421 | −9,947 | 0.01 | −7,355 |

**S8 Convex is the single largest loss source** — −₹29,420 from just 4 trades, with an average loser of −₹9,947. This is the exact failure mode `PAPER_MAX_LOSS_RUPEES = 700` was introduced to prevent, and the config names the causal incident (LAURUSLABS, lot 850, 2026-08-03). The trades predate or escaped the fix. `[DERIVED]`

### 19.4 Strategies with zero trades — and the binding constraint for each

Per operator instruction, each is recorded with the specific gate responsible. `[DERIVED]` from `[CODE]` + `[DATA]`:

| Strategy | Trades | Binding constraint | Evidence |
|---|---|---|---|
| **S1 Momentum ORB** | 0 | Service disabled: `profiles: [momentum]` in `docker-compose.yml` means it never starts under the normal `docker-compose up`. Marked "discontinued" | `logs/momentum.log` 0 bytes since 2026-05-12 |
| **S2 Momentum VWAP** | 0 | Same service, same gate | Same |
| **S4 Vol-Expansion** | 0 | `buy_zone_leaderboard()` requires IVP verdict `BUY`, i.e. **IVP < 20** (`IVP_BUY_BELOW`), *not* the documented 30 (§4.7). Combined with slope ≥ 0.5, the intersection is near-empty | `vol_expansion.log` shows initialisation, then a single expiry fetch for one security, then nothing. No "VolExp booked" or "liquidity fail" lines — the candidate list itself is empty. The 2026-07-30 liquidity-floor fix (50,000→2,500 OI) did **not** unblock it because liquidity was never the binding constraint |
| **S6 Directional-IV** | 0 | `MIN_SCORE = 65` combined with `IV_FILTER` (ATM IV ≤45, IV rank ≤65, expected-move ratio ≤1.2, moneyness ≤2.5%, delta 0.18–0.40) | `directional_iv.log`: *"Directional IV scanner found no qualifying opportunities"* (2026-08-03 15:05). Reject reasons logged per strike: "trade type mismatch", "unusably wide spread" (63.8%), "missing IV", "low delta" (Δ=0.031) |
| **S7 IV-Seller** | 0 | **The shared ₹1,500 `max_risk_rupees` cap.** A short leg's risk is `(sl − entry) × lot = entry × (2.0 − 1) × lot = entry × lot` — the full notional premium. This exceeds ₹1,500 for essentially every liquid name. Secondarily, the ₹5 `min_premium` floor rejects the cheap OTM legs that *would* fit | `iv_seller.log` 2026-08-03: `MARICO CE: 1-lot risk ₹6060 > ₹1500 (entry ₹5.05, lot 1200)`; `MARICO PE: ₹7440 > ₹1500`; `CROMPTON CE premium ₹2.45 < min ₹5.00`; `NYKAA CE ₹3.35 < ₹5.00`. **S7 reaches `book_signal` successfully and is rejected there every time** — the strategy logic works; the shared risk model does not accommodate it |

The operator was previously unaware of the S7 blocking mechanism. `[OPERATOR]`

### 19.5 Exit-reason distribution

| Exit reason | Count | Share | Net ₹ |
|---|---|---|---|
| **`Time 15:20`** | **232** | **62.4%** | −40,050 |
| `SL` | 78 | 21.0% | −132,388 |
| `Target` | 57 | 15.3% | **+78,966** |
| `OI contradiction` (3 variants) | 5 | 1.3% | +519 |

By strategy:

| Strategy | SL | Target | Time/Other |
|---|---|---|---|
| S5 Discount | 74 (29%) | 57 (23%) | 122 (48%) |
| S5 hedge legs | 0 | 0 | **103 (100%)** |
| S3 Break & Bounce | 2 (20%) | 0 | 8 (80%) |
| S3 hedge legs | 0 | 0 | 2 (100%) |
| S8 Convex | 2 (50%) | 0 | 2 (50%) |

**No hedge leg has ever hit its own SL or target.** All 103 closed hedge legs exited at square-off or via a combo-partner auto-exit. `[DATA]`

### 19.6 Frictions — the dominant finding

`[DATA]`

| Component | Amount |
|---|---|
| Gross P&L (state machine, pre-friction) | **−₹22,845** |
| Modelled slippage (2 × half-spread × lot) | −₹48,373 |
| NSE charges (brokerage/STT/txn/SEBI/stamp/IPFT/GST) | −₹21,734 |
| **Net P&L** | **−₹92,953** |

**Frictions account for ₹70,107 — 75.4% of the total loss.** The raw signal loses money before costs (−₹22,845), but transaction costs and spread crossing are **3.07× larger** than the signal's own deficit.

Per closed trade this is ₹188 of friction on an average absolute P&L of ~₹893 — roughly **21% of the typical trade's gross magnitude consumed by costs**. `[DERIVED]`

### 19.7 Per-session

| Date | Strategy | n | Net ₹ |
|---|---|---|---|
| 2026-07-30 | S5 Discount | 124 | −9,950 |
| 2026-07-30 | S5 hedge | 57 | −15,854 |
| 2026-07-30 | S3 B&B | 2 | −1,045 |
| 2026-07-30 | S3 hedge | 2 | +337 |
| 2026-07-31 | S5 Discount | 90 | −11,508 |
| 2026-07-31 | S5 hedge | 12 | −3,525 |
| 2026-08-03 | S5 Discount | 39 | −2,456 |
| 2026-08-03 | S5 hedge | 34 | −7,568 |
| 2026-08-03 | S3 B&B | 8 | −11,963 |
| 2026-08-03 | S8 Convex | 4 | −29,420 |

**Every strategy lost money in every session except the 2-trade B&B hedge sample.**

### 19.8 The `iv_rank` filter — the one measured positive result

`[DATA]` — recomputed on S5's 253 closed trades:

| Filter | n | Win rate | Net ₹ | Expectancy |
|---|---|---|---|---|
| No filter (baseline) | 253 | 40.3% | −23,914 | −₹95 |
| `iv_rank ≥ 25` | 106 | 46.2% | **+4,912** | +₹46 |
| **`iv_rank ≥ 30`** | **78** | **51.3%** | **+10,510** | **+₹135** |
| `iv_rank ≥ 40` | 45 | 40.0% | −8,722 | −₹194 |

**The relationship is not monotonic.** It improves to 30 and then reverses sharply at 40. The prior analysis recorded in `discount_config.py` claimed monotonicity through ≥30; extending to ≥40 shows it breaks. With n=45 in the ≥40 bucket, this could be noise — but it is equally possible the ≥25 and ≥30 results are noise. `[DERIVED]`

The gate went live at `MIN_IV_RANK = 25` and is demonstrably enforced: `[DATA]`

| Date | Discount trades | Min iv_rank | Avg iv_rank | Below 25 |
|---|---|---|---|---|
| 2026-07-30 | 124 | 0.0 | 21.6 | 85 |
| 2026-07-31 | 193 | 0.0 | 19.8 | 128 |
| **2026-08-03** | **39** | **25.3** | **38.4** | **0** |

The gate rejected **47,232** candidates in `scan_log` — 94.6% of all logged decisions. `[DATA]`

### 19.9 Metrics not computable

`[DERIVED]` — from the §18.4 logging gaps:

| Requested metric | Status |
|---|---|
| MAE / MFE | **Not computable.** No column exists |
| Annualised Sharpe / Sortino | **Not meaningful.** 3 sessions |
| Rolling drawdown curve | Computable only trade-by-trade, not intraday |
| Per-factor attribution | `factors_json` is populated but no attribution analysis has been run |
| Live-vs-paper slippage divergence | **Not computable.** No live fills exist |

---

## 20. Known Weaknesses

Derived by the specifier from code and data, per operator instruction. Each is stated with its evidence.

### 20.1 Orphaned positions on process restart — **confirmed defect**

`[DERIVED]` from `[CODE]` + `[DATA]`

**Mechanism.** `paper_trader.monitor()` and `run_eod()` both query:
```sql
SELECT * FROM paper_trades WHERE date = ? AND status = 'open'
```
using **today's** date. No code path anywhere revisits a prior date's open positions.

**Evidence.** On 2026-07-31, closes stop at **15:15:11**; the 15:20 square-off never executed. 201 positions remain `status='open'` permanently. `last_price` had updated for 193 of them, proving the monitor was alive until it stopped. Every file in `logs/` begins at **2026-07-31 21:57** — the containers were restarted that evening and that day's logs are lost.

**Impact.**
1. ~35% of all booked trades (201/573) have no recorded outcome and are permanently unmeasurable.
2. The daily-loss gate reads `book.all_trades(today)` — orphans are invisible to it, so a catastrophic prior-day book cannot inform today's risk.
3. Any container restart, deploy, or crash during market hours silently strands the entire open book.

**Latent severity.** Under paper trading the loss is data only. Under live trading this same code path would leave real positions open overnight with no monitoring and no square-off.

### 20.2 The combined-spread exit has never executed — **confirmed defect**

`[DATA]` — **zero** `SPREAD_SL` or `SPREAD_TARGET` exits exist across all 573 trades, despite 145 successfully-paired combos.

Combo-tagged rows exited as: `Time 15:20` ×230, `SL` ×76, `Target` ×57, `OI contradiction` ×5. Every one of those is an `apply_tick` (per-leg) outcome, not an `apply_combo_tick` (combined) outcome.

**Two mechanisms, jointly sufficient:** `[DERIVED]`

1. **Unpaired combos.** Only 41% of primaries receive a hedge leg (§20.3). `_combo_pairs()` requires exactly 2 legs — `if len(legs) != 2: continue` — so 59% of `combo_id` rows fall through to per-leg handling by design.
2. **Quote-failure fallthrough (suspected, for the paired 41%).** `monitor()` contains:
   ```python
   lp, sp = prices.get(long_leg["id"]), prices.get(short_leg["id"])
   if lp is None or sp is None:
       continue   # can't evaluate the combined value this tick
   ```
   Hedge legs sit **3 strikes OTM** and are thin. `_requote` returns `None` whenever `quote.get("last")` is `None` or `0`. A short leg that fails to quote silently drops the pair out of combo evaluation and into independent per-leg management — precisely what the design exists to prevent.

**This second mechanism is inferred, not proven** — it is consistent with every observation but would need instrumentation (logging combo-skip events) to confirm.

**Impact.** The entire combined-exit design — `SPREAD_SL_PCT = 0.40`, `SPREAD_TARGET_CAPTURE_PCT = 0.55`, and the reasoning about legs exiting together — is inert. Hedged positions are managed as two independent trades, which is the exact failure the design document describes as wrong: *"the short leg's own target fires on pure theta decay regardless of what the long leg is doing."*

### 20.3 Hedge coverage is 41%, not 100% — **confirmed defect**

`[DATA]` — 253 closed Discount primaries produced only 103 closed hedge legs. Of all rows carrying a `hedge_attempt` record: **145 booked, ~99 failed.**

Failure reasons, from persisted `factors_json`:

| Reason | Count | Cause |
|---|---|---|
| `"credit ₹X is N% of primary (max 60%) — spread too narrow"` | ~76 distinct instances, ratios 60%–79% | `HEDGE_MAX_CREDIT_RATIO = 0.60` |
| `"hedge strike collapsed onto primary (chain too short past primary)"` | 23 | Insufficient strikes beyond the primary, and/or the strike-interval mis-derivation of §11.2 |

**Impact.** The 2026-07-30 decision to re-enable S5's kill switch was justified on the grounds that *"every discount buy is a capped debit spread, not a naked long, addressing the loss profile that triggered the original kill switch."* **That premise is false for 59% of trades.** The majority of S5's book is naked long premium — exactly the risk profile the kill switch was imposed to eliminate.

The 2→3 strike widening (2026-07-31) was made to reduce the "too narrow" failures. Failures at ratios up to 79% persist after it. `[DATA]`

### 20.4 Frictions exceed the signal deficit by 3× — **confirmed, quantified**

`[DATA]` §19.6. Gross −₹22,845; frictions −₹70,107; net −₹92,953.

The system trades **372 times in 3 sessions on 1-lot positions**. At ₹188 of friction per trade, high-frequency small-size trading is structurally friction-dominated. A strategy would need to overcome ~21% of its own gross magnitude in costs before producing any net edge.

This interacts with §12.1: because sizing is flat 1-lot, cost-per-trade does not amortise over larger positions the way it would under risk-based sizing.

### 20.5 The Discount composite score does not discriminate — **confirmed, quantified**

`[DATA]` — 253 closed S5 trades:

| Metric | Value |
|---|---|
| Score range | 51.07 – 95.00 |
| Mean / median | 93.87 / **95.00** |
| At or above 94.9 | **226 of 253 = 89%** |
| Average score, winners | 94.29 |
| Average score, losers | 93.58 |
| **Separation** | **0.71 points** |

The score saturates at its ceiling for 89% of the traded population and separates winners from losers by less than one point on a 100-point scale. As a ranking or gating instrument it carries almost no information.

`MIN_SCORE = 55` is consequently near-inert for the traded population, though `scan_log` shows 1,168 `score_below_min` rejections — so it does bind on the wider candidate pool. `[DATA]`

This was previously identified by the operator (recorded in `discount_config.py`: *"score is saturated at the 95.0 ceiling for ~86% of candidates and doesn't separate winners from losers (win-trade avg 94.66 vs loss-trade avg 94.70)"*) and **remains unfixed**. This specification's independent recomputation on a larger sample confirms it: 89% and 0.71 points.

### 20.6 The clock, not the thesis, decides most trades

`[DATA]` — 62.4% of all closes are `Time 15:20`. For hedge legs it is 100%.

Neither the stop nor the target is reached within the session for the majority of positions. The trade plan's SL and target levels are therefore descriptive of a minority of outcomes. A −15% stop and +25% target (S5) on a ~5-DTE option, sampled every 5 minutes and force-closed at 15:20, produces a distribution dominated by "premium drifted somewhere and time ran out."

### 20.7 Six of sixteen risk controls are active; the concentration gate is armed but empty

`[DERIVED]` §15.8. `PORTFOLIO_GATE_MODE = "hard"` with both caps at 0 produces a gate that runs, loads the sector map, counts every position, and blocks nothing. The correlated-risk problem it was written to solve (documented in its own docstring) is unmitigated — evidenced by 12 HINDUNILVR positions in one session.

All four risk controls added 2026-08-02 remain off and have never executed.

### 20.8 Selection bias from rejection-based risk control

`[DERIVED]` §12.1. Because the ₹1,500 cap **discards** signals rather than resizing them, the traded population is systematically biased toward small-`lot_size` symbols. For a platform whose purpose is measuring edge, the measurement sample is not representative of the signal population. 54 `risk_cap` rejections are logged, plus every S7 leg ever generated.

### 20.9 The primary documentation is materially wrong

`[DERIVED]` — `CLAUDE.md` states S5's kill switch is `False` and the service "never books." It is `True` and has booked 356 of 573 rows. Anyone onboarding from the project's own documentation would misidentify the system's largest trade source.

### 20.10 Logging gaps preclude the platform's stated purpose

`[DERIVED]` §18.4. MAE, MFE, entry Greeks, entry OI/volume, and raw bid/ask are not persisted. Stop-width and target-distance optimisation — the most immediate use of 372 trades — is not possible from the recorded data. `factors_json` is well-designed and fully populated, but no attribution analysis has yet been run against it.

Compounding this, S5's own log file has been 0 bytes since 2026-03-31 (§18.5).

### 20.11 Silent, undiagnosed strategy starvation

`[DERIVED]` §19.4. Four strategies have run daily for months and produced nothing. There is no monitoring that detects "this service has scanned N times and booked zero," so starvation is invisible until someone queries the database. The S4 case is instructive: a liquidity floor was diagnosed and fixed on 2026-07-30, and the strategy remained at zero because the actual binding constraint (IVP < 20) was elsewhere.

### 20.12 Uncertain assumptions and never-validated components

| Component | Validation status |
|---|---|
| **S8 Convex conviction v2.1** | **Only validated component.** Replay over 38k labelled decisions, train 07-03→07-16, validate 07-17→07-23; monotone ladder on train, top-grade positive on validation. Note it is nonetheless the worst live performer (−₹29,420 / 4 trades) |
| S5 score weights (0.30/0.40/0.10/0.10/0.20) | Never validated. §20.5 shows the composite does not discriminate |
| S6 score weights (1.3/1.1/1.0/0.9/0.9/0.7/0.5) | Never validated. Zero trades — no data could exist |
| IV slope threshold 0.5, window 4 | Never validated. Zero trades |
| `buy_score = slope × (1 − IVP/100)` | Never validated |
| Sonar veto | Never validated. 143 `sonar_contradiction` + 3 `sonar_flat` rejections; no counterfactual measurement of what those trades would have done |
| OI-contradiction auto-exit (**active, hard mode**) | Never validated. 5 executions, +₹519 net. Acts on drift as small as 1% |
| Hedge parameters (3 strikes, 60% ratio, 2.5×/0.15× levels) | Never validated. §20.3 shows 59% never book |
| Spread SL 40% / target capture 55% | **Cannot** be validated — never executed (§20.2) |
| `MIN_IV_RANK = 25` | Partially validated in-sample only, on the same 214 trades that motivated it. §19.8 shows non-monotonicity at ≥40 |
| Trailing SL (20% activation / 15% giveback) | Never validated. Never enabled |
| `MAX_RISK_RUPEES = 1500` | Never validated. Blocks S7 entirely |
| Slippage model (2 × half-spread) | Never validated against live fills |
| Margin estimates (15% of notional) | Explicitly labelled an estimate, never checked against a broker calculator |

---

## 21. Assumptions

Derived by the specifier from the code's structure, per operator instruction. Each is a belief the system acts on without statistical proof.

### 21.1 Volatility assumptions

**A1 — Cheap IV mean-reverts upward, profitably, within one session.**
Where: the core thesis of S4, S5, S6. Every buy-side strategy selects on IV being low relative to some baseline and books a same-day trade.
Unproven because: no study relates entry IV rank/percentile to next-session premium change. §19.8's `iv_rank` result is in-sample on the trades that motivated the threshold.
Hidden sub-assumption: a *daily* IV signal has *intraday* predictive power. S4's signal moves over days; its positions live hours.

**A2 — A 4-day IV slope proxies for an unknown catalyst.**
Where: §10. Stated explicitly in source: *"A steep positive slope IS the 'climbing IV into an event' signature."*
Unproven because: no event calendar exists to test the correspondence. The proxy has never been checked against actual event dates.

**A3 — Cross-sectional IV skew identifies mispricing, not risk.**
Where: `skew_score`, weight **0.40** — S5's heaviest component. A strike whose IV is below its chain neighbours is scored as *cheap*.
Unproven because: the alternative reading — that the market prices that strike lower because it is genuinely less likely to be reached — is never tested. The 10% `is_iv_stable` band filters *outliers*, not *correctness*.

**A4 — IV below HV means the option is underpriced.**
Where: `hv_score` in S5 (weight 0.30) and S6.
Unproven because: IV is forward-looking, HV backward-looking. The gap is a legitimate forecast of *lower future* volatility as often as it is a mispricing.

**A5 — Rich IV mean-reverts downward before the underlying moves against a short.**
Where: S7's entire premise. `SELL_ZONE_MIN = 65`, straddle at ≥85.
Unproven because: S7 has never booked a trade.

### 21.2 Directional assumptions

**A6 — Breakouts continue.**
Where: S1 (ORB), S3 (prior-day H/L break). Both bet on continuation past a reference level.
Unproven because: momentum is disabled; B&B has 10 closed trades at 30% win rate.
**Counter-evidence within the system:** the Convex engine set `W_GAP = 0.0` after replay showed *"intraday gaps fade"*, and noted gap-as-**fade** showed top-grade alpha. That is direct evidence against continuation in an adjacent context.

**A7 — Volume confirms.**
Where: S1 requires ≥1.5× the prior 5-candle average; S2 requires ≥1.3×.
Unproven because: the thresholds have no derivation and momentum has never traded. 1.5 and 1.3 differ with no stated rationale.

**A8 — Aggregate option OI buildup reveals directional intent, actionably, at 1% drift.**
Where: the OI-contradiction auto-exit — **currently `hard`, the only active auto-exit.**
Unproven because: 5 executions total. `MIN_OI_CHG_PCT` was set to 1.0 explicitly to act on the same weak reads that generate warnings, and `REQUIRE_STRONG = False` admits SHORT_COVERING and LONG_UNWINDING. This is the most aggressive live assumption in the system.

**A9 — Laplace-filtered support/resistance carries directional information.**
Where: the Sonar veto, applied to every S5 candidate.
Unproven because: 146 vetoed candidates were never counterfactually evaluated.

**A10 — A multi-factor composite produces a valid CE/PE lean for a direction-agnostic signal.**
Where: S4 takes its entire side selection from `composite_history`.
Unproven because: S4 has never traded. Note the composite fuses OI-buildup, smart-money, delivery-surge and gap — and the Convex engine independently zeroed both institutional-flow and gap weights as anti-predictive.

**A11 — A ≥2% spot drift over 6 days predicts today's direction.**
Where: S4's momentum fallback.
Unproven, and internally acknowledged: the config calls it *"Weak basis for a vega signal; kept only as a fallback."* The threshold was doubled from 1.0% on the reasoning that ≤1% is noise — an admission that the original was arbitrary.

### 21.3 Structural assumptions

**A12 — A 3-strike-wide short leg caps risk without materially capping reward.**
Where: `HEDGE_STRIKES_OTM = 3`.
Unproven, and contradicted: hedge legs show a 0.31 profit factor and −₹26,948 net; 59% never book at all.

**A13 — 5-minute LTP sampling adequately approximates continuous price.**
Where: the entire paper engine.
Acknowledged in source: *"intrabar touches between samples are still missed — treat paper results as an estimate, not ground truth."* Direction of bias is unknown and unmeasured: missed intrabar SL touches flatter results; missed intrabar target touches understate them.

**A14 — 2 × half-spread is the right slippage estimate.**
Where: `_finalize`. This is ₹48,373 — over half the total loss.
Unproven because: no live fills exist. Also assumes the quoted spread at *scan* time is the spread at *fill* time.

**A15 — Broker-supplied Greeks are accurate.**
Where: every delta gate and delta score.
Unproven because: delta is taken from the chain payload and never recomputed or sanity-checked.

**A16 — One lot is the correct size for every signal.**
Where: §12.1.
Unproven, and known to introduce >10× rupee-risk dispersion, mitigated only by a rejection cap that biases the sample (§20.8).

**A17 — Same-day exit is the right horizon.**
Where: universal 15:20 square-off.
Unproven, and §20.6 shows it binds on 62.4% of trades — the horizon, not the thesis, determines most outcomes. BTST/swing was investigated and rejected. `[OPERATOR]`

**A18 — Fail-open is safer than fail-closed.**
Where: every gate.
A deliberate architectural choice, not an oversight, and made loud via alerts. But it means a broken shared DB results in *unfiltered trading*, not *no trading*.

**A19 — Zero trade-count limits are acceptable during the testing phase.**
Where: six caps set to 0 = unlimited.
The stated rationale is data collection. The realised cost is 12 positions in one underlying in one session and 303 trades in a single day.

**A20 — The scrip-master fallback of `lot_size = 1` is safe.**
Where: `ScripMasterLotSizer`. A lot of 1 makes rupee risk ≈ premium, which trivially passes the ₹1,500 cap. A lot-sizing outage would therefore *increase* booking volume while making every P&L figure meaningless. The fee model was explicitly fixed to still charge costs in this state, but the sizing hazard remains.

---

## 22. Questions for Future Research

### 22.1 Immediate defect remediation (blocking further measurement)

| # | Item | Rationale |
|---|---|---|
| R1 | **Recover or write off the 201 orphaned trades**, and make `monitor`/`run_eod` reconcile *any* open position regardless of `date` | §20.1. 35% of the book is unmeasurable; the same bug is catastrophic under live trading |
| R2 | **Instrument `apply_combo_tick` skip events** — log every tick where a paired combo is skipped due to a `None` quote | §20.2. Confirms or refutes the suspected mechanism; cheap to add |
| R3 | **Determine why 59% of hedge legs fail** and either fix or stop claiming trades are hedged | §20.3. The kill-switch re-enable rests on a false premise |
| R4 | **Fix S7's risk-cap incompatibility** — a short leg's risk is not `entry × lot` | §19.4. An entire strategy is blocked by a model that does not apply to it |
| R5 | **Route S5's logs to a file that is actually written** | §18.5. The largest trade source is unobservable |
| R6 | **Reconcile `CLAUDE.md` with the code** | §20.9 |
| R7 | **Populate `NSE_HOLIDAYS`** | §6.5. Every trading-day DTE is currently wrong |
| R8 | **Add a zero-trade alarm** — alert when a service scans N times and books nothing | §20.11 |

### 22.2 Highest-value research questions

**Q1 — Is there any edge at all before frictions?**
Gross P&L is −₹22,845 over 372 trades. Before optimising anything, establish whether any strategy has positive *gross* expectancy. If not, cost reduction is irrelevant.

**Q2 — What is the friction-minimal trade frequency?**
Frictions are 75% of the loss (§19.6) and 372 trades in 3 sessions on 1-lot positions is structurally friction-dominated. Test: fewer, larger, higher-conviction trades against the same signals.

**Q3 — Does the `iv_rank` filter survive out-of-sample?**
§19.8's ≥30 result (+₹10,510, 51.3%) is in-sample on the trades that motivated it, and breaks at ≥40. Requires a clean forward test with the threshold frozen.

**Q4 — Replace the Discount composite score, or drop it?**
89% saturation and 0.71 points of separation (§20.5). Options: recalibrate the components; replace the composite with `iv_rank` alone; or apply the Convex engine's replay methodology — the only validation method that has produced a monotone ladder in this system.

**Q5 — Why do 62% of trades die on the clock?**
Test: (a) different session horizons; (b) tighter targets reachable within a session; (c) entry timing relative to `intraday_decay_curve`'s midday lull, which is computed but never used for gating.

**Q6 — Is the hedge overlay net-positive?**
Hedge legs: PF 0.31, −₹26,948 (§19.3). Compare, on matched trades, primary-only P&L against primary+hedge P&L. The margin benefit is real but unmeasured against a real broker calculator.

**Q7 — Does the Sonar veto add value?**
146 vetoed candidates. Book them in a shadow book and compare.

**Q8 — Is the OI-contradiction auto-exit helping?**
The only *active* auto-exit, running in hard mode on 1% drift, with 5 executions. Run it in soft mode with a shadow comparison before trusting it further.

### 22.3 Unblocking the silent strategies

| # | Item |
|---|---|
| U1 | **S4:** resolve the IVP threshold conflict (20 vs 30, §4.7). Measure candidate counts at each before changing anything |
| U2 | **S4:** decide whether it still has a distinct edge now that S5 also trades expanding-IV names (§6.3) |
| U3 | **S6:** replace the positional-slice universe with a ranked one (§2.4); measure how many candidates each `IV_FILTER` component eliminates |
| U4 | **S7:** after R4, run in alert mode first to size the opportunity before booking |
| U5 | **S1/S2:** decide formally whether momentum is discontinued or dormant. Currently ambiguous |
| U6 | **S8:** investigate why the only replay-validated component is the worst live performer. Confirm `PAPER_MAX_LOSS_RUPEES` is actually applied |

### 22.4 Measurement infrastructure

| # | Item |
|---|---|
| M1 | **Persist MAE and MFE per trade.** Without them stop-width and target-distance cannot be optimised (§18.4). Highest-leverage single addition |
| M2 | **Persist entry Greeks, OI, volume, and raw bid/ask.** All are in the signal dict and discarded at insert |
| M3 | **Run the attribution analysis `factors_json` was built for.** It has been populated for 573 trades and never analysed. This is the platform's stated purpose |
| M4 | **Extend the backtest engine.** Phase 1 (Momentum, BS-premium reconstruction) exists; a daily-IV coverage gap of ~119/211 symbols was found and is unresolved. Note `fcntl` usage fails on Windows dev |
| M5 | **Build an out-of-sample harness.** The operator's stated bar — *"positive expectancy, robustness across market regimes, acceptable risk-adjusted performance"* — cannot currently be evaluated for any strategy |
| M6 | **Log regime state at entry** (VIX, breadth, engine GREEN/AMBER/RED) so §6.1's regime hypothesis becomes testable |

### 22.5 Open hypotheses recorded but untested

| # | Hypothesis | Source |
|---|---|---|
| H1 | Gap-as-**fade** carries top-grade alpha (opposite of the current continuation reading) | Convex v2.1 replay; explicitly deferred as *"research candidate for v2.2, not shipped — one change at a time"* |
| H2 | Institutional flow (bulk/block) has a BTST horizon and is anti-predictive for 60-min bets | Convex replay: edge −0.52 present vs −0.10 absent. Weight zeroed but votes still journalled for possible re-inclusion |
| H3 | Premium value (cheap IV) is direction-neutral and inflates a directional score | Convex replay. Weight zeroed, hard EXPENSIVE gate retained |
| H4 | A narrower (2-strike) hedge would give a genuinely different risk/reward shape worth comparing | Original S5 design intent, reverted 2026-07-31 due to booking failures. Untested |
| H5 | `MIN_EXPECTED_MOVE_PCT` should rise from 0.8 toward 1.2 | Config comment: *"raise toward 1.2 when the journal shows theta losses"* — the journal now shows losses; the check has not been run |
| H6 | The midday IV lull is a bad entry window | `intraday_decay_curve()` computes it and advises avoiding it; nothing enforces it |

---

## Appendix A — Complete Parameter Reference

Every tunable constant, with module, default, and environment override.

### A.1 Global / shared

| Parameter | Module | Default | Env override |
|---|---|---|---|
| `INTRADAY["scan_interval_min"]` | discount_config | 15 | — |
| `INTRADAY["monitor_interval_min"]` | discount_config | 5 | — |
| `INTRADAY["session_start"]` | discount_config | 09:30 | — |
| `INTRADAY["no_entry_after"]` | discount_config | 15:00 | — |
| `INTRADAY["square_off"]` | discount_config | 15:20 | — |
| `INTRADAY["monitor_until"]` | discount_config | 15:20 | — |
| `INTRADAY["eod_summary_at"]` | discount_config | 15:25 | — |
| `INTRADAY["max_signals_per_day"]` | discount_config | 0 (unlimited) | `PAPER_MAX_SIGNALS` |
| `INTRADAY["max_per_symbol_per_day"]` | discount_config | 1 | `PAPER_MAX_PER_SYMBOL` |
| `INTRADAY["min_premium"]` | discount_config | 5.0 | — |
| `INTRADAY["max_risk_rupees"]` | discount_config | 1500.0 | settings-DB `MAX_RISK_RUPEES` |
| `PAPER_FALLBACK_SPREAD_PCT` | paper_trader | 0.02 | `PAPER_FALLBACK_SPREAD_PCT` |
| `PORTFOLIO_MAX_SAME_DIRECTION` | order_manager | 0 | `PORTFOLIO_MAX_SAME_DIRECTION` |
| `PORTFOLIO_MAX_PER_SECTOR` | order_manager | 0 | `PORTFOLIO_MAX_PER_SECTOR` |
| `PORTFOLIO_GATE_MODE` | order_manager | hard | `PORTFOLIO_GATE_MODE` |

### A.2 Risk gates

| Parameter | Module | Default | Env override |
|---|---|---|---|
| `MODE` | daily_loss_config | off | `DAILY_LOSS_GATE_MODE` |
| `LIMIT_RUPEES` | daily_loss_config | 5000.0 | `DAILY_LOSS_LIMIT_RUPEES` |
| `INCLUDE_OPEN` | daily_loss_config | true | `DAILY_LOSS_INCLUDE_OPEN` |
| `FLATTEN_ON_BREACH` | daily_loss_config | false | `DAILY_LOSS_FLATTEN_ON_BREACH` |
| `MODE` | exposure_config | off | `EXPOSURE_GATE_MODE` |
| `MAX_OPEN_POSITIONS` | exposure_config | 0 | `EXPOSURE_MAX_OPEN_POSITIONS` |
| `MAX_OPEN_PREMIUM_RUPEES` | exposure_config | 0 | `EXPOSURE_MAX_OPEN_PREMIUM_RUPEES` |
| `MODE` | auto_exit_config | **hard** | `AUTO_EXIT_OI_MODE` |
| `MIN_OI_CHG_PCT` | auto_exit_config | 1.0 | `AUTO_EXIT_OI_MIN_OI_CHG_PCT` |
| `REQUIRE_STRONG` | auto_exit_config | false | `AUTO_EXIT_OI_REQUIRE_STRONG` |
| `MAX_PROFIT_PCT` | auto_exit_config | 10 | `AUTO_EXIT_OI_MAX_PROFIT_PCT` |
| `MODE` | sonar_exit_config | off | `SONAR_EXIT_MODE` |
| `MAX_PROFIT_PCT` | sonar_exit_config | 20 | `SONAR_EXIT_MAX_PROFIT_PCT` |
| `ENABLED` | trailing_config | false | `TRAILING_SL_ENABLED` |
| `ACTIVATION_PCT` | trailing_config | 0.20 | `TRAILING_SL_ACTIVATION_PCT` |
| `GIVEBACK_PCT` | trailing_config | 0.15 | `TRAILING_SL_GIVEBACK_PCT` |
| `GATE_MODE` | entry_gate_config | off | `GATE_MODE` |
| `MIN_GATE_SCORE` | entry_gate_config | 45 | `GATE_MIN_SCORE` |
| `ALLOW_IF_NO_COMPOSITE` | entry_gate_config | true | `GATE_ALLOW_IF_MISSING` |
| `GATE_SOURCE` | entry_gate_config | composite | `GATE_SOURCE` |
| `ENGINE_MAX_AGE_MIN` | entry_gate_config | 20 | `GATE_ENGINE_MAX_AGE_MIN` |

### A.3 Hedge

| Parameter | Default | Env override |
|---|---|---|
| `ENABLED` | true | `HEDGE_ENABLED` |
| `HEDGE_STRIKES_OTM` | 3 | `HEDGE_STRIKES_OTM` |
| `HEDGE_MAX_CREDIT_RATIO` | 0.60 | `HEDGE_MAX_CREDIT_RATIO` |
| `HEDGE_MIN_CREDIT` | 0.50 | `HEDGE_MIN_CREDIT` |
| `HEDGE_SL_MULT` | 2.5 | `HEDGE_SL_MULT` |
| `HEDGE_T1_MULT` | 0.15 | `HEDGE_T1_MULT` |
| `SPREAD_SL_PCT` | 0.40 | `HEDGE_SPREAD_SL_PCT` |
| `SPREAD_TARGET_CAPTURE_PCT` | 0.55 | `HEDGE_SPREAD_TARGET_CAPTURE_PCT` |
| `NAKED_SPAN_PCT_OF_NOTIONAL` | 0.15 | `NAKED_SPAN_PCT_OF_NOTIONAL` |
| `DISCOUNT_HEDGE_STRIKES_OTM` | 3 (was 2) | `DISCOUNT_HEDGE_STRIKES_OTM` |

### A.4 Per-strategy (see §7, §9, §11, §13, §14 for full context)

Momentum (S1/S2), Break & Bounce (S3), Vol-Expansion (S4), Discount (S5), Directional-IV (S6), IV-Seller (S7), and Convex (S8) parameters are tabulated in the sections above and in their respective `*_config.py` modules. All are environment-overridable except `momentum_config` and `break_bounce_config` structural dicts, and the two hardcoded literals noted in §4.4 and §10.3.

---

## Appendix B — Service Topology

`[CODE]` — `docker-compose.yml`, verified 2026-08-03.

| # | Service | Container | Entrypoint | Auto-start | Trades |
|---|---|---|---|---|---|
| 1 | iv-collector | `iv-collector` | `collectors.iv_collector_service` | Yes | No — data only |
| 2 | momentum | `momentum-strategy` | `momentum_runner.py` | **No** (`profiles: [momentum]`) | Would (S1/S2) |
| 3 | discount | `discount-strategy` | `main.py` | Yes | **Yes** (S5 + hedge) |
| 4 | break-bounce | `break-bounce-strategy` | `break_bounce_runner.py` | Yes | **Yes** (S3 + hedge) |
| 5 | directional-iv | `directional-iv-strategy` | `directional_iv_runner.py` | Yes | S6 — zero |
| 6 | iv-rank | `iv-rank-scanner` | `iv_rank_runner.py` | Yes | No — alerts |
| 7 | oi-buildup | `oi-buildup-scanner` | `oi_buildup_runner.py` | Yes | No — feeds auto-exit |
| 8 | gap-scan | `gap-scanner` | `gap_scanner_runner.py` | Yes | No — alerts |
| 9 | delivery-surge | `delivery-surge-scanner` | `delivery_surge_runner.py` | Yes | No — alerts |
| 10 | smart-money | `smart-money-scanner` | `smart_money_runner.py` | Yes | No — alerts |
| 11 | composite | `composite-scanner` | `composite_runner.py` | Yes | No — feeds S4 direction |
| 12 | sonar | `sonar-scanner` | `sonar_laplace_runner.py` | Yes | No — feeds veto |
| 13 | vol-expansion | `vol-expansion-strategy` | `vol_expansion_runner.py` | Yes | S4 — zero |
| 14 | iv-seller | `iv-seller-strategy` | `iv_seller_runner.py` | Yes | S7 — zero |
| 15 | convex-engine | `convex-engine` | `engine_runner.py` | Yes | **Yes** (S8) |
| 16 | dashboard | `dashboard` | `dashboard_app.py` | Yes | No — UI |

**API callers:** `iv-collector` (continuous chain sweep), `discount` (chains + candles, 15-min scans), `sonar` (5-min candles), `directional-iv` / `iv-seller` / `vol-expansion` (chains per scan). All other scanners are zero-API and read `iv_history.db` only.

**Dependency:** all strategies `depends_on: iv-collector`.

> **⚠ DIVERGENCE — Convex service role**
> **As-built:** `engine/paper.py` books real paper trades into the shared book under `ENGINE_PAPER_MODE=paper`, tagged `Convex`. Nine rows exist; four are closed at −₹29,420.
> **Intent:** `CLAUDE.md` describes convex-engine as *"P0 observe-only journal (decisions logged, no order path yet)."*
> **Impact:** An eighth trade-generating strategy is running and is the largest per-trade loss source in the book, while the primary documentation describes it as non-trading.

---

## Appendix C — Items Requiring Operator Input

Genuinely unresolved. Each blocks a specific implementation decision.

| # | Item | Section | Why it matters |
|---|---|---|---|
| C1 | **ADX period** for S1/S2 | §4.2 | Not a named constant; must be read from `momentum_strategy.py` source or specified |
| C2 | **HV window lengths and blend weights** | §4.5 | `weighted_hv` drives 25–30% of two scoring models; the composition is not surfaced as constants |
| C3 | **Is momentum (S1/S2) discontinued or dormant?** | §19.4 | Determines whether an implementer builds it at all |
| C4 | **Should S8 Convex be in scope?** | Appendix B | It trades and is the largest loss source, but was outside the stated seven |
| C5 | **Intended resolution of the IVP 20-vs-30 conflict** | §4.7 | Directly determines whether S4 can ever trade |
| C6 | **Is `PORTFOLIO_GATE_MODE=hard` with caps at 0 intended?** | §15.4 | Determines whether an implementer wires real caps or preserves the no-op |
| C7 | **Target broker for eventual live execution** | §17.2 | `place_bracket_order` is written against the Dhan surface; Upstox supplies data |
| C8 | **Was S5's kill-switch re-enable made knowing hedge coverage was 41%?** | §20.3 | Determines whether the current state is an informed risk or an unnoticed regression |

---

*End of specification.*

**Document statistics:** 22 numbered sections plus a reading-convention preamble and 3 appendices; 8 strategies; 16 divergences; 20 assumptions; 12 weaknesses; ~22,300 words.

**Verification basis:** all parameters read from source on 2026-08-03; all performance figures computed from `data/paper_trades.db` (573 rows, 372 closed) and `logs/*.log` on the same date. Operator input recorded on primary objective, regime hypothesis, capital model, execution status, and authority convention.
