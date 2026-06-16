# Discount Scanner → Intraday Volatility-Only Paper-Trading System — Plan & Handoff

Repo: github.com/powercoder23/fuzzy-tribble
Working file: `discount.py` (+ `discount_config.py`, `main.py`, new `paper_trader.py`)
Status date: 2026-06-17

---

## 0. Goal (one line)

Turn the discount scanner into an **intraday-only, single-leg, Volatility-Expansion-Play-only**
signal + **paper-trading** system on **Upstox**, with 5-min scans, live per-signal
Telegram alerts, and a realized-P&L EOD summary. No carry-forward.

---

## 1. DONE so far (this session)

**Upstox migration of `discount.py` — COMPLETE and compiles.**

- Imports: `from dhanhq import dhanhq, DhanContext` → `from upstox_adapter import UpstoxDhanAdapter`
  + `from upstox_token_manager import load_upstox_token`.
- `__init__` now: `(hardtoken=None, client_id=None, store_intraday=False, upstox_adapter=None)`;
  auto-builds `self.dhan = UpstoxDhanAdapter(load_upstox_token())` if no adapter injected.
  `self.access_token=None`, `self.context=None`, `self.client_id="upstox"`.
- Why it was needed beyond the broker swap: the earlier revert to `0ec27c5` left the
  constructor requiring `hardtoken`, but **every caller** (`main.py`, `iv_collector_service.py`,
  `break_bounce_strategy.py`, `momentum_strategy.py`) calls `DiscountedPremiumScanner()` with
  no args → was un-runnable. Migration also fixes that.
- Only `self.dhan` methods used in active code: `option_chain`, `expiry_list`,
  `historical_daily_data` — all covered by the adapter. The `fetch_expired_option_data`
  / `rollingoption` path is **dead code** (defined, never called) → ignore.
- Note: handoff-era claim "no upstox string in repo" is **stale**. `upstox_adapter.py`,
  `upstox_login.py`, `upstox_token_manager.py`, `init_upstox_token.py`, and a
  `DATA_PROVIDER` selector in `config.py` already exist; `momentum`/`directional_iv`
  already route through the adapter.

---

## 2. DECIDED SPEC (all confirmed with user)

### Strategy
- **Only "Volatility Expansion Play"** — strip the directional spread branches
  (Call Debit Spread / Bear Put Spread) from `build_strategy_plan` + `classify_trade_type`.
- **Single leg only** (long one option, no short leg — capital-constrained).

