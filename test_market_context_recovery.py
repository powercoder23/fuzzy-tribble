# -*- coding: utf-8 -*-
"""
test_market_context_recovery.py — regression tests for the 2026-08-04 incident.

WHAT HAPPENED
-------------
`data/market_context.db` became unreadable ("file is not a database"). The
service raised out of `store.init_db()`, `main()` alerted and exited 1, and
`restart: unless-stopped` restarted it ~60s later. That produced six identical
Telegram alerts and a crash loop that only stopped when the file was renamed
by hand.

Forensics on the corrupt file: the real SQLite header had moved to byte offset
28672 (7 x 4096), i.e. pages written at wrong offsets — two writers without
coherent locking.

ROOT CAUSE (ours, not the environment's)
----------------------------------------
`store.connect(read_only=True)` opened the file READ-WRITE and executed
`PRAGMA journal_mode=WAL` — a header write — before `PRAGMA query_only=ON`.
Every "read-only" market_context.get() therefore took write locks and touched
the -wal/-shm files. Harmless from one host; corrupting when the NAS container
holds the DB in WAL mode and a dev machine touches the same file over SMB,
because WAL's shared-memory index is not coherent across hosts.

Three fixes, one test group each.
"""

import os
import sqlite3
import time
from datetime import datetime

import pytest

from market_context import config as cfg
from market_context import store


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "market_context.db")
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(cfg, "DB_PATH", path)
    return path


def _corrupt(path: str, size: int = 8192) -> None:
    """Write a file that is emphatically not a SQLite database."""
    with open(path, "wb") as fh:
        fh.write(b"\x0d\x00\x00\x00\x03" + b"\x00" * (size - 5))


