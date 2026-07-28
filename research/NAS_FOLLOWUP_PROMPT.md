# NAS follow-up: validate the oi_flow factor, then backfill bhavcopy

## Context
This repo's Convex Engine is at roadmap step P0.5 (re-observing on formula
v2.1 — see CONVEX_NEXT_STEPS.md). On a dev machine, an independent IV/OI
audit was run against a **local, non-production** copy of iv_history.db —
treat every number below as a hypothesis to re-check against the REAL
production DB here, not as confirmed fact. Nothing has been changed in
engine/config.py yet — this is diagnosis + a validated test method, not a
shipped fix.

## Finding to validate
`engine/factors.py`'s `oi_flow` factor (~line 54-62) reads
`oi_buildup_history.bias` (via `oi_buildup_scanner.get_latest_buildup`) and
scores it as a forward-predictive vote in the v2.1 composite score, weighted
`W_OI_FLOW = 20.0` in `engine/config.py`. Three sibling factors (`inst_flow`,
`gap`, `premium_value`) were already found (P0.3) to vote on the wrong time
horizon — same-day snapshot mistaken for a forward-looking signal — and
zeroed out in v2.1 (`engine/config.py` lines 59-61, each with a `# v2.1: was
X.0` comment). `oi_flow` was never tested and is still live at weight 20.0.

A dev-side proxy test (daily OI-buildup classification -> forward 5-day
return, non-production data, n≈2800) found `LONG_BUILDUP`'s 95% CI sat
entirely below the unconditional baseline return — i.e. a "bullish"
classification statistically underperforming the average stock's return
over the same period. This is only suggestive since it wasn't run against
real production history — it needs confirmation here, at the engine's
actual 60-minute horizon, using its actual gating logic.

## Task 1 — confirm engine_outcomes is healthy here
```bash
python -c "
import sqlite3
conn = sqlite3.connect('data/iv_history.db')
print(conn.execute('SELECT COUNT(*), MIN(day), MAX(day) FROM engine_outcomes').fetchone())
print(conn.execute('PRAGMA quick_check').fetchone())
"
```
Confirm the table exists, has a meaningful row count and date range, and the
DB passes integrity check before proceeding.

## Task 2 — add + run the oi_flow replay variants
Open `engine/replay.py`. Find the `VARIANTS` list (ends with a `combo_fade`
variant). If these two variants aren't already present, add them right
after `combo_fade`:

```python
    Variant("oi0", "oi_flow weight -> 0 (same-day co-movement tag, not tested in P0.4)",
            weights={"oi_flow": 0.0}),
    Variant("combo_drop_oi", "inst0 + gap0 + pv0 + oi0 (shipped v2.1 formula, plus oi_flow)",
            weights={"inst_flow": 0.0, "gap": 0.0, "oi_flow": 0.0}, w_premium_value=0.0),
```

Then run:
```bash
python -m engine.replay
```

Compare `oi0` and `combo_drop_oi`'s train/valid ladders against `baseline`
and the currently-shipped `combo_drop` (same drops, minus oi_flow).

**Decision rule** — same discipline P0.4 already used: only adopt the
oi_flow drop if the A+ > A > B ladder becomes MORE monotone (or stays
monotone) in BOTH train and validation, and the ALL-row edge doesn't degrade
materially. If `combo_drop` (currently shipped) is already monotone and
`combo_drop_oi` doesn't improve on it, oi_flow isn't clearly broken — leave
it alone. Don't zero it out on vibes; require the same train/validation bar
every other factor drop was held to.

## Task 3 — if the replay confirms it, ship the fix
In `engine/config.py`, change:
```python
W_OI_FLOW = _f("ENGINE_W_OI_FLOW", 20.0)
```
to:
```python
W_OI_FLOW = _f("ENGINE_W_OI_FLOW", 0.0)  # v2.2: was 20.0 — same-day snapshot, no fwd edge (replay-confirmed)
```
Bump `FORMULA_VER` from `"v2.1"` to `"v2.2"`, and add a line to
CONVEX_NEXT_STEPS.md's P0.4 section recording the oi_flow finding, in the
same style as the existing inst_flow/gap/premium_value writeup.

## Task 4 — bhavcopy backfill (separate, lower priority)
Two files were built on the dev machine — copy them into this checkout if
git hasn't already picked them up:
- `research/backfill_bhavcopy.py`
- `research/__init__.py` (empty — just makes `research` a package)

It reuses `collectors.bhav_collector.BhavCollector` unchanged, rate-limits
to 5-12s between requests plus a 60s rest every 20 requests and exponential
backoff on failure (safe for a multi-month unattended run against NSE's
archive), and is resumable/idempotent — it skips any date already in
`delivery_daily`, so stopping and restarting only ever fills gaps.

Run from the real production data directory:
```bash
python -m research.backfill_bhavcopy --start 2025-01-01
```
Expect it to take a few hours; safe to leave unattended and check back later.