### Trade plan (intraday, single-leg premium %)
- Entry = option mid premium.
- **Hard SL = −15%** of entry (would be an SL-M in live; here it's the paper exit trigger).
- **T1 = +25%** → book **70%** of position.
- **T2 = +45%** → trail remaining **30%** to square-off.
- RR at T1 ≈ 1.7:1 (honest for intraday single leg).

### Timing / no carry-forward
- **Scan cadence: every 5 minutes** during the session.
- **No new entries after 14:00.**
- **Forced square-off 15:20** (exit any open paper trade at last price).
- **`MIN_DTE_DAYS = 5`** (user choice; note trade-off below).

### Expiry trade-off (recorded for future tuning)
- DTE 5 = low theta but smaller intraday premium % moves → keep targets modest
  (the +25/+45 above are calibrated for this). If target hits feel too rare,
  the lever is DTE: dropping to 2–3 DTE adds gamma and makes +25/+45 easier intraday.

### Alerts
- **Live per-signal Telegram alert** the moment a play qualifies in a scan cycle
  (granularity = 5-min scan, NOT tick-level). Pro-level formatted, one trade per message.
- **Top 5 by score per day**; **one paper trade per symbol+strike+side per day** (no dup re-alerts).

### Paper trading
- On each alert, open a paper trade.
- **Realistic simulation**: each 5-min scan re-checks the alerted option's **actual LTP**;
  mark whichever happens first in real order — T1 / T2 / SL / 15:20 time-exit.
  Book 70% at T1, trail 30% to T2 (or square-off).
- P&L on **1-lot basis**, reported in **% and ₹**.
- Persist to **`paper_trades.db`** (SQLite) so EOD job can read it back.
- **EOD summary** (~15:25) = realized P&L per trade + aggregate (wins/losses, total ₹, hit-rate).

---

## 3. UPSTOX RATE LIMITS (verified from docs, 2026-06-17)

Source: https://upstox.com/developer/api-documentation/rate-limiting
Enforced **per-API, per-user**. Scanner uses the "Other Standard APIs" bucket
(option chain, historical candles):

| Window | Limit |
|---|---|
| per second | 50 |
| per minute | 500 |
| per 30 min | **2000 (binding constraint)** |

### Feasibility of 5-min full scan — NOT possible as-is, YES after fixes

Universe ≈ 200 F&O names. Per stock per scan today: 1 option-chain + 1 daily-candle
call (expiry cacheable; intraday IV/OI is read from local `iv_history.db`, no API) ≈
**~400 calls/scan**.

Two blockers:
1. **Legacy throttle** `CHAIN_API_MIN_INTERVAL_SEC = 3.1` (Dhan's 1-req/3s). Serialized,
   400 calls = **~20 min/scan** — can't finish in 5 min. Dhan-specific; must relax for Upstox.
2. **30-min cap**: 5-min cadence = 6 scans/30 min × 400 = **2,400 > 2,000** → throttle/suspend.

### Required changes to make 5-min cadence safe
- **Cache daily candles + expiry list once per day** (daily candles don't change intraday).
  → each scan drops to ~1 chain call/stock (~200 calls). 6 × 200 = **1,200/30 min < 2,000** ✓
  (safe even if all standard APIs share one bucket).
- **Relax throttle** to Upstox pace (~6–8 req/s; under 50/s and 500/min).
  → 200 calls finish in **~30–40 s/scan**, fits 5 min with margin.
- *(Optional)* **Trim universe to ~100–150 liquid names** for more headroom + speed.

### iv-collector shared budget — IMPORTANT
`iv_collector_service.py` uses the **same `DiscountedPremiumScanner` class → same Upstox
token → same option-chain 2,000/30-min bucket.** Its continuous polling competes with the
scanner. Mitigation: stagger cadences, or give the two services **separate Upstox apps/tokens**.

---

## 4. TARGET ARCHITECTURE

```
                    ┌─────────────────────────────────────────┐
                    │ main.py  (StrategySchedulerApp)           │
                    │  • scan job  every 5 min 09:30→14:00*     │
                    │  • monitor   every 5 min 09:30→15:20      │
                    │  • EOD job   ~15:25                        │
                    └───────────────┬───────────────────────────┘
                                    │
              ┌─────────────────────┼───────────────────────────┐
              ▼                     ▼                           ▼
   DiscountedPremiumScanner   PaperTradeBook (new)      TelegramAlerts (new fmt)
   (discount.py)              paper_trader.py
   • volatility-only          • open_trade()            • per-signal alert (HTML)
   • intraday T1/T2/SL         • update_open_trades()   • EOD summary
   • daily-candle cache        • simulate T1/T2/SL/TE
   • relaxed throttle          • paper_trades.db
              │
              ▼
   Upstox (UpstoxDhanAdapter) + local iv_history.db (IV/OI, no API)
```
\* entries gated to ≤14:00; monitor keeps running to 15:20 to manage exits.

### Data flow per 5-min cycle
1. **Monitor** open paper trades first: fetch current LTP for each (≤5) open option,
   apply T1/T2/SL/time rules, persist state. (≤5 chain/quote calls.)
2. **Scan** (if ≤14:00): for each universe stock, pull option chain (daily candle from
   cache), run volatility classification + scoring, collect qualifying plays.
3. Rank, take **top 5/day** not already open, send **per-signal alert**, **open paper trade**.
4. At **15:20** force-close remaining; at **15:25** send **EOD summary**.

---

## 5. FILE-BY-FILE CHANGE PLAN

### `discount_config.py`
Replace `TRADE_PLAN` and add intraday block:
```python
TRADE_PLAN = {
    "stop_loss_mult": 0.85,   # -15%
    "t1_mult": 1.25,          # +25%, book 70%
    "t2_mult": 1.45,          # +45%, trail 30%
    "t1_book_fraction": 0.70,
}
INTRADAY = {
    "scan_interval_min": 5,
    "no_entry_after": "14:00",
    "square_off": "15:20",
    "eod_summary_at": "15:25",
    "max_signals_per_day": 5,
}
MIN_DTE_DAYS = 5
# Upstox pacing (replaces Dhan 3.1s throttle)
UPSTOX_MAX_REQ_PER_SEC = 7
CACHE_DAILY_CANDLES = True      # fetch daily candles once/day
LIQUID_UNIVERSE_ONLY = True     # optional ~100–150 name subset (OPEN Q1)
```

### `discount.py`
- `build_strategy_plan`: collapse to volatility-only; always
  `strategy = "Volatility Expansion Play"`, `short_strike=None`; compute
  `entry`, `stop_loss = entry*0.85`, `t1 = entry*1.25`, `t2 = entry*1.45`;
  return `t1`, `t2`, `t1_book_fraction` in the dict (and keep `target` = t1 for
  backward-compat with existing CSV columns).
- `classify_trade_type`: return only `"volatility"` or `None` (drop directional branch),
  OR keep classify but force the downstream plan to volatility. Simplest: in the
  opportunity-assembly path, skip any row whose type isn't volatility.
- **Throttle**: make `_throttle_chain_api` honor `UPSTOX_MAX_REQ_PER_SEC` instead of the
  hard-coded 3.1s; keep a small per-minute guard well under 500.
- **Daily-candle cache**: wrap `fetch_historical_prices` with a per-day in-memory (or
  on-disk) cache keyed by security_id+date so it fires once/day/stock.
- Add a helper `get_current_option_premium(security_id, segment, expiry, strike, side)`
  for the paper monitor to re-price an open trade (reuse `get_option_chain`).

### `main.py`
- `DEFAULT_SCAN_TIMES`: interval **15 → 5**, end **15:15 → 15:20** (monitor window);
  gate new entries at ≤14:00 inside the scan.
- Wire: after scan produces opportunities → `PaperTradeBook.process_signals(top5)` →
  send per-signal alerts.
- Add monitor tick + 15:20 force-close + 15:25 EOD summary jobs.
- Replace the single `send_telegram_summary` digest with per-signal alerts + EOD summary.

### NEW `paper_trader.py`
- `PaperTradeBook` (SQLite `paper_trades.db`): schema
  `id, date, symbol, security_id, side, strike, expiry, entry_premium, sl, t1, t2,
   t1_book_fraction, status(open/closed), opened_at, qty_remaining, realized_pct,
   realized_rupees, exit_reason, closed_at`.
- `open_trade(signal)` — dedup on (date, symbol, strike, side).
- `update_open_trades(scanner, now)` — re-price via `get_current_option_premium`;
  apply T1 (book 70%), T2 (trail/close 30%), SL (−15% full), 15:20 time-exit.
- `eod_summary()` — aggregate realized P&L (% and ₹ @ 1 lot), hit-rate, per-trade lines.
- Lot size from `ScripMasterLotSizer` (already in momentum) for ₹ conversion.

### NEW alert formatter (in `paper_trader.py` or small `alerts.py`)
Pro-level per-signal Telegram (HTML, `parse_mode: "HTML"`). Draft layout:
```
🟢 VOLATILITY EXPANSION • <b>RELIANCE</b> CE 1400  (1-OTM)
Score 78.5 | DTE 5 | Spot 1392
IVR 22 • IV/HV 18.2/14.5 • IV-trend flat • Skew +0.6
Entry ₹24.50  SL ₹20.83 (−15%)
T1 ₹30.63 (+25%, book 70%)   T2 ₹35.53 (+45%, trail)
Liquidity OI 18,400 • Vol 2,100 • Spread 2.1%
Square-off 15:20 • Lot 250 • Risk ≈ ₹918/lot
#paper
```
EOD summary: list each trade with exit_reason + realized %/₹, then totals
(N trades, win-rate, total ₹, best/worst).

---

## 6. OPEN QUESTIONS (answer next session)

1. **Universe**: trim to a liquid subset (~100–150, recommended) or scan all ~200?
   (Affects rate-budget headroom and scan speed.)
2. **iv-collector token**: same Upstox token as the scanner? If yes → add coordinated
   pacing or split into separate Upstox apps so they don't blow the shared 2,000/30-min
   option-chain budget together.

---

## 7. SUGGESTED BUILD ORDER

1. `discount_config.py` — new TRADE_PLAN + INTRADAY + pacing constants.
2. `discount.py` — volatility-only plan + T1/T2 + throttle relax + daily-candle cache
   + `get_current_option_premium`. (Byte-compile + construct test.)
3. `paper_trader.py` — PaperTradeBook + simulation + alert formatter. (Unit-test the
   T1/T2/SL/time-exit state machine with synthetic price paths — no API needed.)
4. `main.py` — 5-min schedule, entry gate ≤14:00, monitor, 15:20 close, 15:25 EOD.
5. End-to-end dry run with a tiny mock universe; verify rate-budget math (≤2000/30min).
6. Docker: existing `Dockerfile.discount` / compose service unchanged except it now
   needs the Upstox token mount (same as other migrated services).

---

## 8. RISK / WATCHLIST
- Shared option-chain budget with iv-collector (Q2).
- 15:20 square-off must be robust even if a scan is mid-flight (idempotent close).
- Dedup so a play re-qualifying every 5 min doesn't open 5 trades.
- DTE-5 may yield few target hits intraday — review after first week; consider DTE 2–3.
- Keep all changes additive/surgical; do not touch momentum / break-bounce / directional paths.
```
