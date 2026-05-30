# PROJECT_CONTEXT.md
> Technical handoff document — generated 2026-05-30.
> Covers every active file, all databases, all scheduler logic, and data flows.

---

## 1. Project Overview

**What it does:** An automated NSE F&O (Futures & Options) intraday trading system
for Indian equities. It monitors the entire F&O universe (~200+ stocks) continuously,
detects entry signals using multiple strategies, and either fires Telegram alerts or
places real orders via Dhan / Upstox broker APIs.

**Main goals:**
- Scan all F&O stocks for tradeable options opportunities every few minutes during market hours.
- Collect and persist IV (Implied Volatility) data as a shared time-series database so multiple strategies can consume it without each calling the API directly.
- Execute trades automatically when `AUTO_EXECUTE=true`; otherwise alert-only mode.
- Decouple data collection from signal generation so each runs at its own cadence.

**Brokers:** Dhan (primary order placement), Upstox (data provider, switchable via `DATA_PROVIDER` env var).

---

## 2. Folder Structure

```
c:\Users\dhira\Desktop\plan\
│
├── ROOT — Active Python strategy / service files
│   ├── config.py                        Central config loader (env vars → Config class)
│   ├── main.py                          Discount + Directional IV scheduler wrapper
│   ├── discount.py                      Discounted Premium Scanner (Strategy 1) — ~1,400 lines
│   ├── directional_iv_strategy.py       Directional IV Scanner (Strategy 2, discontinued)
│   ├── directional_iv_runner.py         Directional IV runner — called from main.py
│   ├── directional_iv_config.py         Constants for Directional IV
│   ├── momentum_strategy.py             ORB + VWAP scanner (Strategy 3, discontinued)
│   ├── momentum_runner.py               Momentum service scheduler
│   ├── momentum_config.py               Constants for Momentum
│   ├── break_bounce_strategy.py         Break & Bounce scanner (Strategy 4, ACTIVE)
│   ├── break_bounce_runner.py           Break & Bounce service scheduler
│   ├── break_bounce_config.py           Constants for Break & Bounce
│   ├── iv_collector_service.py          IV Collector (Service 1 — shared data layer)
│   ├── iv_store.py                      SQLite read/write layer for iv_history.db
│   ├── token_manager.py                 Dhan access token TOTP refresh
│   ├── upstox_token_manager.py          Upstox access token refresh (file-locked)
│   ├── upstox_adapter.py                UpstoxDhanAdapter — unifies SDK interfaces
│   ├── instrument_mapper.py             Symbol ↔ security_id mapping utilities
│   ├── f_o_stocks_list.py               NSE F&O contract file fetcher + cache
│   ├── load_scrip_master_sqlite.py      Dhan CSV scrip master → SQLite loader
│   ├── trader_logger.py                 Structured JSONL + human-readable EOD logging
│   ├── init_upstox_token.py             One-shot Upstox token initialiser (called by entrypoint)
│   ├── deals.py                         NSE bulk/block/short deals scraper (standalone script)
│   ├── complete_json_tosqlite.py        One-off data migration utility
│   ├── test_expiry_fetch.py             Expiry list fetch test utility
│   └── __pycache__/
│
├── DOCKER
│   ├── docker-compose.yml               Production compose (4 active services)
│   ├── docker-compose.prod.yml          Alternative production compose
│   ├── docker-compose.prod.validated.yaml
│   ├── docker-compose.dev.yaml          Dev compose (all services including paused ones)
│   ├── Dockerfile.iv-collector
│   ├── Dockerfile.discount
│   ├── Dockerfile.break-bounce
│   ├── Dockerfile.momentum
│   ├── Dockerfile.directional
│   ├── Dockerfile                       Generic base (unused)
│   ├── entrypoint.sh                    Token init + service start
│   └── run-prod.sh                      Production launcher script
│
├── data/
│   ├── api-scrip-master.db              SQLite: Dhan instrument directory (~36 MB)
│   ├── api-scrip-master.csv             CSV backup of scrip master (~30 MB)
│   ├── complete.db                      SQLite: Upstox full instrument directory (~40 MB)
│   ├── iv_history.db                    SQLite: IV snapshot history — shared across all services
│   ├── scrip_master_last_updated.txt    Timestamp of last scrip master update
│   ├── fno_cache/                       NSE F&O contract file cache (.csv.gz per day)
│   ├── expired_options_cache/           Cached expired option metadata
│   ├── tokens/
│   │   ├── access_token.json            Dhan token
│   │   └── upstox_access_token.json     Upstox token
│   ├── signals/                         Per-scan signal log files
│   └── break_bounce_trades.csv          Trade journal (CSV)
│
├── logs/
│   ├── scanner.log
│   ├── discount.log
│   ├── momentum.log
│   ├── break_bounce.log
│   ├── iv_collector.log
│   ├── directional_iv.log
│   ├── scan_YYYY-MM-DD.jsonl            Machine-readable event stream
│   └── scan_YYYY-MM-DD_summary.txt      Human-readable EOD summary
│
├── old/                                 Archived / legacy code (not imported anywhere)
│   ├── discount-old.py                  Prior version of discount strategy (~85 KB)
│   ├── discount copy.py
│   ├── discount copy before premarkeet.py
│   ├── discount_context.md
│   ├── banknifty_opening_range_breakout.py
│   ├── basktest_engine.py
│   ├── intraday_backtest.py
│   ├── nifty_*.py  (10 files)           Various backtesting experiments
│   ├── trading_plan_backtest.py
│   ├── run_analysis.py
│   └── test.py
│
├── .env                                 Live credentials (never commit)
├── .env.example                         Redacted template
├── .gitignore / .gitattributes
├── .dockerignore
├── .mcp.json                            MCP server configuration
├── CLAUDE.md                            Architecture notes (Claude project memory)
├── README.md
├── fix_discount.md                      Notes on discount strategy fixes
├── requirements.txt
├── discounted_premiums.csv              Last scan output (regenerated each run)
├── iv_history.csv                       CSV backup of iv_history.db
└── iv_migrated.flag                     One-off migration marker
```

