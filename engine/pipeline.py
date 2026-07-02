# -*- coding: utf-8 -*-
"""Cycle pipeline — one engine pass over the universe (the whole funnel).

Dependency-injected for testability: regime_fn / factors_fn / trigger_fn /
universe_fn default to the production loaders but tests pass fakes.
Zero broker calls anywhere in this module.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from engine import config as cfg
from engine import conviction, factors as factors_mod, regime as regime_mod, store
from engine.contracts import Decision, EMITTED, REJECTED, WATCH, RED

logger = logging.getLogger(__name__)


def default_universe(db_path: str) -> list[tuple[str, str]]:
    """[(security_id, symbol)] — every name the IV collector has seen."""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT security_id, MAX(symbol) FROM iv_history GROUP BY security_id"
            ).fetchall()
        return [(str(sid), sym) for sid, sym in rows if sym]
    except sqlite3.OperationalError:
        return []


def _breadth_snapshot(db_path: str):
    try:
        import breadth
        return breadth.compute(db_path=db_path)
    except Exception:  # noqa: BLE001
        return None


class EnginePipeline:
    def __init__(self, db_path: str | None = None, *,
                 regime_fn=None, universe_fn=None, factors_fn=None,
                 trigger_fn=None, risk_state_fn=None, now_fn=None):
        if db_path is None:
            from collectors import iv_store
            db_path = iv_store.DB_PATH
        self.db_path = db_path
        self._regime_fn = regime_fn or (lambda: regime_mod.load(self.db_path))
        self._universe_fn = universe_fn or (lambda: default_universe(self.db_path))
        self._factors_fn = factors_fn      # (sid, symbol, breadth_snap) -> dict
        self._trigger_fn = trigger_fn or factors_mod.load_trigger
        self._risk_state_fn = risk_state_fn or (lambda: {})
        self._now_fn = now_fn or datetime.now

    # ------------------------------------------------------------------ #
    def run(self, persist: bool = True) -> dict:
        """One full cycle. Returns {regime, emitted, watch, rejected}."""
        now = self._now_fn()
        now_hhmm = now.strftime("%H:%M")
        regime = self._regime_fn()
        risk_state = self._risk_state_fn()
        breadth_snap = None if self._factors_fn else _breadth_snapshot(self.db_path)
        load_factors = self._factors_fn or (
            lambda sid, sym: factors_mod.load_factors(sid, sym, breadth_snap))

        emitted, watch, rejected = [], [], []
        for sid, symbol in self._universe_fn():
            f = load_factors(sid, symbol)
            trig = self._trigger_fn(sid)

            if trig is None:
                cs = conviction.context_score(f)
                if cs >= cfg.WATCH_MIN_CONTEXT and regime.posture != RED:
                    watch.append(Decision(
                        symbol, sid, WATCH, score=cs,
                        factors=list(f.values()), formula_ver=cfg.FORMULA_VER,
                        why=f"{symbol} — context {cs}, awaiting trigger"))
                continue

            gates = conviction.run_gates(regime, f, trig, risk_state, now_hhmm)
            ok, fail_reason = conviction.gates_pass(gates)
            if not ok:
                rejected.append(Decision(
                    symbol, sid, REJECTED, direction=trig.direction, trigger=trig,
                    factors=list(f.values()), gates=gates,
                    reject_reason=fail_reason, formula_ver=cfg.FORMULA_VER,
                    why=f"{symbol} {trig.direction} rejected — {fail_reason}"))
                continue

            res = conviction.score(trig, f, regime)
            if res["grade"] is None:
                rejected.append(Decision(
                    symbol, sid, REJECTED, direction=trig.direction, trigger=trig,
                    factors=list(f.values()), gates=gates,
                    score=res["score"], breakdown=res["breakdown"],
                    reject_reason=f"score {res['score']} < grade floor",
                    formula_ver=cfg.FORMULA_VER,
                    why=f"{symbol} {trig.direction} — trigger without context"))
                continue

            emitted.append(Decision(
                symbol, sid, EMITTED, direction=trig.direction, trigger=trig,
                factors=list(f.values()), gates=gates,
                score=res["score"], grade=res["grade"], breakdown=res["breakdown"],
                formula_ver=cfg.FORMULA_VER,
                why=conviction.why_line(symbol, trig, res, f)))

        emitted.sort(key=lambda d: d.score, reverse=True)
        watch.sort(key=lambda d: d.score, reverse=True)

        if persist:
            store.ensure_tables(self.db_path)
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            store.persist_regime(self.db_path, regime, ts)
            store.persist_decisions(self.db_path, emitted + watch + rejected, ts)

        logger.info("engine: %s | %d emitted, %d watch, %d rejected",
                    regime.posture, len(emitted), len(watch), len(rejected))
        return {"regime": regime, "emitted": emitted,
                "watch": watch, "rejected": rejected}
