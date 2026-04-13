# `discount.py` Updated Context

This document is an up-to-date AI-readable context for [`discount.py`](/c:/Users/dhira/Desktop/plan/discount.py). It reflects the current code as of April 11, 2026, not the older simplified description.

## Purpose

`discount.py` is a live options scanner built around the Dhan API. Its goal is to identify option contracts whose implied volatility looks relatively cheap, but only when that cheapness is supported by liquidity, delta quality, expected-move relevance, historical IV regime, and broader chain context.

It is not simply finding low-priced options. It is looking for options that appear underpriced in volatility terms and still tradable.

## What the file does end to end

At a high level, the scanner:

1. builds the F&O universe dynamically
2. fetches the nearest expiry option chain for each symbol
3. derives ATM IV and chain-wide call/put IV context
4. loads historical ATM IV snapshots from SQLite
5. optionally uses premarket intraday IV change as extra context
6. pulls daily spot-history to compute realized volatility
7. pulls expired-options rolling data to study how similar low-IV setups behaved historically
8. evaluates every strike on both CE and PE sides
9. applies hard quality filters
10. scores surviving contracts
11. builds a simple strategy suggestion
12. returns top opportunities, logs a report, writes CSV, and can send a Telegram summary

## Main dependencies

`discount.py` depends on:

- `dhanhq` / `DhanContext` for market-data access
- `pandas`, `numpy`, `scipy.stats`
- `requests`
- `dotenv`
- [`f_o_stocks_list.py`](/c:/Users/dhira/Desktop/plan/f_o_stocks_list.py) for live NSE stock-futures symbols
- [`load_scrip_master_sqlite.py`](/c:/Users/dhira/Desktop/plan/load_scrip_master_sqlite.py) for scrip-master refresh and symbol-to-security-id resolution

It is also used by [`main.py`](/c:/Users/dhira/Desktop/plan/main.py), which initializes the IV database and runs scheduled scans plus premarket warmups.

## Persistent files and storage

Current persistence is split across CSV and SQLite:

- `iv_history.db`
  Main store for ATM IV snapshots.
- `iv_history.csv`
  Legacy file still referenced for one-time migration.
- `iv_migrated.flag`
  Prevents repeated CSV-to-SQLite migration.
- `data/expired_options_cache/`
  Per-symbol cached rolling expired-option history used to avoid refetching old data.
- `discounted_premiums.csv`
  Latest scan output when saved.

## Core constants

- `DB_PATH = "iv_history.db"`
- `EXPIRED_OPTIONS_CACHE_DIR = Path("data/expired_options_cache")`
- `MIN_IV_SAMPLES = 30`
- `DEFAULT_FNO_STOCKS`
- `IV_HISTORY_COLUMNS`

`MIN_IV_SAMPLES` is important because it decides when the scanner can trust historical-IV mode instead of falling back to chain-skew mode.

## Top-level helper functions

### `init_iv_db()`

Creates the SQLite table `iv_history` if it does not exist.

Schema highlights:

- `security_id`
- `symbol`
- `timestamp`
- `spot_price`
- `atm_strike`
- `atm_iv`
- `atm_call_iv`
- `atm_put_iv`
- `data_type`

There is a uniqueness constraint on:

- `(security_id, timestamp, data_type)`

`data_type` is either:

- `daily`
- `intraday`

### `migrate_csv_to_sqlite()`

One-time migration from legacy `iv_history.csv` into `iv_history.db`.

Important behavior:

- skips migration if `iv_migrated.flag` exists
- skips if CSV is missing or empty
- inserts migrated rows as `data_type = 'daily'`
- uses `ON CONFLICT DO NOTHING`

### `normalize_expiry_value(value)`

Normalizes Dhan expiry payloads into `YYYY-MM-DD`.

### `unwrap_dhan_payload(payload)`

Some Dhan responses have nested `data -> data -> data`. This helper keeps unwrapping until it reaches the innermost dict.

### `clip_score(value, floor=0.0, ceiling=100.0)`

Bounds scores to `0..100`.

### `native_number(value)`

Converts pandas/numpy numbers into plain Python floats and returns `None` for missing values.

## Main class: `DiscountedPremiumScanner`

This class contains all scanner logic.

