# P2/P3 Review — Code Quality, Performance, Refactoring Design, Production Readiness

**Date:** 2026-07-02 · **Scope:** P2 items 7-9, P3 item 10 · **Prerequisites:** ARCHITECTURE_REVIEW_P0.md, STRATEGY_REVIEW_P1.md
All metrics below were measured against the tree, not estimated.

---

## 7. Code review

### 7.1 Duplicate code — measured

- **Risk managers:** `MomentumRiskManager` vs `BreakBounceRiskManager` are **83% textually identical** (79 vs 68 lines, diff-ratio). Same capital/max-risk/lots/SL/record/reset surface, forked instead of shared.
- **Runners:** the five scanner runners are 52-79% identical to `iv_rank_runner.py` — the same schedule-loop boilerplate five times; only the scanner class, config module, and log name differ.
- **Configs:** 12+ `*_config.py` files repeat the same `os.getenv`-with-cast pattern; `oi_config.py` even defines its own `_env_bool/_env_float/_env_int` helpers that nothing else reuses.
- **Notifications:** 4 per-strategy `*TelegramNotifier` classes + `notifications.py` + `collectors/notify.py` (a wrapper over notifications). Six paths to send one Telegram message.
- **Schema management:** the `iv_history` table + the same index (`idx_iv_security_time`) are created in **three places** (iv_store, discount.py ×2) — the P0 sole-writer fix removed the write path but the duplicate DDL code remains in `discount.py` (`ensure_iv_history_schema`, `init_iv_db`, `migrate_csv_to_sqlite`) and should be deleted.
- **IV math:** `iv_rank`/`iv_percentile` implemented twice (discount methods, iv_rank_scanner functions) — the scanner docstrings admit it ("Mirrors discount...").

### 7.2 Dead code — inventory