---

## 3. Files Inventory

### Core Infrastructure

#### `config.py`
**Purpose:** Central config object — reads `.env` via `python-dotenv` and exposes every
setting as a class attribute. All other files import `from config import Config`.

**Key class:** `Config`
- `DHAN_CLIENT_ID`, `DHAN_PIN`, `DHAN_TOTP_SECRET`, `DHAN_MOBILE` — Dhan broker creds
- `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_REDIRECT_URL`, `UPSTOX_MOBILE_NO`,
  `UPSTOX_PIN`, `UPSTOX_TOTP_SECRET` — Upstox creds
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `DATA_PROVIDER` — `"dhan"` (default) or `"upstox"`
- `DATA_DIR`, `TOKEN_DIR`, `SIGNALS_DIR`, `LOGS_DIR` — computed `Path` objects
- `TOKEN_FILE` = `data/tokens/access_token.json`
- `UPSTOX_TOKEN_FILE` = `data/tokens/upstox_access_token.json`
- `ensure_dirs()` — creates all required directories

**Imports:** `os`, `dotenv`, `pathlib`

---

#### `iv_store.py`
**Purpose:** The single read/write layer for `iv_history.db`. No strategy file should
write to this DB directly — they all call functions here.

**Key functions:**
- `init_db()` — creates table + index; idempotent
- `save_snapshot(*, security_id, symbol, timestamp, spot_price, atm_strike, atm_iv, ...)` → `bool`
  Inserts one IV row; `ON CONFLICT DO NOTHING` for duplicate `(security_id, timestamp, data_type)`
- `get_latest_snapshot(security_id)` → `dict` — most recent `intraday` row
- `get_iv_history(security_id, days=252)` → `list[float]` — daily ATM IV series (for IV Rank / Percentile)
- `get_bulk_latest_snapshots(security_ids)` → `dict[int, dict]` — one SQL for all IDs at once
- `get_eod_stats(date_str)` → `dict` — aggregated counts for EOD Telegram summary
- `daily_snapshot_exists_today(security_id)` → `bool`
- `_ensure_optional_columns(cursor)` — schema migration: adds new columns if absent

**DB_PATH:** `data/iv_history.db`

**Imports:** `sqlite3`, `pandas`, `logging`, `datetime`, `pathlib`

---

#### `token_manager.py`
**Purpose:** Manages Dhan access tokens using TOTP-based login. Persists token to
`data/tokens/access_token.json` with an `expires_at` field (midnight of current day).

**Key class:** `TokenManager`
- `get_valid_token(force_refresh=False)` → `str` — public entry point
- `refresh_if_needed(force_refresh=False)` — loads saved token; skips refresh if valid
- `_is_token_valid(token_data, min_remaining_seconds=300)` → `bool`
- `_generate_new_token()` → `str` — calls `DhanLogin.generate_token(pin, totp)`
- `_load_token()` / `_save_token(token_data)` — file I/O

**Imports:** `json`, `pyotp`, `datetime`, `dhanhq.DhanLogin`, `config.Config`

---

#### `upstox_token_manager.py`
**Purpose:** Manages Upstox OAuth tokens. Uses a file-lock (`data/tokens/upstox_token.lock`)
to prevent multiple containers from refreshing simultaneously at startup.

**Key functions / class:** `UpstoxTokenManager`
- `get_valid_token()` → `str`
- `refresh_token()` — uses Selenium to automate the Upstox login flow (TOTP)
- Token stored in `data/tokens/upstox_access_token.json` as `{access_token, fetched_at}`

**Imports:** `selenium`, `pyotp`, `json`, `fcntl` (file lock), `config.Config`

---

#### `upstox_adapter.py`
**Purpose:** `UpstoxDhanAdapter` — wraps the Upstox Python SDK so strategy code that
was written against the Dhan interface works unchanged when `DATA_PROVIDER=upstox`.

**Key class:** `UpstoxDhanAdapter`
- Mirrors Dhan SDK method signatures: `get_intraday_data(security_id, interval, ...)`,
  `get_option_chain(security_id, ...)`, etc.
- Internally translates Dhan `security_id` → Upstox `instrument_key` via
  `instrument_mapper.py` + `data/complete.db`

**Imports:** `upstox_python_sdk`, `instrument_mapper`, `upstox_token_manager`, `config`

---

#### `instrument_mapper.py`
**Purpose:** Bidirectional mapping between Dhan `security_id` integers and Upstox
`instrument_key` strings (e.g., `"NSE_EQ|INE002A01018"`).