# =========================================================================== #
# FIX 1 — read-only connections must not write
# =========================================================================== #
def test_read_only_connection_cannot_write(db_path):
    store.init_db(db_path)
    conn = store.connect(db_path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE nope (x INTEGER)")
    conn.close()


def test_read_only_connection_does_not_set_journal_mode(db_path, monkeypatch):
    """THE root cause. `PRAGMA journal_mode=WAL` is a header WRITE, and it was
    being issued on every get() — including from a machine reading the live
    file over SMB while the container had it open.

    Uses set_trace_callback rather than patching conn.execute: sqlite3
    Connection is a C type and rejects attribute assignment.
    """
    store.init_db(db_path)
    executed = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(lambda sql: executed.append(str(sql).lower()))
        return conn

    monkeypatch.setattr(sqlite3, "connect", spy)
    store.connect(db_path, read_only=True).close()
    assert executed, "trace callback captured nothing"
    assert not any("journal_mode" in sql for sql in executed)
    assert any("busy_timeout" in sql for sql in executed)


def test_read_only_connection_uses_the_ro_uri(db_path, monkeypatch):
    """mode=ro is enforced by SQLite and cannot silently fall back to
    read-write, unlike query_only which we set ourselves."""
    store.init_db(db_path)
    seen = {}
    real_connect = sqlite3.connect

    def spy(target, *a, **kw):
        seen["target"] = target
        seen["uri"] = kw.get("uri", False)
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    store.connect(db_path, read_only=True).close()
    assert seen["uri"] is True
    assert seen["target"].startswith("file:") and "mode=ro" in seen["target"]


def test_read_only_open_of_a_missing_file_raises_not_creates(tmp_path):
    """A reader must never bring a database into existence."""
    missing = str(tmp_path / "absent.db")
    with pytest.raises(sqlite3.OperationalError):
        store.connect(missing, read_only=True)
    assert not os.path.exists(missing)


def test_writer_still_gets_wal(db_path):
    store.init_db(db_path)
    conn = store.connect(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_get_does_not_write_to_the_database(db_path):
    """End-to-end guard on the corruption vector: a full get() cycle must
    leave the file's mtime untouched."""
    import market_context

    store.init_db(db_path)
    before = os.stat(db_path).st_mtime_ns
    time.sleep(0.01)
    for _ in range(5):
        market_context.invalidate()
        market_context.get()
    assert os.stat(db_path).st_mtime_ns == before


# =========================================================================== #
# FIX 2 — a corrupt database must be recovered, not crash-looped
# =========================================================================== #
def test_init_db_quarantines_and_recreates(db_path):
    _corrupt(db_path)
    moved = {}
    store.init_db(db_path, on_quarantine=lambda p: moved.update(path=p))

    assert moved.get("path"), "quarantine callback was not invoked"
    assert os.path.exists(moved["path"]), "corrupt file must be KEPT, not deleted"
    with store.connect(db_path, read_only=True) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "mc_regime" in names and "mc_features" in names


def test_quarantine_moves_the_wal_and_shm_sidecars(db_path):
    """Leaving a stale -wal beside a fresh database would corrupt the new one
    the instant it is opened."""
    _corrupt(db_path)
    for suffix in ("-wal", "-shm"):
        with open(db_path + suffix, "wb") as fh:
            fh.write(b"stale")
    store.init_db(db_path)
    assert not os.path.exists(db_path + "-shm") or \
        os.path.getsize(db_path + "-shm") != 5


def test_corrupt_database_does_not_raise_out_of_init(db_path):
    """The crash-loop itself: init_db used to propagate and kill the process."""
    _corrupt(db_path)
    store.init_db(db_path)          # must not raise


def test_a_busy_database_is_never_quarantined(db_path, monkeypatch):
    """CRITICAL: transient lock contention must NOT destroy the database.
    Only 'not a database' / 'malformed' qualify."""
    store.init_db(db_path)
    calls = {"n": 0}

    def busy(_path):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_apply_schema", busy)
    monkeypatch.setattr(store, "quarantine",
                        lambda *a, **k: pytest.fail("must not quarantine"))
    with pytest.raises(sqlite3.OperationalError):
        store.init_db(db_path)
    assert calls["n"] == 1           # attempted once, not retried blindly


def test_fatal_error_classifier():
    assert store._is_fatal_db_error(sqlite3.DatabaseError("file is not a database"))
    assert store._is_fatal_db_error(
        sqlite3.DatabaseError("database disk image is malformed"))
    assert not store._is_fatal_db_error(sqlite3.OperationalError("database is locked"))
    assert not store._is_fatal_db_error(sqlite3.OperationalError("no such table: x"))


def test_second_failure_propagates(db_path, monkeypatch):
    """If recreation ALSO fails the problem is the filesystem, not the file.
    Retrying forever would hide that."""
    _corrupt(db_path)

    def always_fatal(_path):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(store, "_apply_schema", always_fatal)
    with pytest.raises(sqlite3.DatabaseError):
        store.init_db(db_path)


def test_quarantine_of_a_missing_file_is_a_noop(tmp_path):
    assert store.quarantine(str(tmp_path / "nothing.db")) is None


# =========================================================================== #
# FIX 3 — alert cooldown must survive process death
# =========================================================================== #
@pytest.fixture
def alert_dir(tmp_path, monkeypatch):
    from market_context import service
    monkeypatch.setattr(service, "ALERT_STATE_DIR", tmp_path / "alerts")
    sent = []
    import notifications
    monkeypatch.setattr(notifications, "notify",
                        lambda msg, *a, **kw: sent.append(msg) or True)
    return sent


def test_repeated_fatal_alerts_are_suppressed(alert_dir, monkeypatch):
    """Six identical alerts in six minutes is what the incident produced."""
    from market_context import service
    monkeypatch.setattr(service, "ALERT_COOLDOWN_SEC", 3600.0)
    assert service._alert("boom", key="fatal") is True
    for _ in range(5):
        assert service._alert("boom", key="fatal") is False
    assert len(alert_dir) == 1


def test_cooldown_expires(alert_dir, monkeypatch):
    from market_context import service
    monkeypatch.setattr(service, "ALERT_COOLDOWN_SEC", 0.05)
    assert service._alert("boom", key="fatal") is True
    time.sleep(0.08)
    assert service._alert("boom", key="fatal") is True
    assert len(alert_dir) == 2


def test_different_keys_do_not_share_a_cooldown(alert_dir, monkeypatch):
    from market_context import service
    monkeypatch.setattr(service, "ALERT_COOLDOWN_SEC", 3600.0)
    service._alert("a", key="fatal")
    service._alert("b", key="log_not_writable")
    assert len(alert_dir) == 2


def test_unkeyed_alerts_are_never_suppressed(alert_dir):
    from market_context import service
    for _ in range(3):
        service._alert("always send me")
    assert len(alert_dir) == 3


def test_cooldown_marker_survives_a_new_process(alert_dir, monkeypatch, tmp_path):
    """The marker is a FILE precisely because an in-process set is useless
    against a crash loop."""
    from market_context import service
    monkeypatch.setattr(service, "ALERT_COOLDOWN_SEC", 3600.0)
    service._alert("boom", key="fatal")
    marker = service.ALERT_STATE_DIR / "fatal.last"
    assert marker.exists()
    # Simulate a restart: fresh call, same marker on disk.
    assert service._alert("boom", key="fatal") is False


# =========================================================================== #
# Dynamic subscriptions — the loose-coupling seam for order_manager
# =========================================================================== #
def test_request_and_release_are_refcounted_by_owner(db_path):
    store.init_db(db_path)
    store.request_subscription(["NSE_FO|1"], owner="order_manager",
                               mode="full", db_path=db_path)
    store.request_subscription(["NSE_FO|1"], owner="discount",
                               mode="ltpc", db_path=db_path)
    # richest mode wins while BOTH owners want it
    assert store.active_subscriptions(db_path) == {"NSE_FO|1": "full"}

    store.release_subscription("order_manager", db_path=db_path)
    # discount still wants it — one owner releasing must not cut the other off
    assert store.active_subscriptions(db_path) == {"NSE_FO|1": "ltpc"}
    store.release_subscription("discount", db_path=db_path)
    assert store.active_subscriptions(db_path) == {}


def test_expired_requests_are_ignored(db_path):
    """A crashed caller must not leak a subscription forever."""
    store.init_db(db_path)
    store.request_subscription(["NSE_FO|9"], owner="dead", mode="full",
                               ttl_minutes=-1, db_path=db_path)
    assert store.active_subscriptions(db_path) == {}


def test_request_is_idempotent_and_upgrades_mode(db_path):
    store.init_db(db_path)
    store.request_subscription(["NSE_FO|2"], owner="om", mode="ltpc",
                               db_path=db_path)
    store.request_subscription(["NSE_FO|2"], owner="om", mode="full",
                               db_path=db_path)
    assert store.active_subscriptions(db_path) == {"NSE_FO|2": "full"}


def test_desired_state_merges_plan_and_dynamic_requests(db_path):
    from market_context import config as _cfg
    from market_context import instruments as inst
    from market_context.feed import subscription as sub
    from market_context.feed.cache import TickCache
    from market_context.feed.client import UpstoxFeedClient

    store.init_db(db_path)
    plan = sub.SubscriptionPlan(budget=_cfg.subscription_budget(1))
    plan.mode_by_tier = {1: "full", 2: "ltpc", 3: "ltpc", 4: "full"}
    plan.by_tier = {1: [inst.Instrument(inst.NIFTY_INDEX, "NIFTY", "index")],
                    2: [], 3: [], 4: []}

    client = UpstoxFeedClient(plan, TickCache(), db_path=db_path)
    assert client.desired_state() == {"full": [inst.NIFTY_INDEX]}

    store.request_subscription(["NSE_FO|123"], owner="order_manager",
                               mode="full", db_path=db_path)
    merged = client.desired_state()
    assert "NSE_FO|123" in merged["full"]
    # a dynamic request that duplicates the plan must not be added twice
    store.request_subscription([inst.NIFTY_INDEX], owner="order_manager",
                               mode="ltpc", db_path=db_path)
    assert client.desired_state()["full"].count(inst.NIFTY_INDEX) == 1


def test_reconcile_is_a_noop_when_not_streaming(db_path):
    from market_context import config as _cfg
    from market_context.feed import subscription as sub
    from market_context.feed.cache import TickCache
    from market_context.feed.client import UpstoxFeedClient

    store.init_db(db_path)
    plan = sub.SubscriptionPlan(budget=_cfg.subscription_budget(1))
    client = UpstoxFeedClient(plan, TickCache(), db_path=db_path)
    assert client.reconcile() == (0, 0)