### Constructor

`__init__(hardtoken, client_id="1104878989", store_intraday=False)`

What it initializes:

- Dhan auth context and client
- hard-coded `risk_free_rate = 0.065`
- Telegram credentials from env
- expired-option cache directory
- in-memory expired-data cache
- `_scan_quality_stats`
- dynamic F&O universe via `load_fno_stocks()`

`store_intraday=True` matters during premarket warmup runs because it stores ATM IV snapshots as intraday rows.

## Universe building

### `load_fno_stocks()`

Builds the scan universe dynamically.

Flow:

1. refresh scrip master
2. fetch current NSE stock-futures list
3. resolve symbols into Dhan security IDs
4. remove test symbols like `NSETEST`
5. always include reserved indices:
   - `13 -> NIFTY`
   - `14 -> BANKNIFTY`
6. if live resolution fails, fall back to `DEFAULT_FNO_STOCKS`
7. return a symbol-sorted `{security_id: symbol}` dict

## Notification output

### `send_telegram_summary(opportunities_df)`

Sends a compact Telegram message after a run.

If there are results, the summary includes:

- timestamp
- number of matches
- median PCR if available
- median CE/PE skew ratio if available
- top rows grouped by strategy

If no Telegram credentials are present, the method logs and returns.

## Market-data methods

### `get_option_chain(underlying_security_id, underlying_segment, expiry)`

Calls Dhan option-chain API.

Expected `underlying_segment` values:

- `IDX_I` for indices
- `NSE_FNO` for stock F&O

### `get_expiry_list(underlying_security_id, underlying_segment)`

Gets all expiries for one underlying and normalizes them.

This method is defensive because Dhan may return:

- a list
- a dict
- nested dict/list structures
- ISO timestamps or date-like strings

It returns a unique sorted list of `YYYY-MM-DD` strings.

### `fetch_historical_prices(security_id, exchange_segment, from_date, to_date)`

Fetches daily OHLC data for realized-volatility calculation.

Behavior:

- converts stock requests to `NSE_EQ`
- keeps indices on `IDX_I`
- infers `instrument_type` as `INDEX` or `EQUITY`
- tolerates different timestamp column names
- returns a date-sorted DataFrame

This dataset is later used for:

- historical volatility
- trend context via EMA20/EMA50

### `fetch_historical_iv(security_id, exchange_segment, lookback_days=252)`

Reads historical ATM IV from SQLite, not from the legacy CSV.

Current behavior:

- only loads `data_type = 'daily'`
- sorts by timestamp
- drops bad or missing values
- keeps IV values only in the range `1..200`
- returns the most recent `lookback_days` ATM IV samples as a Python list

This is used for:

- IV Rank
- IV Percentile
- deciding whether the scanner has enough IV history to enter `historical` mode

## Expired-options cache and behavior modeling

These methods are one of the major new additions compared with the older context file.

### `_expired_options_cache_path(...)`

Maps `(security_id, exchange_segment, option_type, strike)` into a CSV filename inside `data/expired_options_cache/`.

### `_load_expired_option_cache(cache_path)`

Loads and sanitizes cached expired-option history.

It standardizes columns to:

- `timestamp`
- `iv`
- `close`
- `volume`
- `spot`

### `_save_expired_option_cache(cache_path, df)`

Writes the sanitized cache back to disk.

### `_merge_expired_option_frames(existing_df, new_df)`

Merges new rows with persisted rows, sorts by timestamp, drops duplicates, and standardizes numeric fields.

### `fetch_expired_option_data(...)`

Fetches rolling expired-option data for historical low-IV behavior analysis.

Default behavior:

- `option_type="CALL"`
- `strike="ATM"`
- date range defaults to last 30 days
- uses `OPTIDX` for indices and `OPTSTK` for stocks
- requests `close`, `iv`, `volume`, `spot`
- requests interval `15`
- uses monthly expiry roll logic with `expiry_flag="MONTH"` and `expiry_code=1`

Fetch order:

1. check in-memory cache
2. load persisted CSV cache
3. fetch only the missing tail if possible
4. merge and persist updated cache
5. return only the requested date slice

It prefers Dhan SDK method `expired_options_data` if available, otherwise falls back to direct POST request:

