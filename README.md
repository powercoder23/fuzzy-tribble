# Trading Plan Backtest Engine

This project turns the screenshot trading plan into a testable Python backtest with explicit rule assumptions.

## What is modeled

- Higher-timeframe bias alignment using swing structure on `1M`, `1w`, and `1d`
- Break of structure and change of character from swing breaks
- Fair value gap detection
- Order block approximation as the last opposing candle before structure break
- Discount and premium filtering using the recent dealing range midpoint
- Lower-timeframe confirmation using recent structure breaks
- Fixed stop loss and configurable risk/reward target

## What is not modeled yet

The screenshot includes discretionary concepts that are hard to backtest objectively without more rules:

- Trendline breaks
- Breaker blocks
- Head and shoulders
- Double tops and bottoms
- Candle quality like "strong rejection" without a precise candle definition

Those can be added later, but they should be written as hard rules first or the results will be misleading.

## CSV format

Use a CSV with these columns:

```csv
timestamp,open,high,low,close,volume
2024-01-01 09:15:00,100.0,101.0,99.5,100.7,1200
```

`timestamp` should be sorted or sortable as ISO-like datetimes.

## Usage

Generate sample data:

```bash
python trading_plan_backtest.py --generate-sample sample_ohlcv.csv --sample-bars 25000
```

Run the backtest:

```bash
python trading_plan_backtest.py --data sample_ohlcv.csv --output backtest_trades.csv
```

Optional flags:

```bash
python trading_plan_backtest.py --data sample_ohlcv.csv --poi-timeframe 1h --rr 3.0 --risk-per-trade 0.005
```

Print diagnostics for one run:

```bash
python trading_plan_backtest.py --data dhan_ohlcv.csv --output backtest_trades.csv --diagnostics
```

Test a specific entry session:

```bash
python trading_plan_backtest.py --data dhan_ohlcv.csv --poi-timeframe 1h --rr 2.0 --session-start 09:15 --session-end 10:45 --output backtest_trades_opening.csv --diagnostics
```

Try a different confirmation mode:

```bash
python trading_plan_backtest.py --data dhan_ohlcv.csv --poi-timeframe 1h --rr 2.0 --session-start 09:15 --session-end 10:45 --confirmation-mode bos_and_rejection --output backtest_trades_opening_confirmed.csv --diagnostics
```

Run a simple parameter sweep:

```bash
python trading_plan_backtest.py --data dhan_ohlcv.csv --sweep-poi 1h,4h --sweep-rr 1.5,2.0,3.0 --sweep-output backtest_sweep.csv
```

Compare multiple sessions in one sweep:

```bash
python trading_plan_backtest.py --data dhan_ohlcv.csv --sweep-poi 1h --sweep-rr 2.0 --sweep-session full,09:15-10:45,10:00-13:00,13:00-15:15 --sweep-output backtest_session_sweep.csv
```

Compare multiple confirmation modes:

```bash
python trading_plan_backtest.py --data dhan_ohlcv.csv --sweep-poi 1h --sweep-rr 2.0 --sweep-session 09:15-10:45 --sweep-confirmation bos_or_choch,bos_only,choch_only,rejection_candle,bos_and_rejection --sweep-output backtest_confirmation_sweep.csv
```

## NIFTY ready-to-run scripts

Backtest the current best NIFTY configuration directly:

```bash
python nifty_backtest.py --from-date "2025-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --save-fetched-data dhan_ohlcv.csv --output nifty_backtest_trades.csv --diagnostics
```

Use the same runner for another index by changing the security id and display name:

```bash
python nifty_backtest.py --symbol-name BANKNIFTY --security-id your_banknifty_security_id --from-date "2025-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --output banknifty_backtest_trades.csv --diagnostics
```

If you omit `--security-id`, the script will try to resolve the index from Dhan's official scrip master using `--symbol-name`:

```bash
python nifty_backtest.py --symbol-name BANKNIFTY --from-date "2025-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --output banknifty_backtest_trades.csv --diagnostics
```

Watch for new NIFTY signals and send Telegram alerts:

```bash
python nifty_telegram_alerts.py --once
```

Watch the production intraday NIFTY strategy and send ATM option alerts such as `BUY NIFTY 23500 CE @ 255`:

```bash
python nifty_intraday_production_alerts.py --once
```

Dry-run the production option alert format on local candles and inject a sample option premium:

```bash
python nifty_intraday_production_alerts.py --data dhan_ohlcv.csv --once --dry-run --option-premium-override 255 --expiry 2026-03-26
```

Send alerts for another index:

```bash
python nifty_telegram_alerts.py --symbol-name BANKNIFTY --security-id your_banknifty_security_id --once
```

You can also omit `--security-id` here and let the script resolve it from Dhan's scrip master:

```bash
python nifty_telegram_alerts.py --symbol-name BANKNIFTY --once
```

Run the Telegram watcher continuously every 5 minutes:

