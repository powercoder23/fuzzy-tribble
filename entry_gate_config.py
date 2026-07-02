# -*- coding: utf-8 -*-
"""Configuration for the composite entry gate (ARCHITECTURE_REFACTOR_PLAN.md §9).

Lets any strategy gate (or just annotate) a trade on the composite conviction
before booking. Default OFF so existing behaviour is unchanged until you opt in.
"""

import os

# off  -> disabled, never looks anything up, always allows (current behaviour)
# soft -> never blocks; returns the composite score so it can rank / annotate
# hard -> blocks a trade whose direction disagrees or whose conviction is weak
GATE_MODE = os.getenv("GATE_MODE", "off").strip().lower()

MIN_GATE_SCORE = float(os.getenv("GATE_MIN_SCORE", "45"))   # hard mode threshold

# If there is no composite row yet (e.g. early in history), allow the trade rather
# than silently halting everything. Fail-open.
ALLOW_IF_NO_COMPOSITE = os.getenv("GATE_ALLOW_IF_MISSING", "true").strip().lower() == "true"

# ---- V2 strangler (P1) ------------------------------------------------------
# composite -> gate on composite_history (current behaviour, default)
# engine    -> gate on the Convex engine's engine_decisions table instead
GATE_SOURCE = os.getenv("GATE_SOURCE", "composite").strip().lower()

# engine mode: how stale a decision may be and still gate a trade (minutes).
ENGINE_MAX_AGE_MIN = float(os.getenv("GATE_ENGINE_MAX_AGE_MIN", "20"))