- `https://api.dhan.co/v2/charts/rollingoption`

If API fetch fails, it falls back to persisted cache instead of failing hard.

### `compute_iv_behavior_metrics(df)`

Analyzes expired-option history to measure whether similarly low IV has historically led to option-price expansion.

It computes:

- `forward_return_1`
- `forward_return_3`
- `low_iv_threshold` as the 20th percentile of IV
- `avg_move_after_low_iv`
- current-sample `iv_percentile` within that dataset

Returns:

- `iv_percentile`
- `avg_move_after_low_iv`
- `low_iv_threshold`

This is later used as a score boost or penalty in strike evaluation.

## Volatility and trend logic

### `calculate_historical_volatility(price_df, window=20)`

Computes annualized realized volatility using daily log returns and a rolling window.

### `calculate_hv_metrics(price_df)`

Builds:

- `hv10`
- `hv20`
- `hv60`
- `weighted_hv`

Weighting is:

- `hv10`: `0.3`
- `hv20`: `0.4`
- `hv60`: `0.3`

### `calculate_iv_percentile(current_iv, historical_ivs)`

Percent of historical IV observations below current IV.

### `calculate_iv_rank(current_iv, historical_ivs)`

Computes:

`(current_iv - min_iv) / (max_iv - min_iv) * 100`

### `determine_trend_context(price_df)`

Trend regime based on EMA structure:

- bullish if `last_close > ema20 > ema50`
- bearish if `last_close < ema20 < ema50`
- otherwise neutral

Returns:

- `trend`
- `ema20`
- `ema50`
- `last_close`

### `days_to_expiry(expiry)`

Returns DTE with minimum 1 day.

### `compute_expected_move(spot_price, reference_iv, dte)`

Uses:

`spot_price * (reference_iv / 100) * sqrt(dte / 365)`

### `extract_atm_reference_ivs(option_chain, spot_price)`

Finds the nearest strike to spot and returns:

- `atm_strike`
- `atm_call_iv`
- `atm_put_iv`
- `atm_iv` as average of valid ATM CE/PE IV values

## IV snapshot persistence and premarket context

### `persist_iv_snapshot(...)`

Stores ATM IV snapshots into SQLite.

Rules:

- skips invalid ATM IV
- stores `daily` rows by default
- stores `intraday` rows when `store_intraday=True`

This is how the system builds its own IV history over time.

### `build_premarket_context(security_id)`

Reads same-day `intraday` ATM IV rows from SQLite and returns:

- `iv_change = current_iv - opening_iv`

Only returns context if there are at least 2 intraday snapshots for that day.

This context is used as a directional score adjustment:

- if intraday IV change is below `-2`, score gets boosted
- if intraday IV change is above `2`, score gets penalized

## Strategy suggestion layer

### `build_strategy_plan(option_type, strike_price, spot_price, mid_price, option_chain, expected_move, trend, score)`

Builds a lightweight trade suggestion from a shortlisted contract.

Possible strategies:

- `Call Debit Spread`
- `Bear Put Spread`
- `Volatility Expansion Play`

Logic:

- calls in bullish trend prefer a debit spread
- puts in bearish trend prefer a bear put spread
- otherwise treat as a standalone volatility-expansion idea

Outputs:

- `strategy`
- `short_strike`
- `entry`
- `stop_loss`
- `target`
- `risk_reward`

Current heuristics:

- `stop_loss = mid_price * 0.65`
- `target = mid_price * 1.8`

## Scoring model

### `score_option(...)`

This method returns:

- `score`
- `component_scores`

There are two scoring modes.

### Historical mode

Used when `len(historical_ivs) >= MIN_IV_SAMPLES`.

Core components:

- `cheap_vol_score` from IV Rank and IV Percentile
- `hv_score`
- `delta_score`
- `vega_score`
- `liquidity_score`
- `skew_score`
- `relevance_score`

Weights:

- IV regime: `25%`
- IV vs HV: `20%`
- delta: `15%`
- vega: `10%`
- liquidity: `10%`
- skew: `10%`
- strike relevance: `10%`

### Skew mode

Used when historical IV samples are insufficient.

Weights:

