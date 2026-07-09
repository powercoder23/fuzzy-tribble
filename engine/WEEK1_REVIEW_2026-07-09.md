# Convex V2 Engine — Week 1 Observe-Only Review (2026-07-09)

**Data window:** 2026-07-03, 07-06, 07-07, 07-08 (4 trading days). VIX 11.6–12.3 — a very calm, flat, low-vol week.

## Verdict: NO-GO for full P1

Infrastructure is healthy. **Signal quality is not** — the conviction ladder is inverted on its only week of data. Do **not** retire composite_scanner / trade_suggester / morning_confluence yet.

## What's healthy
- **Logging & cadence:** engine_decisions + engine_regime writing continuously 09:25–15:05 every trading day, ~68 cycles/day (matches 5-min loop). Full session covered, no gaps.
- **candles_5m persisting:** 14,421 rows/day, 209 symbols, 09:15–14:55. Sonar/candle hook is working — and triggers have moved in-engine ahead of the README's P0 note: ORB (688), VWAP (137), BREAK_RETEST (95) all firing alongside SONAR_BAND (1019).
- **Explainability:** every `why` line is coherent (trigger + quality + supporting factors + IV state + grade/score). Reject reasons are sensible gate logic (with_the_tape, premium_not_expensive, entry_cutoff, factors_not_contradicting, score floor). Score floor of 45 enforced on all EMITTED.
- **Volume is not a fire-hose:** entry_gate is pull-based with a 20-min freshness window + fail-open, so ~485 emits/day = a signal source strategies query, not 485 orders.

## Blocking issue 1 — inverted conviction ladder (critical)
Forward underlying spot direction after each EMITTED signal (this flat week):

| Grade | 30m hit | 60m hit | 90m hit | avg 60m move |
|-------|---------|---------|---------|--------------|
| A+    | 24%     | 21%     | 18%     | **−0.46%** |
| A     | 38%     | 42%     | 37%     | −0.21% |
| B     | 45%     | 50%     | 44%     | −0.06% |

Monotonic and stable across horizons: **the higher the engine's conviction, the worse the forward move** — A+ is systematically the worst grade. That's a broken ranking (likely a flipped factor sign, mislabeled trigger direction, or a score-weight problem), not just noise. Caveats: only 4 flat days (worst regime to judge a directional engine), and spot direction ≠ option P&L. But an inverted ladder must be root-caused before the engine becomes the sole gate.

## Blocking issue 2 — regime running one-legged
`index_slope_pct` is NULL on all 269 regime rows — the NIFTY slope input is never passed into `regime.load()`. Regime lean is derived from breadth alone, and the breadth/slope disagreement cross-check never fires. Lean drives the #1 reject reason (`with_the_tape`, ~900–1000/day) and the CE/PE emit tilt, so this directly shapes what gets emitted.

Also: no RED posture all week (GREEN/AMBER only) — plausible at VIX 11–12, but the RED path is untested.

## Recommendation
1. **Fix index_slope_pct** — pass NIFTY SuperSmoother slope into `regime.load()`.
2. **Root-cause the inverted grade→edge relationship** — audit factor signs / trigger direction labels / score weights; bump FORMULA_VER.
3. **Re-evaluate over ≥1 non-flat week** (higher VIX / some AMBER-RED days) before trusting forward edge.
4. **Interim:** OK to run `GATE_SOURCE=engine` in **soft** mode (annotate/rank only, never blocks, alongside composite) to gather live A/B comparison. Do **not** use hard mode and do **not** retire the V1 gates until 1–3 are done.
