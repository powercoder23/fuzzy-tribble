# `discount.py` Context

This file implements an options scanner that looks for "discounted premium" trades using live Dhan option-chain data, historical volatility context, IV history, and a simple strategy suggestion layer.

## High-level purpose

The scanner tries to find option contracts whose implied volatility looks relatively cheap compared with:

- the underlying's historical volatility
- the underlying's own past ATM implied volatility
- the current ATM IV skew for the same expiry

It then filters out weak candidates, scores the remaining ones, suggests a basic trade structure, and prints or saves the best opportunities.

## Main dependencies

`discount.py` depends on:

- `dhanhq` and `DhanContext` for broker API access
- `dotenv` to load credentials from `.env`
- `pandas` and `numpy` for data processing
- `requests` for Telegram notifications
- `f_o_stocks_list.get_stock_futures()` to build the live F&O stock universe
- `load_scrip_master_sqlite.update_scrip_master()` and `get_security_id_symbol_map()` to resolve symbols to Dhan security IDs

## Core constants

- `IV_HISTORY_FILE = Path("iv_history.csv")`
  Stores ATM IV snapshots used later for IV Rank and IV Percentile.
- `MIN_IV_SAMPLES = 30`
  Minimum history size before the scanner trusts historical IV mode.
- `DEFAULT_FNO_STOCKS`
  Fallback watchlist if the dynamic F&O universe build fails.
- `IV_HISTORY_COLUMNS`
  Standard schema for `iv_history.csv`.

## Helper functions

### `normalize_expiry_value`
Normalizes Dhan expiry values into `YYYY-MM-DD` strings.

### `unwrap_dhan_payload`
Some Dhan responses contain nested `data` keys. This function keeps unwrapping until it reaches the inner payload.

### `clip_score`
Keeps numeric scores inside a bounded range, usually `0` to `100`.

### `native_number`
Converts pandas/numpy values into plain Python floats and returns `None` for missing values.

## Main class: `DiscountedPremiumScanner`

This is the heart of the file. It manages broker access, market-data fetches, volatility calculations, strike analysis, scoring, and reporting.

### `__init__`
What happens here:

- validates `DHAN_CLIENT_ID` and access token
- creates the Dhan API client
- sets a hard-coded risk-free rate placeholder
- loads Telegram config from environment variables
- builds the F&O stock universe immediately

## 1. Universe building and notifications

### `load_fno_stocks`
This method creates the list of symbols to scan.

Flow:

1. Refresh Dhan/NSE scrip-master data.
2. Pull stock futures symbols from NSE.
3. Resolve those symbols into Dhan security IDs.
4. Filter out test symbols like `NSETEST`.
5. Always keep `NIFTY` and `BANKNIFTY`.
6. If anything fails, fall back to `DEFAULT_FNO_STOCKS`.

Result:
It returns a sorted dictionary like `{security_id: symbol}`.

### `send_telegram_summary`
After a scan finishes, this sends either:

- a "no opportunities found" message, or
- a short summary with the top 5 ideas

If Telegram credentials are missing, it logs and skips silently.

## 2. Market-data fetching

### `get_option_chain`
Fetches the live option chain for one underlying and one expiry from Dhan.

### `get_expiry_list`
Fetches all available expiries for an underlying and normalizes them into a clean sorted list.

This method is defensive because Dhan payload shape may vary.

### `fetch_historical_prices`
Fetches daily historical candles for about one year so the scanner can compute historical volatility.

Important behavior:

- uses `IDX_I` for indices
- uses `NSE_EQ` for stocks
- accepts different timestamp column names from the API
- returns a clean pandas DataFrame sorted by date

### `fetch_historical_iv`
Reads `iv_history.csv` and returns past ATM IV values for the given symbol/security ID.

This is used for:

- IV Rank
- IV Percentile

It also:

- handles missing columns safely
- filters out invalid IV values
- limits the history to the requested lookback

