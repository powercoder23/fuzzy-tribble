# Plan: News & Events Crawler with LLM Sentiment Analysis

**Date:** 2026-07-29  
**Status:** Planning

---

## Goal

Build a crawler service that monitors news and corporate events for F&O stocks, uses an LLM to classify sentiment, and feeds the output into the trading system as:
1. **Entry veto** — block new positions before high-impact events (earnings, results, regulatory actions).
2. **Risk alert** — Telegram warning when breaking negative news hits an open position.
3. **Sentiment signal** — directional lean from news can reinforce or contradict the composite conviction engine.

---

## Data Sources

| Source | What it provides | Access |
|---|---|---|
| NSE announcements | Corporate results, board meetings, DRHP filings | `https://www.nseindia.com/companies-listing/corporate-filings-announcements` (scrape or RSS) |
| BSE corporate filings | Same as NSE, often earlier | BSE API / scrape |
| Moneycontrol / ET Markets | Breaking news per ticker | RSS feeds |
| Google News RSS | `site:economictimes.com RELIANCE` style queries | No auth needed |
| NSE calendar | Earnings dates, ex-dividend, record dates | NSE API |
| SEBI orders | Regulatory actions (freeze, suspension) | `https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doRecent=yes` |

**Priority order for MVP:** NSE announcements RSS → Google News RSS per ticker → BSE filings.

---

## Architecture

### Service: news-crawler

**File:** `news_crawler_service.py`  
**Container:** `news-crawler`  
**Cadence:** Poll every 10 min during market hours + pre-market sweep at 08:45 IST.

```
news-crawler (Python service)
    │
    ├── fetch_nse_announcements()     # NSE filing RSS
    ├── fetch_google_news(symbol)     # per-ticker RSS
    ├── fetch_bse_filings(symbol)     # BSE API
    │
    ├── deduplicate (URL + hash)
    ├── classify_sentiment(text)      # LLM call
    │
    └── write → news_events.db (SQLite, WAL)
                └── read by: sonar, composite, order_manager
```

### Storage — `news_events.db`

```sql
CREATE TABLE IF NOT EXISTS news_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    headline     TEXT    NOT NULL,
    url          TEXT    UNIQUE,
    source       TEXT,                  -- 'nse'|'bse'|'google'|'moneycontrol'
    published_at TEXT    NOT NULL,      -- ISO datetime IST
    fetched_at   TEXT    NOT NULL,
    sentiment    TEXT,                  -- 'POSITIVE'|'NEGATIVE'|'NEUTRAL'|'UNKNOWN'
    impact       TEXT,                  -- 'HIGH'|'MEDIUM'|'LOW'
    event_type   TEXT,                  -- 'EARNINGS'|'RESULTS'|'REGULATORY'|'MGMT'|'GENERAL'
    summary      TEXT,                  -- LLM-generated 1-sentence summary
    raw_text     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ne_sym_pub ON news_events(symbol, published_at);
CREATE INDEX IF NOT EXISTS idx_ne_pub     ON news_events(published_at);
```

---

## LLM Integration

### Model Choice
- **Claude Haiku 4.5** (`claude-haiku-4-5-20251001`) via Anthropic API — cheapest, fast enough for batch classification.
- Fallback: keyword-based classifier (no API cost) when Anthropic API is unavailable.

### Prompt Design

```
System: You are a financial news classifier for NSE F&O stocks.

User:
Stock: {symbol}
Headline: {headline}
Body (first 500 chars): {body_snippet}

Classify this news in JSON:
{
  "sentiment": "POSITIVE" | "NEGATIVE" | "NEUTRAL",
  "impact": "HIGH" | "MEDIUM" | "LOW",
  "event_type": "EARNINGS" | "RESULTS" | "REGULATORY" | "MGMT_CHANGE" | "GENERAL",
  "summary": "<one sentence>",
  "entry_block": true | false   // true = block new entries for this stock today
}

Rules:
- EARNINGS/RESULTS with HIGH impact → entry_block: true
- SEBI order / suspension / regulatory → impact: HIGH, entry_block: true
- Promoter pledge / default news → NEGATIVE, HIGH
- New order wins, capex announcements → POSITIVE
- Routine filings → LOW, entry_block: false
```

### Batching
- Classify up to 20 headlines per API call using a list prompt.
- Cache results by URL hash — re-classify only new articles.
- Daily cost estimate: ~150 tickers × 3 articles × 500 tokens ≈ 225K tokens/day ≈ $0.02/day at Haiku rates.

---

## Integration with Trading System

### 1. Pre-trade veto (OrderManager)

Add `NewsGate` check in `submit_external_signal` and `submit_signals`:

```python
from news_gate import news_gate_ok

def submit_external_signal(self, sig, now=None):
    ...
    if not news_gate_ok(sig["symbol"], now):
        logger.info("OrderManager: %s blocked by news gate", sig["symbol"])
        return None
```

`news_gate_ok(symbol, now)` queries `news_events.db`:
- Returns `False` if any row for `symbol` in last 24h has `entry_block=True`.
- Returns `True` otherwise (fail-open — missing DB or no rows = allow).

### 2. Open-position risk alert (paper_trader / monitor)

During `run_monitor_cycle` (every 5 min):
- For each OPEN position, check `news_events` for HIGH-impact NEGATIVE news published since trade open time.
- If found: fire Telegram alert "⚠️ BREAKING: {headline} — review {symbol} position".
- Do NOT auto-close (paper mode — human reviews). Flag in `paper_trades.db` as `news_flagged=1`.

### 3. Sentiment feed to Composite Scanner

`composite_runner.py` already aggregates directional signals. Add a news sentiment column:
- Nightly (after market): run `news_sentiment_daily_summary.py` — aggregates today's news by symbol into `BULLISH`/`BEARISH`/`NEUTRAL` daily conviction.
- Store in `composite_history` as a new `news_conviction` column.
- Composite score: `+15` for BULLISH news, `-15` for BEARISH, `0` for NEUTRAL/UNKNOWN.

### 4. Sonar veto

`sonar_laplace_runner.py` already issues risk warnings. Add:
- If HIGH-impact news (any sentiment) in last 4h for a stock → sonar emits a RISK WARNING regardless of IV/statistical signals.

---

## Event Calendar (Earnings / Results Dates)

Separate from news crawler — structured calendar data:

**File:** `events_calendar_service.py`  
**Source:** NSE corporate calendar API  
**Storage:** `events_calendar` table in `news_events.db`

```sql
CREATE TABLE IF NOT EXISTS events_calendar (
    symbol         TEXT NOT NULL,
    event_type     TEXT NOT NULL,    -- 'BOARD_MEETING'|'RESULTS'|'AGM'|'EX_DIVIDEND'
    event_date     TEXT NOT NULL,    -- YYYY-MM-DD
    description    TEXT,
    scraped_at     TEXT NOT NULL,
    PRIMARY KEY (symbol, event_type, event_date)
);
```

**Auto-veto rule:** Block new entries within `N_DAYS_BEFORE_EVENT` (default 1) of a board meeting or results date. Configurable per strategy:
- Directional IV: block 2 days before results (IV already spiking — buying after the spike is late).
- Break & Bounce: block 1 day before results (gap risk).
- Vol Expansion: do NOT block — IV spike IS the signal; but reduce sizing by 50% as guard.

---

## Keyword Fallback Classifier

When LLM API unavailable:

```python
NEGATIVE_KEYWORDS = ["sebi order", "suspension", "default", "pledge", "fraud",
                     "penalty", "downgrade", "sell rating", "loss", "debt trap"]
POSITIVE_KEYWORDS = ["order win", "buyback", "dividend", "upgrade", "profit",
                     "expansion", "new client", "capex"]
HIGH_KEYWORDS     = ["results", "quarterly", "board meeting", "merger", "acquisition",
                     "sebi", "cci", "nclt"]
```

Score: `+1` per positive keyword match, `-1` per negative. Threshold at ±2.

---

## Implementation Phases

| Phase | Deliverable | Effort |
|---|---|---|
| P1 | NSE announcements RSS fetcher + `news_events.db` schema | ~3h |
| P2 | LLM classifier (Haiku) + keyword fallback | ~2h |
| P3 | `news_gate_ok()` + OrderManager integration (entry veto) | ~2h |
| P4 | Open-position risk alert in monitor cycle | ~1h |
| P5 | Events calendar service + results-date auto-veto | ~3h |
| P6 | Composite scanner news_conviction column | ~2h |
| P7 | Google News RSS per ticker (broader coverage) | ~2h |

---

## docker-compose addition (P1)

```yaml
news-crawler:
  build: .
  container_name: news-crawler
  command: python -m news_crawler_service
  volumes:
    - ./data:/app/data
  environment:
    APP_TIMEZONE: "Asia/Kolkata"
    ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
    NEWS_CRAWLER_MODE: "paper"    # paper|off
    NEWS_LLM_ENABLED: "true"
  restart: unless-stopped
```

---

## Cost & Risk

| Item | Estimate |
|---|---|
| API cost (Haiku) | ~$0.02–0.05/trading day |
| Latency | <5s per batch classification |
| Failure mode | Fail-open — no news DB = no block (safe) |
| False positive risk | High-impact genuine announcement falsely blocked | 
| Mitigation | Manual override env: `NEWS_GATE_OVERRIDE=SYMBOL1,SYMBOL2` |
