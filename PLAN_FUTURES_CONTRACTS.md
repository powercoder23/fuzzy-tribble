# Plan: Futures Contracts Integration

**Date:** 2026-07-29  
**Status:** Planning

---

## Why Futures?

Futures data provides signals that option chains alone cannot:

| Use Case | How Futures Help |
|---|---|
| Basis / carry | Futures premium over spot signals institutional directional conviction |
| Rollover OI | Rising OI near expiry = accumulation; falling = unwinding |
| Cost-of-carry trend | Rising carry → bullish posture; collapsing carry → bearish or hedge |
| Price confirmation | Futures leading spot by 1–2 ticks → legit breakout (not noise) |
| Hedge routing (future execution) | Hedge via futures instead of options short-leg when premium is too thin |

---

## Data Architecture

### Source
All data via **Upstox** (`upstox_adapter.UpstoxDhanAdapter`) — same adapter already used for option chains.

Relevant Upstox endpoints:
- `get_option_chain` already returns the underlying futures quote in some responses
- `get_historical_candle_data` works for futures symbols (e.g. `NSE_FO|RELIANCE26AUGFUT`)
- `get_market_quote` for live futures LTP + OI

### Scrip Master
`data/api-scrip-master.db` already contains F&O scrip tokens including futures contracts. Need to query:
```sql
SELECT security_id, trading_symbol, lot_size, expiry_date
FROM   securities
WHERE  instrument_type = 'FUTSTK'  -- or FUTIDX for index futures
ORDER  BY expiry_date ASC
```

### Storage
Add a `futures_snapshots` table to `iv_history.db` (same WAL SQLite, sole-writer via iv_store):

```sql
CREATE TABLE IF NOT EXISTS futures_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT    NOT NULL,
    security_id    TEXT    NOT NULL,
    expiry         TEXT    NOT NULL,           -- YYYY-MM-DD
    spot           REAL,
    futures_price  REAL,
    basis          REAL,                       -- futures_price - spot
    basis_pct      REAL,                       -- basis / spot * 100
    oi             INTEGER,
    oi_change      INTEGER,                    -- vs prior snapshot
    volume         INTEGER,
    recorded_at    TEXT    NOT NULL,           -- ISO datetime IST
    date           TEXT    NOT NULL            -- YYYY-MM-DD IST
);
CREATE INDEX IF NOT EXISTS idx_fs_sym_date ON futures_snapshots(symbol, date);
```

---

## New Service: futures-collector

Mirrors `iv-collector` but for futures data.

**File:** `futures_collector_service.py`  
**Container:** `futures-collector`  
**Cadence:** Every 15 min during market hours (09:15–15:30 IST)  
**Sole writer** of `futures_snapshots` rows.

### Core Logic

```python
def snapshot_futures(security_ids: list[str], now: datetime) -> None:
    for sid in security_ids:
        quote = adapter.get_market_quote(sid)   # LTP, OI, volume
        spot  = adapter.get_ltp(underlying_sid) # underlying spot
        basis = quote["ltp"] - spot
        store.save_futures_snapshot(symbol, sid, expiry, spot,
                                    quote["ltp"], basis, quote["oi"],
                                    quote["volume"], now)
```

---

## How Existing Strategies Use Futures Data

### 1. Directional IV — basis confirmation gate
Before booking a Directional IV signal:
- Fetch latest `futures_snapshots` row for the symbol.
- If CE signal: require `basis_pct > 0` (futures in contango → bullish carry).
- If PE signal: require `basis_pct < -0.05` (futures in backwardation → bearish).
- Skip gate if no futures row (fail-open, same pattern as other gates).

### 2. Break & Bounce — breakout legitimacy filter
After a 15-min candle close above yesterday's high:
- Check if futures price also broke above yesterday's futures high (within 0.2% tolerance).
- If spot breaks but futures lags → possible false breakout; downgrade confidence or skip.

### 3. Composite Scanner — OI change feed
Composite conviction engine already fuses OI-buildup signals. Replace or supplement the options OI feed with futures OI change:
- Large positive OI change + rising futures price → strong accumulation.
- Large positive OI change + falling futures price → short buildup (bearish).

### 4. Sonar — basis anomaly detection
Add a basis-spike detector: when `basis_pct` moves > 2σ from its 20-day mean within a session, fire a Telegram alert. Spike up → institutions loading longs; spike down → unwinding.

### 5. Vol Expansion — carry check before entry
For IV buy-zone entries: if the 30-day carry trend is collapsing (basis_pct declining over 5 sessions), skip the vega entry — expensive IV with collapsing carry is a warning sign.

---

## Futures for Risk / Hedging (Future Execution Phase)

When `AUTO_EXECUTE=true` is enabled (real orders):
- Instead of a short OTM option as the hedge leg, route the hedge via a **futures short** (for CE entries) or **futures long** (for PE entries).
- Futures hedge: lower slippage, no time decay on hedge, better liquidity for large lots.
- `hedge.py`: add `HEDGE_MODE=options|futures` config. Default keeps current options hedge; `futures` mode calls `_place_futures_order` instead.

---

## Implementation Phases

| Phase | Deliverable | Effort |
|---|---|---|
| P1 | `futures_collector_service.py` + DB table + docker-compose service | ~4h |
| P2 | Basis gate in Directional IV runner | ~1h |
| P3 | Futures OI feed to Composite scanner | ~2h |
| P4 | Sonar basis anomaly alerts | ~1h |
| P5 | Futures hedge routing for live execution | ~3h |

---

## docker-compose addition (P1)

```yaml
futures-collector:
  build: .
  container_name: futures-collector
  command: python -m futures_collector_service
  volumes:
    - ./data:/app/data
  environment:
    APP_TIMEZONE: "Asia/Kolkata"
  depends_on:
    - iv-collector
  restart: unless-stopped
```