**Key functions:**
- `get_upstox_instrument_key(security_id)` → `str | None`
- `get_dhan_security_id(instrument_key)` → `int | None`
- Reads from `data/complete.db` (Upstox instrument directory)

**Imports:** `sqlite3`, `config`

---

#### `f_o_stocks_list.py`
**Purpose:** Downloads and caches the NSE F&O contract file (`NSE_FO_contract_DDMMYYYY.csv.gz`).
Provides the authoritative list of F&O eligible stocks for the current expiry.

**Key functions:**
- `get_fno_symbols(force_refresh=False)` → `list[str]` — symbol names
- `_fetch_and_cache_contract_file(date)` — downloads from NSE, stores in `data/fno_cache/`
- Cache is per-day; re-fetched automatically on a new trading day

**Imports:** `requests`, `pandas`, `gzip`, `config`

---

#### `load_scrip_master_sqlite.py`
**Purpose:** Loads Dhan's `api-scrip-master.csv` into `data/api-scrip-master.db`.
One-time + daily refresh. Provides the lot-size + security-id lookup for all strategies.

**Key functions:**
- `load_scrip_master()` — CSV → SQLite (truncate + reload)
- `get_security_id_symbol_map()` → `dict[str, int]` — symbol → security_id
- `get_lot_size(symbol)` → `int`

**Imports:** `sqlite3`, `pandas`, `requests`, `config`

---

#### `trader_logger.py`
**Purpose:** Structured EOD logging — writes two files per day:
- `logs/scan_YYYY-MM-DD.jsonl` — one JSON object per event (signal, order, skip, error)
- `logs/scan_YYYY-MM-DD_summary.txt` — human-readable summary

**Key class:** `TraderLogger`
- `log_event(event_type, symbol, data)` — appends to JSONL
- `write_summary()` — aggregates JSONL → text summary
- Used by `discount.py` for decision audit trail

**Imports:** `json`, `datetime`, `pathlib`

---

### IV Collector Service

#### `iv_collector_service.py`
**Purpose:** Service 1. The sole writer to `iv_history.db`. Runs continuously during
market hours and sweeps all F&O stocks for option-chain data.

**Key class:** `IVCollector`
- `build_eod_watchlist()` — 08:45, scores stocks, builds ordered watchlist for next session
- `run_warmup_pass()` — 09:15–09:50, rapid sweep of all F&O stocks (1.5s sleep/stock)
- `run_intraday_pass()` — 09:50–15:30, full sweep every ~15 min (2s sleep/stock)
- `_fetch_and_save_iv(security_id, symbol)` — fetches option chain via Dhan/Upstox,
  computes ATM IV, calls `iv_store.save_snapshot()`
- `_run_forever()` — main loop

**Schedule constants:**
- `WARMUP_START = dt_time(9, 15)`
- `WARMUP_END   = dt_time(9, 50)`
- `INTRADAY_END = dt_time(15, 30)`
- `EOD_TIME     = dt_time(8, 45)`
- `WARMUP_SLEEP = 1.5` sec, `INTRADAY_SLEEP = 2.0` sec

**Imports:** `discount.DiscountedPremiumScanner`, `discount.unwrap_dhan_payload`,
`iv_store`, `config`, `requests`, `pytz`, `collections`

---

### Strategy 1 — Discounted Premium Scanner

#### `discount.py`
**Purpose:** ~1,400-line monolith. Scans all F&O stocks for options priced at a
statistically significant discount relative to fair value (IV-based). The active
strategy running in the `discount` Docker service.

**Key classes:**
- `DiscountedPremiumScanner` — main scanner
  - `scan_all_fno_stocks(min_discount_score=55)` → `pd.DataFrame` — full sweep
  - `_score_stock(symbol, security_id)` → row dict — per-stock scoring
  - `send_telegram_summary(opportunities)` — Telegram alert
  - `generate_report(opportunities)` — console/log output
- `TraderLogger` (re-exported here)
- Helper functions: `unwrap_dhan_payload(response)`, `get_trading_days_to_expiry(expiry_date)`

**Schedule (via `main.py`):** 09:50, 10:10, 11:30, 12:30, 13:30, 14:30, 15:05, 15:25 (weekdays)

**Output:** `discounted_premiums.csv`, Telegram alerts

**Imports:** `dhanhq`, `upstox_adapter`, `iv_store`, `config`, `f_o_stocks_list`,
`load_scrip_master_sqlite`, `pandas`, `numpy`, `scipy`, `pytz`

---

### Strategy 2 — Directional IV Scanner (discontinued)

#### `directional_iv_strategy.py`
**Purpose:** `DirectionalIVScanner` — scores stocks by a composite of IV Rank, trend
alignment (EMA stack), delta, DTE, and liquidity. Outputs CSV + Telegram alerts.
Lower maturity than other strategies; included in `main.py` but not in default compose.

**Key class:** `DirectionalIVScanner`
- `scan(universe_size)` → `pd.DataFrame`
- `_score_stock(symbol, security_id)` → dict
- Alert threshold: score ≥ `TELEGRAM_ALERT_THRESHOLD` (default 75)

**Config:** `directional_iv_config.py`

