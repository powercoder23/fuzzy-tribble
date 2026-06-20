# -*- coding: utf-8 -*-
"""Unit tests for cycle_gate.CycleGate using a temp SQLite db. No broker."""

import os
import sqlite3
import tempfile

from cycle_gate import CycleGate

TABLES = ["gap_history", "oi_buildup_history", "iv_rank_history"]


def _mkdb():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    with sqlite3.connect(path) as c:
        for t in TABLES:
            c.execute(f"CREATE TABLE {t} (timestamp TEXT)")
    return path


def _stamp(path, table, ts):
    with sqlite3.connect(path) as c:
        c.execute(f"INSERT INTO {table} (timestamp) VALUES (?)", (ts,))


def test_not_ready_until_any_data():
    g = CycleGate(TABLES, db_path=_mkdb())
    assert g.ready() is False


def test_ready_when_all_present_advanced_then_consumed():
    path = _mkdb()
    g = CycleGate(TABLES, db_path=path)
    for t in TABLES:
        _stamp(path, t, "2026-06-19 09:45:00")
    assert g.ready() is True
    g.mark()
    assert g.ready() is False                  # consumed; nothing new yet


def test_fires_again_only_after_a_fresh_full_cycle():
    path = _mkdb()
    g = CycleGate(TABLES, db_path=path)
    for t in TABLES:
        _stamp(path, t, "2026-06-19 09:45:00")
    assert g.ready_and_mark() is True
    _stamp(path, "gap_history", "2026-06-19 11:30:00")   # only one advanced
    assert g.ready() is False                  # paced by the slowest scan
    for t in ("oi_buildup_history", "iv_rank_history"):
        _stamp(path, t, "2026-06-19 11:30:00")
    assert g.ready() is True


def test_missing_table_is_ignored_not_blocking():
    path = _mkdb()
    g = CycleGate(TABLES + ["does_not_exist"], db_path=path)
    for t in TABLES:
        _stamp(path, t, "2026-06-19 09:45:00")
    assert g.ready() is True                    # missing table doesn't block


if __name__ == "__main__":
    import sys
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
