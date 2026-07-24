# Convex Engine — The Plan (one plan, P-phases only) — updated 2026-07-24

There is ONE roadmap: **P0 → P1 → P2 → P3 → P4** (same as the dashboard's
"Migration roadmap" panel). Everything we work on is a numbered step inside a
phase. No other numbering.

## P0 — Observe-only engine: prove the brain works  [ACTIVE — we are here]

The engine watches the market every 5 min and journals what it WOULD trade.
Zero orders. P0 does not end when we have "enough rows" — it ends when the
journal PROVES the picks are good.

| Step | What it means | Status |
|------|---------------|--------|
| P0.1 | Engine journals decisions beside V1 (running since Jul 3) | ✅ DONE |
| P0.2 | Truth layer: outcome labeler + Grade × Outcome scoreboard on dashboard | ✅ DONE (Jul 24) |
| P0.3 | Verdict from scoreboard: conviction ladder is INVERTED — A+ picks are the worst, and it's almost all on the CE (buy-call) side. Cause found: 3 factors (inst_flow, gap, premium_value) vote on the wrong time horizon | ✅ DONE (Jul 24) |
| P0.4 | **Fix the brain** — replay tool built (`engine/replay.py`); 38k labeled decisions (incl. rejected) replayed with train (Jul 3–16) / validation (Jul 17–23) split; 7 variants tested. Winner: drop `inst_flow` + `gap` + `premium_value` from the score (gates unchanged) — ladder goes monotone in train AND validation. Shipped as **formula v2.1** (Jul 24) | ✅ DONE |
| P0.5 | Re-observe 2 weeks on v2.1 (incl. at least one non-flat week). Pass = A+ > A > B on the scoreboard, v2.1 rows only. Turn on `ENGINE_PAPER_MODE=paper` so option-P&L evidence accrues in parallel | ⬅ **NEXT** |

Also done in P0 (supporting work, no separate track):
- Regime direction input fixed — NIFTY trend now feeds the market lean (was NULL for 3 weeks).
- Convex paper book built — engine can paper-trade its own A+/A picks (off by default,
  turn on with `ENGINE_PAPER_MODE=paper` when we start P0.5).

## P1 — entry_gate re-point  [BLOCKED until P0.4 + P0.5 pass]
V1 order flow gets gated by engine_decisions. Kills: composite · trade_suggester ·
morning_confluence. An inverted ladder must never gate order flow — that is why P1 waits.

## P2 — Native triggers + factors  [PARTIAL — triggers already ported early]
ORB / VWAP / break-retest / sonar-band computed in-engine. Kills the 8 V1 scanner services.

## P3 — Executor unification  [PENDING]
paper_trader + order_manager + auto-exit become one convex-executor. Kills discount
service, per-strategy notifiers, all V1 gates.

## P4 — Consolidation  [PENDING]
DB rename, compose shrinks to 4 services, absorbed modules deleted.

---

## The one thing to do next: P0.5 (restart the engine on v2.1, then wait for evidence)

1. Restart the convex-engine service so it picks up formula **v2.1**
   (and set `ENGINE_PAPER_MODE=paper` on it to start the Convex paper book).
2. Run the labeler nightly (`python -m engine.labeler`) so the scoreboard stays current.
3. Watch the dashboard's Grade × Outcome panel, **v2.1 section only**:
   pass = A+ > A > B after ~2 weeks including one non-flat week. Then P1 unblocks.

P0.4 replay verdict (for the record): baseline reproduced the inversion
(A+ −0.205 train); no single fix worked; `inst_flow=0 + gap=0 + premium_value=0`
made the ladder monotone on train AND validation. Gap-as-FADE showed top-grade
alpha (A+ +0.226/+0.126) but hurt grade B — parked as a v2.2 research candidate.
A+ is now rare by design (~2–3/day max score 82.5).

## Ignore
The "Evidence volume %" number on the dashboard is row-count only. It is NOT readiness.
Readiness = P0.4 + P0.5 passed. (The dashboard banner now says this explicitly.)