**Imports:** `iv_store`, `config`, `upstox_adapter`, `pandas`, `numpy`

#### `directional_iv_runner.py`
**Purpose:** Thin wrapper; exposes `run_directional_scan()` which `main.py` calls on its schedule.

---

### Strategy 3 — Momentum ORB/VWAP (discontinued)

#### `momentum_strategy.py`
**Purpose:** ~800 lines. Two signal types (ORB breakout, VWAP reclaim/break) on 15-min candles,
with regime filter (EMA20/50, ADX) and affordability gate. Now disabled in compose.

**Key classes:**
- `ScripMasterLotSizer` (lines 39–150) — lot-size lookup from `api-scrip-master.db`;
  reused by Break & Bounce
- `MomentumRegimeFilter` — EMA20/EMA50/ADX check on daily candles
- `MomentumRiskManager` — tracks open P&L, daily loss limit, position count
- `MomentumScanner`
  - `check_orb_signal(symbol, security_id)` (lines 480–525)
  - `check_vwap_signal(symbol, security_id)` (lines 527–570)
  - `get_intraday_candles(security_id, interval)` (lines 452–454) — includes VWAP calc
- `MomentumSignalRanker` — scores signals (+40/+20 regime, +30 direction, +10 ORB, +5 vol)
- `MomentumTradeJournal` — per-trade CSV log
- `MomentumStrategyRunner` — orchestrates premarket + intraday scans
- `MomentumTelegramNotifier` — alerts

**Config:** `momentum_config.py`

**Imports:** `iv_store`, `config`, `upstox_adapter`, `f_o_stocks_list`,
`load_scrip_master_sqlite`, `pandas`, `numpy`, `scipy`

---

### Strategy 4 — Break & Bounce (ACTIVE)

#### `break_bounce_strategy.py`
**Purpose:** Implements the three-step B&B entry: yesterday's H/L levels →
15-min breakout candle → 5-min retest with candlestick pattern.

**Key classes:**
- `BreakBounceRiskManager`
  - Tracks `capital`, `daily_pnl`, `open_positions`, `trades_today`
  - `can_take_trade()` → `bool` (checks daily limit + max positions)
  - `calc_lots(premium, spot)` → `int`
- `BreakBounceScanner`
  - `get_yesterday_levels(security_id)` (lines 205–225) → `{yesterday_high, yesterday_low}`
  - `check_15min_breakout(security_id, state)` (lines 229–298) → `"BULLISH" | "BEARISH" | None`
    - Window: 09:15–11:45; closes the setup after `BB_BREAKOUT["window_end_hour:min"]`
  - `check_5min_entry(security_id, direction, state)` (lines 302–403) → signal dict or `None`
    - Patterns: Hammer (lower wick ≥ 2× body, preceded by ≥2 red candles) or
      Bullish Engulfing (`curr.low < prev.low AND curr.high > prev.high AND curr green`)
    - Entry: `last.close` (hammer) or `prev.high` (engulfing)
    - SL: `last.low`; Target: `entry + (sl_dist × BB_RISK["target_ratio"])`
    - Retest expires `BB_BREAKOUT["retest_expiry_minutes"]` (60) minutes after breakout
  - `get_option_details(symbol, direction, spot)` → strike, premium, OI, spread
- `BreakBounceTelegramNotifier`
  - `send_breakout_alert(symbol, direction, level)` — step 2 notification
  - `send_entry_alert(signal_dict)` — step 3 entry notification
  - `send_daily_summary(stats)` — EOD
- `BreakBounceStrategyRunner`
  - `run_premarket()` — loads yesterday levels for all F&O stocks, runs affordability filter
  - `run_intraday_scan()` — 5-min tick:
    - Stocks without breakout → `check_15min_breakout()`
    - Stocks with confirmed breakout → `check_5min_entry()`
  - `run_eod()` — force-exit positions, log summary, reset state dict

**Per-stock state dict:**
```python
{
  "yesterday_high": float,
  "yesterday_low":  float,
  "breakout_dir":   "BULLISH" | "BEARISH" | None,
  "breakout_time":  datetime | None,
  "trade_placed":   bool,
  "entry_price":    float | None,
  "sl":             float | None,
  "target":         float | None,
  "lots":           int,
}
```

**Config:** `break_bounce_config.py`

**Imports:** `discount.DiscountedPremiumScanner`, `discount.unwrap_dhan_payload`,
`momentum_strategy.ScripMasterLotSizer`, `iv_store`, `config`, `pandas`, `numpy`, `pytz`

---

#### `break_bounce_config.py`
```python
CAPITAL        = 200_000           # Override via BB_CAPITAL env var
BB_RISK        = { max_risk_pct: 0.02, sl_pct: 0.30, target_ratio: 2.5,
                   daily_loss_limit_pct: 0.03, max_trades_per_day: 3,
                   max_open_positions: 2 }
BB_BREAKOUT    = { window_end_hour: 11, window_end_min: 45, retest_tol_pct: 0.003,
                   hammer_wick_ratio: 2.0, max_counter_wick: 0.5,
                   force_exit_hour: 15, force_exit_min: 15,
                   retest_expiry_minutes: 60 }
BB_LIQUIDITY   = { min_oi: 500, min_volume: 200, max_spread_pct: 0.05 }
BB_STRIKE      = { otm_offset: 0 }  # ATM
SCRIP_MASTER_DB = "data/api-scrip-master.db"
IV_HISTORY_DB   = "data/iv_history.db"
TRADE_LOG_PATH  = "data/break_bounce_trades.csv"
LOT_SIZE_FALLBACK = { PPLPHARMA: 1800, TORNTPOWER: 750, TATATECH: 475,
                      HUDCO: 2000, NIFTY: 75, BANKNIFTY: 30, FINNIFTY: 65,
                      MIDCPNIFTY: 75 }
```

