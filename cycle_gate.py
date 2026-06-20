# -*- coding: utf-8 -*-
"""
cycle_gate.py — fire an action once per COMPLETED cycle of the repeated scans
(ARCHITECTURE_REFACTOR_PLAN.md §13).

The repeated intraday scanners (gap, oi_buildup, iv_rank, …) each stamp a
timestamp into their *_history table every run. A "cycle" is complete once EVERY
participating scan has produced a row newer than the last time we acted. The gate
then fires once and advances its watermark — so the Trade Suggester runs once per
full cycle instead of on every discount tick.

Fail-open: a table that doesn't exist yet (scanner not running) is ignored rather
than blocking forever. The gate paces itself to the SLOWEST participating scan.
"""

from __future__ import annotations

import logging
import sqlite3

from collectors import iv_store

logger = logging.getLogger(__name__)

# Repeated intraday scans whose freshness defines a cycle.
DEFAULT_TABLES = ["gap_history", "oi_buildup_history", "iv_rank_history"]


class CycleGate:
    def __init__(self, tables=None, db_path: str | None = None):
        self.tables = list(tables or DEFAULT_TABLES)
        self.db_path = db_path or iv_store.DB_PATH
        self._marks: dict = {}     # table -> last-acted MAX(timestamp)

    def _latest(self, table) -> str | None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(f"SELECT MAX(timestamp) FROM {table}").fetchone()
        except sqlite3.OperationalError:
            return None            # table not created yet -> ignored
        return row[0] if row and row[0] else None

    def _present(self) -> dict:
        latest = {t: self._latest(t) for t in self.tables}
        return {t: v for t, v in latest.items() if v is not None}

    def ready(self) -> bool:
        """True once every present participating table has advanced past its
        watermark (i.e. a fresh full cycle is available)."""
        present = self._present()
        if not present:
            return False           # nothing has run yet
        return all(present[t] > self._marks.get(t, "") for t in present)

    def mark(self) -> None:
        """Record the current cycle as consumed."""
        for t, v in self._present().items():
            self._marks[t] = v

    def ready_and_mark(self) -> bool:
        """Convenience: if a fresh cycle is ready, consume it and return True."""
        if self.ready():
            self.mark()
            logger.info("cycle-gate: fresh cycle complete (%s)", ",".join(self._present().keys()))
            return True
        return False
