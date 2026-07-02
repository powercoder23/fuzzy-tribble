# engine/ — Convex Conviction Engine (P0)

The V2 decision core. One funnel replaces composite_scanner, trade_suggester,
morning_confluence, entry_gate, cycle_gate, pre_market_gate and the per-scanner
Telegram streams. See `V2_BLUEPRINT.md` for the full design.

```
regime.py      market posture (GREEN/AMBER/RED + lean + size multiplier)
factors.py     7 factors normalized from V1 *_history tables (strangler adapters)
conviction.py  gate stack + the ONE score formula (versioned)
pipeline.py    universe -> factors -> trigger -> gates -> score -> Decision
store.py       engine_decisions / engine_regime tables (iv_store.connect, WAL)
contracts.py   FactorReading · TriggerEvent · RegimeState · GateResult · Decision
config.py      every knob, env-overridable
```

Run beside V1 (observe-only, zero broker calls, zero orders):

```
python engine_runner.py            # 5-min cycles, 09:25–15:05 IST
pytest test_engine.py -q           # pure unit tests, no DB/network needed
```

P0 limits (by design): the only trigger source is sonar band breakouts already
in the DB; ORB / VWAP / break-retest triggers arrive in P2 when candle access
moves in-engine. Risk-state gates read an empty dict until the executor feeds
them (P3).

Invariants:
- **No trigger, no trade** — factors alone can only produce WATCH rows.
- **Explain or die** — every Decision (incl. REJECTED) carries `why` + full breakdown.
- **One writer per table** — engine owns `engine_decisions` / `engine_regime` only.
- **Formula versioning** — `FORMULA_VER` stored on every row; change weights → bump it.