```bash
python nifty_telegram_alerts.py --poll-seconds 300
```

Dry-run the alert script on a local CSV:

```bash
python nifty_telegram_alerts.py --data dhan_ohlcv.csv --once --dry-run
```

## F&O stock backtests

Backtest one underlying stock:

```bash
python stock_backtest.py --symbol-name RELIANCE --from-date "2025-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --output reliance_backtest_trades.csv --diagnostics
```

Batch-test a list of liquid F&O stocks and save a comparison CSV:

```bash
python stock_batch_backtest.py --symbols RELIANCE,HDFCBANK,ICICIBANK,SBIN,INFY,TCS --from-date "2025-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --output stock_batch_comparison.csv
```

## NIFTY timeframe lab

Compare reduced-timeframe variants of the current NIFTY strategy in a separate experiment runner:

```bash
python nifty_timeframe_lab.py --data dhan_ohlcv.csv --from-date "2025-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --output nifty_timeframe_lab.csv
```

Compare one-filter-at-a-time intraday variants in a separate experiment runner:

```bash
python nifty_intraday_filter_lab.py --data dhan_ohlcv.csv --from-date "2025-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --output nifty_intraday_filter_lab.csv
```

Compare combined high-potential intraday ideas in another separate runner:

```bash
python nifty_combo_lab.py --data dhan_ohlcv.csv --from-date "2025-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --output nifty_combo_lab.csv
```

Compare same-day square-off variants for the best combo setups:

```bash
python nifty_intraday_exit_lab.py --data dhan_ohlcv.csv --from-date "2025-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --output nifty_intraday_exit_lab.csv
```

Test one specific intraday trading day while still using historical context:

```bash
python nifty_single_day_intraday.py --data dhan_ohlcv.csv --trade-date 2026-01-29 --from-date "2025-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --output nifty_single_day_intraday_trades.csv
```

Report trades day by day for a chosen date range, while still using historical context:

```bash
python nifty_multi_day_report.py --from-date "2026-03-01 09:15:00" --to-date "2026-03-18 15:30:00" --strategies winning_15m,fast_5m --output nifty_multi_day_report.csv --trades-output nifty_multi_day_trades.csv
```

Run a simpler generic intraday backtest runner with preset strategy families:

```bash
python intraday_backtest.py --preset 15m --data dhan_ohlcv.csv --from-date "2025-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --output intraday_backtest_trades.csv --diagnostics
```

Use the same runner for a faster 5-minute index setup fetched from Dhan:

```bash
python intraday_backtest.py --preset 5m --symbol-type index --symbol-name NIFTY --from-date "2026-03-01 09:15:00" --to-date "2026-03-18 15:30:00" --save-fetched-data dhan_ohlcv.csv --output intraday_backtest_trades.csv
```

Or run it for a stock symbol:

```bash
python intraday_backtest.py --preset 15m --symbol-type stock --symbol-name RELIANCE --from-date "2026-03-01 09:15:00" --to-date "2026-03-18 15:30:00" --output reliance_intraday_backtest_trades.csv
```

## Dhan broker fetch

The backtest script can also pull candles directly from Dhan and then run the same strategy logic.

Install the SDK:

```bash
pip install dhanhq
```

Set credentials:

```bash
set DHAN_CLIENT_ID=your_client_id
set DHAN_ACCESS_TOKEN=your_access_token
```

Or create a local `.env` file in the project root:

```env
DHAN_CLIENT_ID=your_client_id
DHAN_ACCESS_TOKEN=your_access_token
```

Fetch and backtest:

```bash
python trading_plan_backtest.py --broker dhan --security-id 13 --exchange-segment IDX_I --instrument-type INDEX --from-date "2026-01-01 09:15:00" --to-date "2026-03-14 15:30:00" --interval 15 --save-fetched-data dhan_ohlcv.csv --output backtest_trades.csv
```

Notes:

- `--data` and `--broker dhan` are alternative inputs.
- Dhan fetches are batched in 90-day windows.
- The fetch layer accepts both the SDK list-of-candles shape and the documented parallel-array candle shape.

## Strategy assumptions used in code

1. Monthly, weekly, and daily structure must agree on direction.
2. Price must tap a recent `4h` point-of-interest zone, either FVG or order block, in the direction of bias.
3. Price must be in discount for longs or premium for shorts based on the recent POI timeframe range.
4. A recent structure break on `1h` or `15m` acts as confirmation.
5. Entry is taken at the close of the confirming base-timeframe candle.
6. Stop goes beyond the tapped zone.
7. Target is a fixed `R` multiple.

## Outputs

- Console summary with trades, win rate, average `R`, total `R`, and compounded equity estimate
- `backtest_trades.csv` with individual trades

## Next improvements

- Add explicit monthly bias support
- Add breaker block logic
- Add objective rejection-candle filters
- Add session filters and spread/slippage
- Add walk-forward testing on real market data