---

#### `break_bounce_runner.py`
**Purpose:** Service entry point. Registers all schedule jobs and runs `schedule.run_pending()`
every 15 seconds.

**Schedule registration:**
```python
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
INTRADAY_TIMES = [every 5 min from "09:15" to "14:30"]   # ~63 time slots
# Per day:
schedule.every().monday.at("09:00").do(_premarket)
schedule.every().monday.at("15:15").do(_eod)
for t in INTRADAY_TIMES:
    schedule.every().monday.at(t).do(_intraday)
```

**Mid-session catch-up:** If service starts between 09:00–14:30 on a weekday, runs
`_premarket()` immediately.

---

### Scheduler / Main Entry Points

#### `main.py`
**Purpose:** Scheduler for the `discount` Docker service. Runs `DiscountedPremiumScanner`
and `run_directional_scan()` on fixed times.

**Key class:** `StrategySchedulerApp`
- `run_discount_scan()` — instantiates `DiscountedPremiumScanner`, calls
  `scan_all_fno_stocks(min_discount_score=55)`, saves CSV, sends Telegram
- `run_directional_iv_scan()` — calls `directional_iv_runner.run_directional_scan()`
- `setup_schedule()` / `run(run_now, exit_after_run)`

**Discount scan times:** `09:50, 10:10, 11:30, 12:30, 13:30, 14:30, 15:05, 15:25`
**Directional IV times:** `09:45, 11:15, 13:15, 14:45, 15:05`

**CLI flags:** `--run-now` (scan immediately then loop), `--once` (scan once then exit)

---

### Utility / One-shot Scripts

#### `deals.py`
**Purpose:** Standalone NSE bulk/block/short deals scraper. Not part of any service;
run manually to inspect large institutional deals.
- GETs `https://www.nseindia.com/api/snapshot-capital-market-largedeal`
- Prints `BULK_DEALS`, `BLOCK_DEALS`, `SHORT_DEALS` counts + sample rows
- No persistent output; terminal only

#### `init_upstox_token.py`
**Purpose:** Called by `entrypoint.sh` at container startup. Ensures a valid Upstox
token exists before the strategy service starts. Uses `UpstoxTokenManager`.

#### `complete_json_tosqlite.py`
**Purpose:** One-off migration — converts Upstox's JSON instrument directory to
`data/complete.db`. Run once; not in any schedule.

#### `test_expiry_fetch.py`
**Purpose:** Dev utility — tests that expiry date fetching from Dhan API works correctly.

---

## 4. Database Schema

### `data/iv_history.db`

**Table: `iv_history`**

| Column               | Type    | Notes |
|----------------------|---------|-------|
| `id`                 | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `security_id`        | TEXT    | NOT NULL — Dhan security_id |
| `symbol`             | TEXT    | e.g. `"RELIANCE"` |
| `timestamp`          | DATETIME| NOT NULL |
| `spot_price`         | REAL    | Underlying price |
| `atm_strike`         | REAL    | ATM strike at snapshot time |
| `atm_iv`             | REAL    | Average (call+put) IV at ATM |
| `atm_call_iv`        | REAL    | Call IV at ATM |
| `atm_put_iv`         | REAL    | Put IV at ATM |
| `atm_call_oi`        | REAL    | Call OI at ATM |
| `atm_put_oi`         | REAL    | Put OI at ATM |
| `total_call_oi`      | REAL    | Sum of all call strikes OI |
| `total_put_oi`       | REAL    | Sum of all put strikes OI |
| `total_call_volume`  | REAL    | Sum of all call strikes volume |
| `total_put_volume`   | REAL    | Sum of all put strikes volume |
| `max_oi_strike_call` | REAL    | Strike with highest call OI |
| `max_oi_strike_put`  | REAL    | Strike with highest put OI |
| `data_type`          | TEXT    | `"daily"` or `"intraday"` (default `"daily"`) |

**Unique constraint:** `(security_id, timestamp, data_type)`

**Index:** `idx_iv_security_time` on `(security_id, timestamp)`

**Notes on schema evolution:** New optional columns are added via `_ensure_optional_columns()`
on every `init_db()` call — no separate migration scripts exist.

---

### `data/api-scrip-master.db`

Loaded from Dhan's CSV (`api-scrip-master.csv`). Contains all tradeable instruments
on Dhan. Used by `ScripMasterLotSizer` to look up `lot_size` and `security_id` for
F&O stocks.

