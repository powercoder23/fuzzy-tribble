# -*- coding: utf-8 -*-
"""
market_context/regime/engine.py — classify all six axes and persist mc_regime.

Orchestration only: the maths lives in features/estimators.py, the thresholds
in regime/axes.py, and the anti-whipsaw logic in regime/hysteresis.py.

There is deliberately NO composite regime label, no bias, no size multiplier
and no exit verdict. Six independent axes are published and each strategy
consumes the ones it needs. `test_market_context.py` fails the build if a
trading-decision field ever appears in the payload.

STATE LIVES IN THE PROCESS, WARM-STARTED FROM THE DB
----------------------------------------------------
Dwell and score history are held in memory and seeded from the most recent
mc_regime row of the SAME day on startup. A restart therefore resets dwell
credit rather than inheriting a stale one — conservative in the right
direction, since a freshly-restarted service should not claim a state has been
held for two hours when it has not observed any of it.
"""

from __future__ import annotations

import json
import logging
from contextlib import closing
from datetime import datetime

from market_context import config as cfg
from market_context import store
from market_context.contracts import ALL_AXES, SCHEMA_VERSION, UNKNOWN
from market_context.features import builder as feature_builder
from market_context.regime import axes as axis_mod
from market_context.regime.hysteresis import (
    AxisTracker, confidence_score, transition_probability,
)

logger = logging.getLogger(__name__)

#: Column order for mc_regime, generated so it cannot drift from the schema.
_AXIS_COLUMNS = ("state", "score", "confidence", "direction",
                 "dwell_minutes", "transition_prob")

_COLUMNS = ["ts"]
for _axis in ALL_AXES:
    _COLUMNS.extend(f"{_axis}_{suffix}" for suffix in _AXIS_COLUMNS)
    if _axis == "trend":
        _COLUMNS.append("trend_event")
_COLUMNS.extend(["axes_available", "axis_inputs", "reasons", "data_quality",
                 "config_hash", "config_version", "schema_version"])


class RegimeEngine:
    """Stateful six-axis classifier."""

    def __init__(self, db_path=None):
        self.db_path = db_path
        self.trackers = {name: AxisTracker(name=name) for name in ALL_AXES}
        self._warm_started = False

    # ---- warm start -------------------------------------------------------- #
    def warm_start(self, now: datetime | None = None) -> bool:
        """Adopt the last published states from today's most recent row.

        Only same-day rows are used: carrying yesterday's HIGH_VOL into this
        morning would assert continuity across an overnight session nobody
        observed.
        """
        if self._warm_started:
            return False
        self._warm_started = True
        row = store.latest_regime_row(self.db_path)
        if not row or not row.get("ts"):
            return False
        now = now or datetime.now()
        if str(row["ts"])[:10] != now.strftime("%Y-%m-%d"):
            logger.info("regime: last row is from a prior session — starting cold")
            return False
        adopted = 0
        for name, tracker in self.trackers.items():
            state = row.get(f"{name}_state")
            if state and state != UNKNOWN:
                tracker.state = state
                tracker.since = now          # dwell restarts; see docstring
                adopted += 1
        logger.info("regime: warm-started %d axis state(s) from %s", adopted, row["ts"])
        return adopted > 0

    # ---- main -------------------------------------------------------------- #
    def run(self, cache=None, now: datetime | None = None) -> dict | None:
        """Build features, classify every axis, persist both rows.

        Returns the classification dict, or None if nothing could be produced.
        Never raises.
        """
        now = now or datetime.now()
        try:
            self.warm_start(now)
            fv = feature_builder.build(cache=cache, now=now, db_path=self.db_path)
            feature_builder.persist(fv, self.db_path)
            result = self.classify(fv, now)
            self.persist(result)
            return result
        except Exception:
            logger.exception("regime engine: run failed (non-fatal)")
            return None

    def classify(self, fv, now: datetime | None = None) -> dict:
        """Feature vector -> per-axis published states. Pure apart from the
        trackers' own state, so it is directly unit-testable."""
        now = now or datetime.now()
        out = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "axes": {},
            "axes_available": [],
            "axis_inputs": {},
            "reasons": {},
            "data_quality": fv.data_quality,
            "config_hash": fv.config_hash,
            "config_version": fv.config_version,
            "schema_version": SCHEMA_VERSION,
            "transitioned": False,
        }

        for name in ALL_AXES:
            tracker = self.trackers[name]
            classifier = axis_mod.CLASSIFIERS[name]
            try:
                res = classifier(fv, tracker.state)
            except Exception:
                logger.exception("regime: %s classifier failed", name)
                res = axis_mod.AxisResult()

            observation = tracker.update(res.state, res.score, now)
            subsample = (name == "breadth" and getattr(fv, "breadth_is_subsample", False))
            confidence = confidence_score(
                agreement=res.agreement,
                data_quality=fv.data_quality,
                dwell_minutes=observation.dwell_minutes,
                boundary_margin=res.margin,
                subsample=subsample,
            )
            prob = transition_probability(observation.momentum, res.margin, confidence)

            available = observation.state not in (UNKNOWN, "", None)
            out["axes"][name] = {
                "state": observation.state,
                "score": res.score,
                "confidence": round(confidence, 4),
                "direction": observation.direction,
                "dwell_minutes": observation.dwell_minutes,
                "transition_prob": round(prob, 4),
                "event": res.event if name == "trend" else None,
            }
            if available:
                out["axes_available"].append(name)
            out["axis_inputs"][name] = {
                k: (round(v, 6) if isinstance(v, (int, float)) else v)
                for k, v in (res.parts or {}).items()
            }
            out["reasons"][name] = list(res.reasons or [])
            if observation.transitioned:
                out["transitioned"] = True

        return out

    # ---- persistence ------------------------------------------------------- #
    def persist(self, result: dict) -> bool:
        if not result:
            return False
        values = [result["ts"]]
        for name in ALL_AXES:
            axis = result["axes"].get(name, {})
            values.extend([
                axis.get("state"), axis.get("score"), axis.get("confidence"),
                axis.get("direction"), axis.get("dwell_minutes"),
                axis.get("transition_prob"),
            ])
            if name == "trend":
                values.append(axis.get("event"))
        values.extend([
            json.dumps(result["axes_available"]),
            json.dumps(result["axis_inputs"], default=str),
            json.dumps(result["reasons"], default=str),
            result["data_quality"], result["config_hash"],
            result["config_version"], result["schema_version"],
        ])

        placeholders = ",".join("?" * len(_COLUMNS))
        try:
            with closing(store.connect(self.db_path)) as conn:
                conn.execute(
                    f"INSERT OR IGNORE INTO mc_regime ({','.join(_COLUMNS)}) "
                    f"VALUES ({placeholders})", values)
                conn.commit()
            return True
        except Exception:
            logger.exception("regime engine: persist failed (non-fatal)")
            return False

    # ---- reporting --------------------------------------------------------- #
    def summary(self, result: dict) -> str:
        if not result:
            return "regime: unavailable"
        bits = []
        for name in ALL_AXES:
            axis = result["axes"].get(name, {})
            state = axis.get("state")
            if state and state != UNKNOWN:
                bits.append(f"{name}={state}({axis.get('confidence', 0):.2f})")
        return (f"regime @ {result['ts']} | " + (" ".join(bits) or "no axes")
                + f" | dq={result['data_quality']:.2f}")