### `fetch_expired_option_data`
Fetches Dhan's expired-options rolling data for ATM contracts so the scanner can compare current cheap IV with how similar low-IV setups behaved historically.

Important behavior:

- uses the Dhan SDK method `expired_options_data` when available
- keeps a fallback request shape for `/v2/charts/rollingoption`
- defaults to the last 30 days
- requests `iv`, `close`, `volume`, and `spot`
- uses `interval=15`, `expiryFlag="MONTH"`, and `expiryCode=1`
- caches results in `self.expired_data_cache` to avoid repeated fetches during the same run
- also persists symbol-level ATM rolling history under `data/expired_options_cache/`
- when a local cache file exists, loads it first and only calls the API for the missing newer tail
- appends new rows, deduplicates by `timestamp`, and reuses the persisted history across process restarts

### `compute_iv_behavior_metrics`
Builds a lightweight historical-behavior summary from expired ATM option data.

It computes:

- IV percentile for the latest sample relative to that dataset
- forward 1-bar and 3-bar option returns
- a 20th-percentile low-IV threshold
- average forward move after low-IV observations

This adds a new optional signal layer without replacing the existing scoring system.

## 3. Volatility and market-context calculations

### `calculate_historical_volatility`
Computes annualized realized volatility from log returns over a rolling window.

### `calculate_hv_metrics`
Builds a multi-window HV view:

- `hv10`
- `hv20`
- `hv60`
- `weighted_hv`

The weighted value reduces noise from relying on just one lookback period.

### `calculate_iv_percentile`
Returns the percentage of historical IV observations below the current IV.

### `calculate_iv_rank`
Returns where current IV sits between historical min and max IV.

### `determine_trend_context`
Uses EMA20 and EMA50 from daily closes to classify trend as:

- `bullish`
- `bearish`
- `neutral`

### `days_to_expiry`
Calculates remaining days to expiry, with a minimum of 1.

### `compute_expected_move`
Uses the usual volatility-based approximation:

`spot * (IV / 100) * sqrt(dte / 365)`

This gives a rough expected move for strike relevance filtering.

### `extract_atm_reference_ivs`
Finds the ATM strike from the option chain and extracts:

- ATM strike
- ATM call IV
- ATM put IV
- average ATM IV

This becomes the reference for skew-based comparisons.

### `persist_iv_snapshot`
Saves one ATM IV snapshot into `iv_history.csv`.

Purpose:

- build a historical IV database over time
- enable IV Rank / IV Percentile later

Behavior:

- writes one row per day per symbol by default
- can optionally store intraday snapshots if `store_intraday=True`
- cleans and deduplicates rows before saving

## 4. Strategy construction and scoring

### `build_strategy_plan`
Transforms a shortlisted option into a simple trade idea.

Possible outputs:

- `Call Debit Spread`
- `Bear Put Spread`
- `Volatility Expansion Play`

It also computes:

- entry
- stop loss
- target
- risk/reward
- optional short strike for spread construction

This is a lightweight suggestion engine, not a full execution model.

### `score_option`
Assigns a weighted score out of 100.

Factors used:

- IV vs historical volatility
- delta quality
- vega sensitivity
- liquidity using OI and volume
- skew discount versus ATM IV
- strike relevance using expected move
- IV Rank / IV Percentile when enough IV history exists

Two scoring modes exist:

- `historical`
  Uses IV Rank and IV Percentile heavily when there is enough stored IV history.
- `skew`
  Falls back more on ATM skew and HV comparison when IV history is limited.

## 5. Single-strike analysis

### `scan_single_strike`
This is where each strike gets filtered and evaluated.

For both `ce` and `pe`, it:

1. skips missing option sides
2. checks liquidity using OI and volume
3. rejects extremely low-delta contracts unless hedging mode is enabled
4. requires non-zero implied volatility
5. compares contract IV with ATM IV to compute `skew_discount`
6. measures how far the strike is from spot using expected move
7. computes a score
8. optionally adjusts that score using historical ATM low-IV behavior
9. builds a strategy suggestion
10. creates human-readable reasons explaining why the contract qualified