Key columns (from Dhan's CSV schema): `SEM_SMST_SECURITY_ID`, `SEM_TRADING_SYMBOL`,
`SEM_LOT_UNITS`, `SEM_INSTRUMENT_NAME`, `SEM_EXPIRY_DATE`, `SM_SYMBOL_NAME`.

Table name: typically `scrip_master` (loaded by `load_scrip_master_sqlite.py`).

---

### `data/complete.db`

Loaded from Upstox's full instrument JSON. Contains `instrument_key` strings used to
call the Upstox API. Used by `instrument_mapper.py`.

Primary mapping columns: `instrument_key`, `trading_symbol`, `exchange`, `lot_size`.

---

### `data/break_bounce_trades.csv`

Flat CSV trade journal. Written by `BreakBounceStrategyRunner` after each executed trade.

Columns (inferred): `date`, `symbol`, `direction`, `entry_price`, `sl`, `target`,
`lots`, `entry_time`, `exit_price`, `exit_time`, `pnl`

---

## 5. Scheduler / Cron Jobs

All scheduling uses the `schedule` library (Python), not system cron.
Each service runs `schedule.run_pending()` in a `while True: ... time.sleep(N)` loop.

### `iv_collector_service.py` — continuous loop (no `schedule` library)

| Time          | Action |
|---------------|--------|
| 08:45         | `build_eod_watchlist()` — scores F&O stocks, stores ordered list |
| 09:15–09:50   | Warmup pass — sweeps all stocks, 1.5s sleep between each |
| 09:50–15:30   | Intraday pass — full sweep every ~15 min, 2s between stocks |

Timing logic: `while True` with `datetime.now(IST).time()` checks.

---

### `break_bounce_runner.py` — `schedule` library, `time.sleep(15)`

| Time          | Action |
|---------------|--------|
| Mon–Fri 09:00 | `runner.run_premarket()` |
| Mon–Fri 09:15–14:30, every 5 min (63 slots) | `runner.run_intraday_scan()` |
| Mon–Fri 15:15 | `runner.run_eod()` |

---

### `momentum_runner.py` — `schedule` library, `time.sleep(15)`

| Time          | Action |
|---------------|--------|
| Mon–Fri 09:00 | `runner.run_premarket()` |
| Mon–Fri 09:30–11:30, every 5 min (25 slots) | `runner.run_intraday_scan()` |
| Mon–Fri 15:15 | `runner._notifier.send_daily_summary()` + `risk_manager.reset_daily()` |

---

### `main.py` (discount + directional IV) — `schedule` library, `time.sleep(30)`

| Time          | Strategy |
|---------------|----------|
| Mon–Fri 09:50, 10:10, 11:30, 12:30, 13:30, 14:30, 15:05, 15:25 | `run_discount_scan()` |
| Mon–Fri 09:45, 11:15, 13:15, 14:45, 15:05 | `run_directional_iv_scan()` |

---

## 6. Configuration

### Environment Variables (`.env`)

| Variable               | Used by | Purpose |
|------------------------|---------|---------|
| `DHAN_CLIENT_ID`       | `config.py` | Dhan broker client ID |
| `DHAN_PIN`             | `token_manager.py` | Dhan login PIN |
| `DHAN_TOTP_SECRET`     | `token_manager.py` | TOTP seed for Dhan 2FA |
| `DHAN_MOBILE`          | `config.py` | Dhan mobile number |
| `UPSTOX_API_KEY`       | `upstox_token_manager.py` | Upstox app key |
| `UPSTOX_API_SECRET`    | `upstox_token_manager.py` | Upstox app secret |
| `UPSTOX_REDIRECT_URL`  | `config.py` | OAuth redirect URL |
| `UPSTOX_MOBILE_NO`     | `upstox_token_manager.py` | Upstox login mobile |
| `UPSTOX_PIN`           | `upstox_token_manager.py` | Upstox login PIN |
| `UPSTOX_TOTP_SECRET`   | `upstox_token_manager.py` | TOTP seed for Upstox 2FA |
| `TELEGRAM_BOT_TOKEN`   | `config.py` | Telegram bot token |
| `TELEGRAM_CHAT_ID`     | `config.py` | Telegram group chat ID |
| `DATA_PROVIDER`        | `config.py` | `"dhan"` or `"upstox"` |
| `AUTO_EXECUTE`         | Strategy runners | `"true"` to place real orders |
| `BB_DEBUG`             | `break_bounce_runner.py` | Set `"true"` for per-stock API debug logs |
| `BB_CAPITAL`           | `break_bounce_config.py` | Override capital (default 200,000) |
| `MOMENTUM_CAPITAL`     | `momentum_config.py` | Override capital (default 200,000) |
| `DIRECTIONAL_IV_CAPITAL` | `directional_iv_config.py` | Override capital |
| `DIRECTIONAL_IV_UNIVERSE_SIZE` | `directional_iv_config.py` | How many stocks to scan |
| `DIRECTIONAL_IV_TELEGRAM_ALERT_THRESHOLD` | `directional_iv_config.py` | Min score to alert |
| `APP_TIMEZONE`         | all runners | Force TZ (default `"Asia/Kolkata"`) |
| `APP_BASE_DIR`         | `config.py` | Override base directory path |
| `HTF_INTERVAL`         | `config.py` | Higher timeframe candle interval (default 60) |
| `LTF_INTERVAL`         | `config.py` | Lower timeframe candle interval (default 15) |
| `LOOKBACK_DAYS`        | `config.py` | Daily candle lookback (default 10) |
| `MAX_SYMBOLS_PER_SCAN` | `config.py` | Cap on symbols per scan (default 100) |

### Strategy Config Files (hardcoded defaults, env overrides where noted)

| File | Key Settings |
|------|-------------|
| `break_bounce_config.py` | `CAPITAL=200k`, `sl_pct=30%`, `target_ratio=2.5×`, `retest_tol_pct=0.3%`, `max_trades_per_day=3` |
| `momentum_config.py` | `CAPITAL=200k`, `sl_pct=30%`, `T1=1.8×`, `T2=3.0×`, `adx_min=25`, `vix_max=22` |
| `directional_iv_config.py` | `CAPITAL` from env, `max_atm_iv=45%`, `min_delta=0.18`, `max_delta=0.40`, `min_dte=7`, `max_dte=35` |

---

## 7. Data Flow

### Flow 1 — IV Data Collection → Storage

```
NSE F&O Stock List  ──┐
                       ▼
               iv_collector_service.py
               IVCollector._fetch_and_save_iv()
                       │
               ┌───────▼───────┐
               │ Dhan/Upstox   │  GET option chain for each stock
               │ Option Chain  │  (via DiscountedPremiumScanner._get_chain / upstox_adapter)
               └───────┬───────┘
                       │  ATM strike, call IV, put IV, OI, volume
                       ▼
               iv_store.save_snapshot()
                       │
                       ▼
               data/iv_history.db   (shared Docker volume)
```

### Flow 2 — Break & Bounce Strategy

```
09:00  data/iv_history.db ──→ affordability filter ──→ stock watchlist (in-memory)
       api-scrip-master.db ──→ ScripMasterLotSizer ──→ lot sizes
       Dhan API (daily candles) ──→ yesterday_high, yesterday_low per stock

09:15–11:45  (every 5 min)
       Dhan API (15-min candles) ──→ check_15min_breakout()
       if close > yesterday_high → BULLISH breakout confirmed
       if close < yesterday_low  → BEARISH breakout confirmed

After breakout (every 5 min, up to 14:30 or +60 min)
       Dhan API (5-min candles) ──→ check_5min_entry()
       if Hammer or Engulfing near level → signal dict

Signal → BreakBounceRiskManager.can_take_trade()
       → if AUTO_EXECUTE: place market BUY + SL-M SELL (Dhan)
       → Telegram alert (always)
       → data/break_bounce_trades.csv (trade log)

15:15  force-exit all open positions → EOD Telegram summary → reset state
```

### Flow 3 — Discounted Premium Scanner

```
(8 times per day, 09:50–15:25)
F&O stock list ──→ for each stock:
    iv_store.get_latest_snapshot() ──→ ATM IV, spot
    Dhan option chain ──→ available premiums at various strikes
    Compute: fair_value_premium = f(atm_iv, dte, spot, strike)
    Compute: discount_pct = (fair_value - market_price) / fair_value
    Score: composite of discount_pct, liquidity, affordability
    Filter: score ≥ 55

Top opportunities ──→ discounted_premiums.csv
                 ──→ Telegram alerts
                 ──→ TraderLogger JSONL + summary
```

### Flow 4 — Token Lifecycle

```
Container startup:
  entrypoint.sh ──→ python init_upstox_token.py
                 ──→ UpstoxTokenManager.get_valid_token()
                     ├─ token file exists + valid → reuse
                     └─ else: Selenium login → TOTP → store upstox_access_token.json

During runtime (strategy needs API call):
  TokenManager.get_valid_token()
  ├─ access_token.json valid → return cached token
  └─ expired/missing → DhanLogin.generate_token(pin, totp) → save → return
```

---

## 8. Docker Setup

### `docker-compose.yml` (Production — default `docker-compose up`)

**Shared volume:** `shared-data` — mounted at `/app/data` in all containers.
Contains `iv_history.db`, `api-scrip-master.db`, `complete.db`, tokens, logs.

| Service | Container Name | Dockerfile | Command | Profile |
|---------|---------------|------------|---------|---------|
| `iv-collector` | `iv-collector` | `Dockerfile.iv-collector` | `python iv_collector_service.py` | (always on) |
| `discount` | `discount-strategy` | `Dockerfile.discount` | `python main.py` | (always on) |
| `break-bounce` | `break-bounce-strategy` | `Dockerfile.break-bounce` | `python break_bounce_runner.py` | (always on) |
| `momentum` | `momentum-strategy` | `Dockerfile.momentum` | `python momentum_runner.py` | `momentum` |
| `directional-iv` | `directional-iv-strategy` | `Dockerfile.directional` | `python directional_iv_runner.py` | `directional-iv` |

**Dependencies:** `discount`, `break-bounce`, `momentum`, `directional-iv` all have
`depends_on: iv-collector`.

**Environment (all services):**
```yaml
env_file: .env
environment:
  TZ: Asia/Kolkata
  APP_TIMEZONE: Asia/Kolkata
```

**Volumes (all services):**
```yaml
volumes:
  - shared-data:/app/data
  - ./logs:/app/logs
```

### Dockerfiles — common pattern

All Dockerfiles (`Dockerfile.*`) follow the same structure:
- Base: `python:3.11-slim`
- System packages: `git`, `tzdata`, `chromium`, `chromium-driver` (for Selenium Upstox login)
- Install `requirements.txt`
- Create `/app/data/tokens`, `/app/data/signals`, `/app/logs` with `chmod 777`
- `ENTRYPOINT ["/app/entrypoint.sh"]`

### `entrypoint.sh`
```bash
#!/bin/bash
set -e
python /app/init_upstox_token.py   # init Upstox token before service starts
exec "$@"                           # hand off to CMD (python <runner>.py)
```

### Starting Services

```bash
# Default (iv-collector + discount + break-bounce):
docker-compose up

# Include momentum strategy:
docker-compose --profile momentum up

# Include directional IV strategy:
docker-compose --profile directional-iv up

# All services:
docker-compose --profile momentum --profile directional-iv up
```

---

## 9. Current State

### What is Working

- **IV Collector** — production-ready; runs continuously, writes to `iv_history.db`
- **Break & Bounce** — active strategy; 3-step logic fully implemented;
  alert + order modes both functional; per-stock state management works
- **Discounted Premium Scanner** (`discount.py`) — active strategy; scheduled scans
  run 8×/day; Telegram alerts working
- **Token management** — both Dhan (`token_manager.py`) and Upstox
  (`upstox_token_manager.py`) handle refresh with TOTP; file-locking prevents
  multi-container races
- **Broker abstraction** — `upstox_adapter.py` + `instrument_mapper.py` allow switching
  between Dhan and Upstox via `DATA_PROVIDER` env var without code changes
- **Docker setup** — all 5 services have Dockerfiles; compose file correctly uses
  profiles for discontinued strategies

### What is Discontinued / Paused

- **Momentum (ORB/VWAP)** — complete implementation exists but excluded from default
  compose via `profiles: [momentum]`. Disabled because B&B superseded it.
- **Directional IV** — code exists and is included in `main.py`'s schedule, but the
  strategy is less battle-tested. Its runner is bundled inside the `discount` container.

### Known Issues / TODOs

- **`discount.py` command mismatch:** `Dockerfile.discount` calls `python main.py --auto-loop`
  but `main.py` does not accept `--auto-loop` (it accepts `--run-now` and `--once`). At
  container start the flag is silently ignored (argparse does not error on unknown flags
  in this version) but this should be cleaned up.
- **`momentum_config.py` IV_HISTORY_DB path:** Set to `"iv_history.db"` (relative, no
  `data/` prefix) unlike all other configs which use `"data/iv_history.db"`. This works
  inside a container because WORKDIR is `/app` and the volume is `/app/data`, but will
  fail if run locally without the Docker volume path match.
- **No order-placement code in `break_bounce_strategy.py` is visible in docs** — confirm
  `AUTO_EXECUTE` path and SL-M SELL fallback are covered in `BreakBounceStrategyRunner`
  or the runner.
- **`deals.py`** has hardcoded NSE session setup at module import time — running it
  inside any service that imports from the root will trigger live HTTP calls. It is
  not imported anywhere currently, but should be isolated further.
- **`iv_history.db` schema migration** — `_ensure_optional_columns()` is called on
  every connection in `get_latest_snapshot()` and `save_snapshot()`. Under high-frequency
  writes this adds unnecessary overhead. Should be moved to `init_db()` only.
- **No test suite** — no unit or integration tests exist. All verification is manual
  or via `test_expiry_fetch.py` (single utility script).
- **`data/fno_cache/` grows unbounded** — old daily contract files are never deleted.

---

## 10. Dependencies

### `requirements.txt`

```
git+https://github.com/dhan-oss/DhanHQ-py.git@06c830c4f5a7593ede3deeabbf203debd8632826
python-dotenv
schedule
pandas
numpy
scipy
requests
pyotp
pytz
upstox-python-sdk
selenium
```

### Key Libraries and Why

| Library | Version pin | Used for |
|---------|-------------|----------|
| `dhanhq` (from git) | commit `06c830c` | Dhan broker SDK — order placement, option chain, candles |
| `upstox-python-sdk` | latest | Upstox broker SDK — candles, option chain, market data |
| `python-dotenv` | latest | Load `.env` → `os.environ` |
| `schedule` | latest | Pure-Python cron scheduler (`schedule.every().monday.at("09:00").do(fn)`) |
| `pandas` | latest | DataFrame for scan results, candle processing, IV history |
| `numpy` | latest | Array math (VWAP, EMA, ATM IV calculations) |
| `scipy` | latest | Statistical functions (IV percentile, normal distribution for fair value) |
| `requests` | latest | HTTP calls to NSE, Dhan REST endpoints not in SDK |
| `pyotp` | latest | TOTP generation for broker 2FA (DHAN_TOTP_SECRET, UPSTOX_TOTP_SECRET) |
| `pytz` | latest | IST timezone (`Asia/Kolkata`) for all time comparisons |
| `selenium` | latest | Headless Chromium for Upstox OAuth login automation |

### System Dependencies (installed in all Dockerfiles)

| Package | Purpose |
|---------|---------|
| `chromium` | Headless browser for Upstox Selenium login |
| `chromium-driver` | WebDriver for chromium |
| `tzdata` | Timezone data for `TZ=Asia/Kolkata` |
| `git` | Required to install `dhanhq` from git URL |

---

*End of PROJECT_CONTEXT.md*