- skew: `40%`
- IV vs HV: `10%`
- delta: `15%`
- vega: `10%`
- liquidity: `10%`
- strike relevance: `15%`

### Scoring ingredients

- `hv_score`
  Higher when option IV is below weighted HV.
- `delta_score`
  Best in roughly `0.15..0.40` absolute delta.
- `vega_score`
  Based on `vega * 400`, clipped.
- `liquidity_score`
  Uses `log1p(oi)` and `log1p(volume)`.
- `skew_score`
  Based on same-side chain cheapness.
- `relevance_score`
  Penalizes strikes too far outside expected move.

## Single-strike evaluation

### `scan_single_strike(...)`

This is the main strike-level engine.

For each strike it evaluates both:

- `ce`
- `pe`

### Hard filters

Contracts are skipped if:

- side data is missing
- `oi < 1000`
- `volume <= 0`
- `volume < 200`
- absolute delta `< 0.10` unless hedging mode is enabled
- `implied_volatility == 0`
- strike lies beyond `2.0x` expected move

### Same-side chain context

For calls and puts separately, it compares contract IV to:

- same-side mean IV
- same-side IV standard deviation
- same-side average volume

It computes:

- `skew_z = (current_iv - reference_iv) / skew_std`
- `skew_discount = -skew_z`
- `iv_context`:
  - `below_chain_mean`
  - `above_chain_mean`

### Quality gate

Before final scoring, it builds a simple `quality_score`.

Points are added for:

- positive `skew_discount`
- preferred delta range `0.15..0.40`
- `expected_move_ratio <= 1.2`
- `volume > 1000`

Candidates with `quality_score < 2` are rejected.

### Score adjustments after base score

After `score_option()`, the score is adjusted using several context layers.

#### Premarket IV adjustment

- `iv_change < -2` => `+8`
- `iv_change > 2` => `-10`

#### Distance / chain percentile / volume adjustments

- `expected_move_ratio > 1.5` => `-20`
- same-side chain IV percentile `< 20` => `+7`
- same-side chain IV percentile `> 80` => `-7`
- volume above same-side average => `+7`

#### Historical low-IV behavior adjustment

If current IV is below historical `low_iv_threshold`:

- positive `avg_move_after_low_iv > 0.01` => `+10`
- otherwise => `-10`

#### Market-bias penalty

Uses whole-chain context:

- `pcr < 0.7` penalizes calls by `-8`
- `pcr > 1.3` penalizes puts by `-8`
- `skew_ratio < 0.9` penalizes puts by `-5`
- `skew_ratio > 1.1` penalizes calls by `-5`

### Human-readable reasons

Each shortlisted candidate includes a `reason` list describing why it qualified, such as:

- low IV Rank or Percentile
- IV below weighted HV
- preferred delta range
- same-side chain cheapness
- inside expected move
- cheap chain percentile
- above-average volume
- strong liquidity
- favorable historical low-IV behavior

### Candidate output schema

Each returned candidate dict currently includes:

- `symbol`
- `strategy`
- `strike`
- `short_strike`
- `type`
- `vol_mode`
- `iv_context`
- `iv`
- `iv_rank`
- `iv_percentile`
- `hv`
- `hv10`
- `hv20`
- `hv60`
- `delta`
- `vega`
- `theta`
- `score`
- `entry`
- `stop_loss`
- `target`
- `risk_reward`
- `reason`
- `mid_price`
- `bid`
- `ask`
- `spot`
- `moneyness`
- `oi`
- `volume`
- `expected_move`
- `expected_move_ratio`
- `quality_score`
- `atm_iv`
- `atm_reference_iv`
- `skew_discount`
- `pcr`
- `skew_ratio`
- `market_bias`
- `trend`
- `dte`
- `component_scores`

This output schema is important for any downstream AI or automation because it exposes both the final recommendation and the supporting diagnostics.

## Underlying-level scan flow

### `scan_underlying(security_id, security_segment, security_name, expiry=None, use_hv=True)`

This is the full workflow for one underlying.

Actual flow:

1. resolve expiry if not provided
2. fetch option chain
3. read `last_price` and `oc`
4. compute chain-wide call/put IV lists and average volumes
5. compute:
   - `call_mean`
   - `put_mean`
   - `call_std`
   - `put_std`
   - `pcr`
   - `skew_ratio`
   - `market_bias`
