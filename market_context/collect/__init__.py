# -*- coding: utf-8 -*-
"""market_context.collect — derive and persist market observations.

Phase 1 keeps the four collectors in one module (snapshots.py) because they
share a single traversal of the TickCache and a single write transaction.
Splitting them into futures.py / breadth.py / sector.py / vix.py (as sketched
in MARKET_CONTEXT_PLAN.md) would triple the cache walks for no benefit; the
functions are already independent and separately testable.
"""