Each qualifying candidate is returned as a rich dictionary containing:

- option type
- strike
- IV metrics
- HV metrics
- Greeks
- score
- trade plan
- liquidity
- expected move context
- reasons
- component-level score breakdown

## 6. Underlying-level scan flow

### `scan_underlying`
This is the main end-to-end workflow for one symbol.

What happens:

1. Select expiry.
   If none is given, it uses the nearest available expiry.
2. Fetch live option chain.
3. Read spot price and all strikes.
4. Extract ATM IV context.
5. Load historical ATM IV samples from `iv_history.csv`.
6. Persist today's ATM IV snapshot.
7. Compute days to expiry and expected move.
8. Fetch ATM expired-options rolling data and derive historical IV-behavior metrics.
9. Fetch one year of daily price history.
10. Compute historical volatility metrics and trend context.
11. Loop through every strike in the option chain.
12. Call `scan_single_strike` for each strike, passing the optional IV-behavior context.
13. Sort candidates by score descending.

Output:
It returns a list of discounted option opportunities for that one underlying.

## 7. Multi-symbol scan

### `scan_all_fno_stocks`
Runs `scan_underlying` across the full F&O universe.

What it adds:

- automatically picks `IDX_I` for index symbols and `NSE_FNO` for stocks
- filters by minimum score
- attaches `symbol` and `security_id`
- rate-limits requests with `sleep(1)`
- returns a pandas DataFrame of all results

## 8. Reporting

### `generate_report`
Prints a console report for every shortlisted opportunity.

It logs:

- symbol and strategy
- score
- IV / IV Rank / IV Percentile
- HV benchmark
- skew discount
- moneyness
- expected move
- bid/ask/mid
- entry / stop / target / R:R
- OI and volume
- Greeks
- top reasons for selection

It also prints summary statistics like:

- total opportunities
- average score
- average IV
- average HV
- average IV Rank
- breakdown by volatility mode
- breakdown by strategy

## 9. Script entry point

At the bottom, the `if __name__ == "__main__":` block shows example usage.

It does the following:

1. loads `.env`
2. creates `DiscountedPremiumScanner`
3. scans `NIFTY`
4. scans all F&O stocks
5. prints the report
6. saves results to `discounted_premiums.csv`
7. sends a Telegram summary

## Practical meaning of "discounted premium" in this file

In this implementation, a premium is considered "discounted" when several of these conditions line up:

- current option IV is lower than the underlying's weighted historical volatility
- current option IV is lower than historical IV regime levels
- current option IV is below ATM reference IV
- current option IV is below a historically cheap IV zone that has previously led to decent forward option moves
- strike is not too far from the expected move
- liquidity is acceptable
- delta is in a usable range

So this is not just "cheap premium" by price alone. It is mainly "cheap implied volatility with enough structure and liquidity to be tradable."

## Inputs and outputs

### Inputs

- `DHAN_CLIENT_ID`
- `DHAN_ACCESS_TOKEN`
- optional Telegram env vars
- live Dhan option chain and historical data
- local `iv_history.csv`

### Outputs

- in-memory list/DataFrame of opportunities
- `discounted_premiums.csv`
- `iv_history.csv`
- console logs
- optional Telegram summary

## Notes and limitations

- The risk-free rate is hard-coded.
- Strategy planning is heuristic, not options-pricing aware.
- HV is based on daily data only.
- The script assumes Dhan response fields like `oi`, `volume`, `greeks`, and `implied_volatility` are available.
- The score threshold `55` is a fixed heuristic.
- Trend logic is intentionally simple and based only on EMA structure.

## Short summary

`discount.py` is a live options idea generator. It scans an F&O universe, compares option IV against ATM IV and historical volatility context, scores contracts that look relatively cheap but liquid, suggests a basic trade structure, and reports the best candidates.