6. extract ATM IV context
7. read same-day premarket intraday IV context
8. load historical ATM IV samples
9. decide between `historical` and `skew` mode
10. compute ATM IV Rank and Percentile for logging
11. persist today’s ATM IV snapshot
12. compute DTE and expected move
13. fetch rolling ATM expired-option data
14. compute IV-behavior metrics
15. fetch daily spot history
16. compute HV metrics and trend context
17. loop all strikes and call `scan_single_strike(...)`
18. sort by score descending
19. cap per underlying to top 10 opportunities

It also logs pre-quality and post-quality counts from the quality gate.

## Multi-symbol scan flow

### `scan_all_fno_stocks(security_ids=None, expiry=None, min_discount_score=55)`

Scans the whole F&O universe.

Behavior:

- uses dynamic universe by default
- uses `IDX_I` for `NIFTY` and `BANKNIFTY`
- uses `NSE_FNO` for stocks
- filters candidates with `score >= min_discount_score`
- adds `symbol` and `security_id`
- sleeps 1 second between underlyings

### Global output cap

After collecting all opportunities:

1. sort by score
2. split into calls and puts
3. keep up to `120` from each side
4. recombine and cap total results at `200`

So the scanner now explicitly tries to avoid one side dominating the global result set.

## Reporting

### `generate_report(opportunities_df)`

Prints a detailed console report for each candidate plus summary statistics.

Per-candidate reporting includes:

- symbol
- strategy
- score
- IV
- IV Rank / Percentile or fallback IV context
- weighted HV
- skew discount
- moneyness
- expected move and expected-move ratio
- bid / ask / mid
- entry / stop / target / R:R
- OI and volume
- delta / theta / vega
- top reasons

Summary includes:

- total opportunities
- average score
- average IV
- average HV
- average IV Rank
- breakdown by volatility mode
- breakdown by strategy

## How `main.py` uses this file

[`main.py`](/c:/Users/dhira/Desktop/plan/main.py) does three important things with `discount.py`:

1. calls `init_iv_db()`
2. calls `migrate_csv_to_sqlite()`
3. schedules two workflows:
   - `premarket_warmup`
   - `discount`

### Premarket warmup

The premarket job:

- creates the scanner with `store_intraday=True`
- reads the first expiry for the first 10 symbols in the F&O universe
- fetches chain data
- extracts ATM IV
- persists intraday snapshots

This job exists purely to create same-day intraday IV context for later scoring.

### Discount scan

The main scheduled run:

- refreshes token
- scans all F&O stocks
- generates report
- saves CSV
- sends Telegram summary

## Practical meaning of “discounted premium” in the current code

In the present implementation, a contract is attractive when many of these conditions align:

- its IV is low relative to same-side chain distribution
- its IV is low relative to weighted historical volatility
- its IV is low relative to the underlying’s own historical ATM IV regime
- it sits in a useful delta band
- it has sufficient OI and volume
- it is not too far outside expected move
- historical low-IV behavior suggests premium expansion may follow
- premarket IV has compressed rather than expanded too much
- whole-chain context does not strongly argue against that side

So the file is really a multi-factor volatility-discount scanner, not a simple cheap-option screener.

## Important limitations and assumptions

- `risk_free_rate` exists but is currently just a hard-coded placeholder.
- No Black-Scholes pricing model is used to estimate fair value directly.
- Strategy planning is heuristic, not execution-grade.
- Historical volatility uses daily data only.
- Expired-option behavior analysis currently focuses on rolling ATM-style data, not per-strike historical replay across the whole chain.
- The scanner assumes Dhan payloads include usable `greeks`, `volume`, `oi`, and `implied_volatility`.
- Score thresholds and penalties are heuristic and tuned manually.

## Short AI summary

If another AI needs the shortest reliable mental model:

`discount.py` scans Dhan option chains, builds same-side IV cheapness signals plus historical-IV and realized-volatility context, filters out low-quality contracts, adjusts scores using premarket and historical low-IV behavior, suggests a simple strategy structure, and returns rich per-contract diagnostics for the best CE and PE opportunities across the F&O universe.