- **`old/` — 23 files** including an abandoned backtest suite: `basktest_engine.py` (sic), `intraday_backtest.py`, `nifty_intraday_production_backtest.py`, `stock_batch_backtest.py`, `trading_plan_backtest.py`, `nifty_combo_lab.py` and friends. Notable: **you already built NIFTY backtesting once and abandoned it** — mine these for the P1 §6 harness before writing from scratch, then delete the directory (it's in git history).
- Discontinued but maintained: `momentum_strategy.py` (1,301 lines) and `directional_iv_strategy.py` (522) are compose-disabled yet still imported by live code (`data_provider` → momentum fetchers + lot sizer; `main.py` → directional runner). They can't be deleted without extracting `ScripMasterLotSizer`, `get_intraday_candles`, `get_daily_candles`, `calculate_adx` — which is exactly the §9 indicator-library extraction.
- Root-level orphans: `deals.py` (superseded by `collectors/deals_collector.py`), `complete_json_tosqlite.py`, `instrument_mapper.py` (nothing imports it), `iv_history.csv` + `iv_migrated.flag` (migration done), `discounted_premiums.csv` (stale output), `fix_discount.md`, `WhatsApp Image ... .jpeg`, and a checked-in `.venv/` (3,900 site-package files polluting every search).
- Vestigial args carried everywhere: `hardtoken`/`client_id` on the scanner constructor; `--auto-loop` no-op flag.

### 7.3 Long functions — measured (excluding tests)

| Lines | Function | Note |
|---|---|---|
| 493 | `discount.scan_single_strike` | 25+ decisions, 6 responsibilities |
| 340 | `break_bounce_strategy.run_intraday_scan` | scan+select+size+alert+order in one body |
| 330 | `discount.scan_underlying` | fetch+metrics+persist+signal+log |
| 213 | `discount.fetch_expired_option_data` | fetch+cache+merge+validate |
| 154 | `pre_market_gate.evaluate` | 5 gates inline |
| 147 | `directional_iv_strategy.scan_single_strike` | fork of the 493-liner |
| 128 | `discount.generate_report` | pure logging |
| 123 | `break_bounce_strategy.check_5min_entry` | acceptable — cohesive |

Anything over ~80 lines here mixes decision logic with I/O and logging, which is why none of it is unit-tested. The newer scanners (iv_rank, oi_buildup, gap, sonar) keep pure functions small and tested — the codebase already knows the right style; the old core just predates it.

### 7.4 Typing — measured

**61% of 593 functions have any annotation.** The distribution is the story: newer scanners ~100%, but `discount.py` **0/68**, `main.py` 0/17, `paper_trader.py` 2/25, `data_provider.py` 3/27, `order_manager.py` 5/14. The untyped modules are precisely the ones that move money. No mypy/pyright config exists. Recommendation: add `mypy --strict` for new modules, annotate the OrderManager/paper_trader seam first (it's the API other code calls), and don't bother retro-typing `discount.py` — type the extracted modules (§9) instead.

### 7.5 Exception handling

~120 `except Exception`/bare-except sites (17 each in momentum/discount, 14 in break_bounce, 10 in order_manager). Three distinct patterns, needing different treatment:

1. **Deliberate fail-open gates** (order_manager, scanners) — legitimate policy, now alarmed (P0 §3.6 fix). Fine.
2. **Silent `except: pass` / `except: return default`** in data paths — e.g. `iv_store` readers returning `{}`/`[]`, `token_manager`, adapter parse loops (`logger.debug` on skipped strikes). These erase the difference between "no data" and "broken" — the exact mechanism that hid the P0 corruption for days. Every one of these should at minimum count into a per-cycle stats line.
3. **Broad catches around order placement** (`place_bracket_order`, `_place_order`) — catching `Exception` and returning a status dict is correct for the top level, but the same function should not swallow `KeyboardInterrupt`-adjacent conditions; use narrow exceptions where the failure mode is known (requests.Timeout, sqlite3.OperationalError).

### 7.6 Naming

`DiscountedPremiumScanner` runs a volatility-only system; `UpstoxDhanAdapter` speaks a dead broker's dialect; `discount.py` is the system kernel; `main.py` is only the discount service's main; `sonar_laplace` is one filter with two names; `basktest_engine.py` says it all. Naming is fixed by the §9 re-packaging, not by renames in place.

### 7.7 SOLID

- **SRP:** the god class (P0 §1.1) — 68 methods spanning broker I/O, math, persistence, scoring, alerting.
- **OCP:** adding a scanner = copy 3 files + edit compose; the framework (§9) makes it one class + one registry entry.
- **LSP/ISP:** no interfaces exist at all — strategies depend on the concrete scanner's 68-method surface to use 5 of them.
- **DIP:** every strategy constructs its own `DiscountedPremiumScanner` (concrete) rather than receiving a data-provider interface. Only `data_provider.py` and the newer scanners (injectable fetchers, injectable db_path) got this right.

---

## 8. Performance

Context: the system is I/O-bound (API pacing at 7 req/s, per-stock sleeps). CPU/pandas costs are secondary; API budget and SQLite behavior dominate. Priorities reflect that.

### 8.1 API usage — the real budget

- **Verified good:** daily candles are cached per (security, date-range) per day (`CACHE_DAILY_CANDLES=True`, discount.py:777) — my P0 §2.5 wording overstated this one; HV re-fetch happens once/day/stock, not per scan.
- **N+1 monitor pricing (worst offender):** `get_current_option_premium` fetches the **entire option chain to price one leg**, per open position, per 5-min tick (up to 5 positions × 75 ticks/day = ~375 full-chain calls/day for data you already fetch elsewhere). Fix: one chain fetch per (underlying, expiry) per tick shared across positions of the same name, or read the leg from the iv-collector's latest sweep when fresh (<3 min).
- **Expiry list per symbol per scan** in `scan_underlying` — expiries change once a week; cache day-keyed (the runtime-state `expiries` cache exists but `scan_underlying` bypasses it when `expiry=None`... it calls `get_expiry_list` each time — route it through `get_cached_or_fetch`).
- **Sonar:** ~200 per-symbol 5-min candle fetches per scan × 6 scans/day via the DataProvider *fallback* path (nothing subscribes, so the poller batching never engages). Either subscribe the universe once so the poller owns it, or accept and budget it explicitly.
- **No cross-process budget:** the 7 req/s lock is per-process. iv-collector (~0.5/s sustained) + discount bursts (7/s) + sonar bursts + monitor can stack toward Upstox's 500/min ceiling in the 09:50-10:05 overlap. A shared token-bucket (SQLite row or tiny file with atomic decrement) would make the global budget real. Low urgency, high correctness.

### 8.2 SQL / SQLite

- **Per-row INSERT loops** in every scanner's `persist()` (`for ... conn.execute`) — replace with one `executemany` (also fewer WAL fsyncs). Same for the pandas `iterrows()` that feed them (33 uses across 13 files — fine for 200-row frames, but the persist loops are the ones doing I/O per row).
- **Correlated subqueries** in `get_bulk_latest_snapshots` / `iv_rank._latest_iv_map` / the new daily-dedup reads — O(n·log n) with the existing `(security_id, timestamp)` index, acceptable at 200 symbols × ~10k rows; will degrade at millions of rows. When it does: add `(security_id, data_type, timestamp)` composite index and a `latest_snapshot` view. Not urgent.
- **Connection churn:** one connection per call throughout. With WAL this is correctness-safe; the cost is ~0.1-1 ms per open — irrelevant at current volumes. Leave it; revisit only in the replay harness where it will matter (millions of reads → pass one connection through).

### 8.3 Memory

Nothing concerning: frames are small (200 rows), candle caches are day-scoped, `deque` in the collector, `_expiry_cache` now clears at midnight (P0 fix). One slow leak: `MomentumTradeJournal`/CSV logs and `logs/*.log` grow unbounded (see §10 logging).

### 8.4 Threading & async

Post-P0 state: scan thread + monitor thread + (optional) poller threads, serialized at the rate-limiter lock; SQLite in WAL with busy_timeout; paper book WAL. That is the right shape for this scale. **Recommendation: do NOT migrate to asyncio.** The bottleneck is the deliberate 7 req/s pacing, not thread overhead; an async rewrite would touch every function signature for zero throughput gain (you can't go faster than the rate limit you set). The one async-shaped win — issuing the ~200-symbol sweep as batched concurrent requests up to the rate limit — is achievable with a small `ThreadPoolExecutor` inside `rate_limited_call`'s discipline if scan latency ever matters.

### 8.5 Duplicate calculations

ADX/EMA/VWAP computed inside momentum; HV/IV-rank/expected-move inside discount; SuperSmoother inside sonar; each recomputed per call from raw candles with no memo between strategies scanning the same symbol in the same minute. The §9 indicator library with a `(symbol, interval, candle_ts)`-keyed memo removes this class of waste and, more importantly, the *inconsistency* risk (two modules disagreeing on "the" ADX).

---

## 9. Refactoring target design

Goal state (2-3 weeks, incremental, each step shippable). This consolidates P0 §1.1 and everything above into one concrete layout:

```
trading/
  core/
    config.py          # ONE settings loader (pydantic-settings or the oi_config
                       #   _env_* helpers promoted); per-module namespaces
                       #   Settings.iv_rank.BUY_ZONE_MAX, all env-overridable;
                       #   validates at startup, prints effective config
    db.py              # iv_store.connect/init/integrity (already the seam)
    notify.py          # THE notifier: telegram+discord, retry, rate-limit,
                       #   used by everything; per-service prefix tag
    universe.py        # F&O list + security-id map + ban list + daily persist
  data/
    broker.py          # UpstoxClient: auth, rate limit (shared token bucket),
                       #   retry; the ONLY module that imports requests/SDK
    market_data.py     # chains, candles, expiries, quotes — typed dataclasses
                       #   (Chain, Candle, Quote) replacing the Dhan dict dialect;
                       #   adapters translate at this boundary only
    candles.py         # DataProvider (exists) + completed-candle guarantee
  indicators/
    __init__.py        # pure functions, one definition each, memoized:
                       #   ema, adx, vwap, hv, iv_rank, iv_percentile,
                       #   expected_move, super_smoother, dynamic_bands,
                       #   oi_quadrant (the one classify), pcr
  scanners/
    base.py            # class Scanner(ABC): scan()->DataFrame, persist(),
                       #   alert(); table name, cadence, config ns declared
                       #   as class attrs; generic persist via executemany
    iv_rank.py, oi_buildup.py, gap.py, delivery_surge.py,
    smart_money.py, sonar.py, composite.py     # logic only, ~1/3 current size
  strategies/
    base.py            # Strategy(ABC): generate_signals() -> list[Signal]
    volatility.py      # the discount scanner's scoring, decomposed
    break_bounce.py
  engine/
    signals.py         # Signal dataclass (symbol, side, strike, plan, factors)
    gates.py           # pre_market/breadth/entry/concentration behind one
                       #   Gate ABC with mode + health counters (exists ad hoc)
    order_manager.py   # exists — becomes the only consumer of gates
    paper.py           # paper_trader (exists, now with costs)
    costs.py           # exists
  research/
    replay.py          # P1 §6 harness
  services/
    runner.py          # ONE generic runner: python -m services.runner iv_rank
                       #   (kills 8 runner files); scheduling from Scanner attrs
    collector.py       # iv_collector_service
    scheduler.py       # main.py
```

**Plugin system:** don't build a plugin *architecture* (entry points, dynamic discovery) — you are one person; a `SCANNERS = {"iv_rank": IVRankScanner, ...}` registry dict in `services/runner.py` gives you "add a scanner = one class + one dict line + one compose service" with zero magic. Revisit real plugins only if third parties ever contribute scanners.

**Configuration management:** one `Settings` object loaded once, sections per module, every field env-overridable with the current env names preserved (zero behavioral change), validated at startup with the effective values logged — this converts today's "which of 12 config files and 80 env vars is live?" into one startup banner. Keep secrets out of it (see §10.2).

**Migration order (each step independently shippable):**
1. `core/notify.py` + delete the 5 duplicate notifier paths (lowest risk, immediate).
2. `indicators/` — extract from momentum/discount/sonar; unit tests move with them; unblocks deleting the discontinued strategy modules.
3. `services/runner.py` generic runner — delete 8 runner files.
4. `data/broker.py` + `market_data.py` dataclasses — the Dhan-dialect eradication; adapters shrink to translation.
5. Decompose `scan_single_strike` into `strategies/volatility.py` (filters → score → plan as separate pure functions) — do this **together with** the P1 replay harness so every extracted function gets validated against recorded behavior.

---

## 10. Production readiness (P3)

| Area | Verdict | Detail |
|---|---|---|
| Docker | 🟡 | `restart: unless-stopped` ✓; lean scanner image ✓ (`requirements-scanner.txt` correctly minimal). But: **no healthchecks anywhere** (0 in compose), no resource limits, containers run as **root**, `chmod -R 777 /app/data /app/logs`, and the full image installs **chromium + git** (~700 MB) even for services that never run selenium — only the login flow needs it; move token bootstrap to a dedicated one-shot image. |
| Env vars | 🟡 | `.env` correctly gitignored (verified: not tracked; `.env.example` provided ✓). But `entrypoint.sh` runs token init on every broker-facing service start (iv-collector, discount, break-bounce + profile services) — 3-5 containers racing the same token refresh at compose-up; the `fcntl` lock in upstox_token_manager serializes them on one host, but verify the refresh is idempotent when a second process wins the race. |
| Secrets | 🔴 | `.env` holds **both brokers' PINs and TOTP secrets** — full account-takeover material — plus Telegram token, distributed into all 10+ containers via `env_file`. `data/tokens/access_token.json` sits world-readable (777 volume). Minimum fixes: split secrets from tunables (compose `secrets:` or a separate env file mounted only into iv-collector + discount), chmod 600 the token file, and don't inject broker credentials into the 7 zero-API scanner containers **that never call the broker**. |
| Logging | 🟡 | Per-service files ✓, but **no rotation** (plain `FileHandler` — scheduler.log grows forever), `discount.py` calls `logging.basicConfig` at import (a library configuring the root logger → duplicate lines in every importer), and log level is not env-configurable. Fix: `RotatingFileHandler(50MB×3)` in one `core/logging.py`, delete the basicConfig from discount. |
| Monitoring | 🔴 | Nothing exists beyond Telegram messages. Added today: DB `quick_check` alarm + gate-failure alerts. Still missing the two that matter: **a heartbeat** ("collector alive, last pass HH:MM, saved N/M") — silence currently means either "quiet market" or "dead for 3 days"; and **data-freshness checks** (alert if newest intraday snapshot > 30 min old during market hours, if a scanner's `*_history` has no rows today by 11:00). All are ~20-line additions to the collector loop. |
| Error recovery | 🟢 | Restart policies + collector startup catch-up ✓; state day-scoped so restarts are safe; paper book survives restarts on the volume ✓. One gap: if the collector crash-loops (bad token), every downstream scanner runs happily on stale data — the freshness alert above is the fix. |
| Health checks | 🔴 | None. Add per-service compose healthchecks that check *work done*, not process-up: collector → newest intraday row < 20 min old (market hours); scanners → own `*_history` row today; discount → scheduler.log mtime < 20 min. `python -c` one-liners against the shared DB are sufficient. |
| Retry logic | 🟡 | `fetch_with_retry` (5 attempts, exp backoff) ✓, `_collect_one` (2 attempts, 429-aware backoff) ✓, bhav triple-slot ✓. Gaps: the **validator loophole** in `fetch_with_retry` (P0 §3.8 — invalid-but-has-"data" responses pass), and `notifications.notify` sends once with a timeout and no retry — a Telegram blip silently drops a risk alert; add one retry + the failure counter. |
| Rate limiting | 🟡 | Per-process 7/s with a real lock (post-P0) ✓; collector paces via sleeps ✓. No **global** budget across containers (see §8.1) — a shared token bucket keyed in SQLite closes it. |

**P3 quick wins, in order:** (1) log rotation + kill discount's basicConfig; (2) collector heartbeat + freshness alerts; (3) compose healthchecks; (4) secrets split + chmod 600 tokens + stop injecting broker creds into zero-API scanners; (5) notify retry; (6) shared rate budget.

---

## Priority actions (P2+P3 combined)

1. Delete dead weight: `old/` (after mining the backtest files), `deals.py`, `instrument_mapper.py`, `complete_json_tosqlite.py`, stale CSVs, `.venv` from the tree, discount's dead DDL trio. Zero risk, immediate clarity.
2. Fix the N+1 monitor chain fetch (biggest API waste). (§8.1)
3. `core/notify.py` consolidation + retry + failure counter. (§9.1, §10)
4. Log rotation, heartbeat, freshness alerts, healthchecks. (§10)
5. Secrets split; scanner containers lose broker creds. (§10)
6. Indicator library extraction (unblocks deleting discontinued strategies). (§9.2)
7. Generic runner (−8 files), then broker/dataclass boundary, then the scan_single_strike decomposition **alongside the replay harness** — never before it. (§9)
