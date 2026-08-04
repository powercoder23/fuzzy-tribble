# SOLE WRITER: market_context/service.py
# All other modules READ ONLY from this database, via market_context.get().
# -*- coding: utf-8 -*-
"""
market_context/store.py — schema and persistence for the Market Context
subsystem.

Deliberately a SEPARATE SQLite file from iv_history.db. That database is
~315 MB, has 19 tables and is written continuously by iv-collector; adding
tick-derived bars would put write contention on the platform's most important
table for no benefit. Separation also gives independent retention, vacuum and
backup policy, and makes the whole subsystem droppable during development
without risking IV history.

Sole-writer contract mirrors collectors/iv_store.py: only the market-context
service writes here. Every consumer reads through market_context.get().

POINT-IN-TIME DISCIPLINE
------------------------
Every table is APPEND-ONLY. Rows are never updated after the fact (the only
exception is mc_feed_gaps, which closes an open gap row with its end time).
Every classification row carries the config_hash that produced it, so when a
threshold changes, research can partition rather than silently mixing two
different definitions of the same state label.

This is not optional polish: a regime label that got retro-corrected would
invalidate every backtest that used it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from market_context import config as cfg
from market_context.contracts import SCHEMA_VERSION

logger = logging.getLogger(__name__)

DB_PATH = cfg.DB_PATH


# --------------------------------------------------------------------------- #
# Connection — same discipline as iv_store.connect()
# --------------------------------------------------------------------------- #
def connect(db_path: str | None = None, read_only: bool = False) -> sqlite3.Connection:
    """Open the market-context database.

    read_only=True opens via the `file:...?mode=ro` URI and issues NO write
    pragmas at all. This is not a nicety — it is a corruption fix.

    THE BUG THIS FIXES (2026-08-04)
    -------------------------------
    The previous version opened read-write and executed
    `PRAGMA journal_mode=WAL` *before* `PRAGMA query_only=ON`. Setting the
    journal mode is a HEADER WRITE: it takes write locks and creates/updates
    the -wal and -shm files. So every "read-only" market_context.get() was in
    fact writing to the database.

    That is harmless when one process on one filesystem does it. It is not
    harmless when the service container (local filesystem on the NAS) holds
    the database in WAL mode while a developer machine touches the same file
    over an SMB mount: WAL's shared-memory index is not coherent across hosts,
    so pages get written at the wrong offsets. The observed result was a file
    whose real SQLite header had moved to byte offset 28672 (7 pages in) —
    `sqlite3.DatabaseError: file is not a database` — and a service that
    crash-looped until the file was quarantined by hand.

    Writers still get WAL (readers must not block the collector). Readers now
    genuinely only read.
    """
    path = db_path or DB_PATH
    timeout = cfg.BUSY_TIMEOUT_MS / 1000.0

    if read_only:
        # No makedirs, no journal pragma, no query_only needed — mode=ro is
        # enforced by SQLite itself and cannot fall back to read-write.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout={int(cfg.BUSY_TIMEOUT_MS)}")
        except sqlite3.Error:
            logger.debug("market_context: busy_timeout not set on reader",
                         exc_info=True)
        return conn

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={int(cfg.BUSY_TIMEOUT_MS)}")
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        logger.debug("market_context: PRAGMA setup failed", exc_info=True)
    return conn


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
_DDL = [
    # ---- meta ------------------------------------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS mc_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,

    # ---- subscription registry ------------------------------------------- #
    # Makes the subscribed set DATA-DRIVEN and auditable rather than a
    # hardcoded list. Rolling the futures expiry each month is an UPDATE,
    # not a deploy.
    """
    CREATE TABLE IF NOT EXISTS mc_instruments (
        instrument_key TEXT PRIMARY KEY,
        symbol         TEXT NOT NULL,
        kind           TEXT NOT NULL,   -- index|futures|equity|vix|sector_index
        underlying     TEXT,
        expiry         TEXT,
        tier           INTEGER NOT NULL,
        mode           TEXT NOT NULL,   -- ltpc|full|option_greeks|full_d30
        lot_size       INTEGER,
        rank_metric    REAL,            -- OI x volume at selection time
        active         INTEGER NOT NULL DEFAULT 1,
        added_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_mc_inst_tier ON mc_instruments(tier, active)",

    # ---- 1-minute bars: the raw research substrate ----------------------- #
    # PRIMARY KEY (instrument_key, ts) + INSERT OR IGNORE makes writes
    # idempotent: a restart mid-minute cannot double-write.
    # WITHOUT ROWID because the PK *is* the natural key.
    """
    CREATE TABLE IF NOT EXISTS mc_bars_1m (
        instrument_key TEXT NOT NULL,
        ts             TEXT NOT NULL,   -- bar START, 'YYYY-MM-DD HH:MM:00' IST
        open           REAL,
        high           REAL,
        low            REAL,
        close          REAL,
        volume         REAL,            -- vtt delta in-bar; NULL if unknowable
        oi             REAL,
        oi_chg         REAL,
        vwap           REAL,            -- Upstox atp
        bid            REAL,
        ask            REAL,
        bid_qty        REAL,
        ask_qty        REAL,
        tick_count     INTEGER,         -- 0 => REST-backfilled, not observed
        PRIMARY KEY (instrument_key, ts)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS ix_mc_bars_ts ON mc_bars_1m(ts)",

    # ---- futures positioning --------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS mc_futures (
        ts               TEXT NOT NULL,
        instrument_key   TEXT NOT NULL,
        symbol           TEXT,
        expiry           TEXT,
        dte              INTEGER,
        ltp              REAL,
        spot             REAL,
        basis            REAL,
        basis_pct        REAL,
        basis_annualised REAL,
        oi               REAL,
        oi_prev_day      REAL,
        oi_chg_pct       REAL,
        price_chg_pct    REAL,
        quadrant         TEXT,
        volume           REAL,
        vwap             REAL,
        day_open         REAL,
        day_high         REAL,
        day_low          REAL,
        prev_close       REAL,
        PRIMARY KEY (ts, instrument_key)
    ) WITHOUT ROWID
    """,

    # ---- breadth ---------------------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS mc_breadth (
        ts                 TEXT PRIMARY KEY,
        universe_size      INTEGER,
        advancing          INTEGER,
        declining          INTEGER,
        unchanged          INTEGER,
        adv_dec_pct        REAL,
        up_volume          REAL,
        down_volume        REAL,
        volume_breadth_pct REAL,
        new_highs          INTEGER,
        new_lows           INTEGER,
        thrust             REAL,
        is_subsample       INTEGER,   -- 1 => large-cap subsample, not market breadth
        sample_quality     REAL       -- fraction of universe with fresh ticks
    )
    """,

    # ---- sector ----------------------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS mc_sector (
        ts           TEXT NOT NULL,
        sector       TEXT NOT NULL,
        ret_pct      REAL,
        rel_strength REAL,
        rs_rank      INTEGER,
        advancing    INTEGER,
        declining    INTEGER,
        breadth_pct  REAL,
        n_names      INTEGER,
        PRIMARY KEY (ts, sector)
    ) WITHOUT ROWID
    """,

    # ---- intraday VIX ----------------------------------------------------- #
    # Complements (does NOT replace) the existing daily vix_daily table in
    # iv_history.db. That collector and table stay exactly as they are.
    """
    CREATE TABLE IF NOT EXISTS mc_vix (
        ts         TEXT PRIMARY KEY,
        ltp        REAL,
        prev_close REAL,
        chg_pct    REAL,
        day_open   REAL,
        day_high   REAL,
        day_low    REAL,
        percentile REAL,
        z_score    REAL,
        vol_of_vol REAL
    )
    """,

    # ---- computed feature vector ------------------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS mc_features (
        ts                  TEXT PRIMARY KEY,
        -- trend
        ef_ratio            REAL,
        ss_slope_pct        REAL,
        mom_z               REAL,
        vwap_position       REAL,
        range_position      REAL,
        orb_state           TEXT,
        prior_day_state     TEXT,
        breadth_divergence  REAL,
        -- volatility
        rv_yz_short         REAL,
        rv_yz_long          REAL,
        rv_ratio            REAL,
        vix_level           REAL,
        vix_percentile      REAL,
        vol_of_vol          REAL,
        vrp                 REAL,
        iv_ts_slope         REAL,
        -- liquidity
        spread_pctile       REAL,
        depth_total         REAL,
        depth_imbalance     REAL,
        -- participation
        volume_ratio        REAL,
        trade_count_ratio   REAL,
        active_names_pct    REAL,
        -- positioning
        nifty_quadrant      TEXT,
        banknifty_quadrant  TEXT,
        basis_ann_nifty     REAL,
        basis_ann_banknifty REAL,
        stock_fut_long_pct  REAL,
        -- breadth
        adv_dec_pct         REAL,
        volume_breadth_pct  REAL,
        thrust              REAL,
        sector_dispersion   REAL,
        implied_corr_proxy  REAL,
        -- point-in-time integrity
        data_quality        REAL,
        missing_inputs      TEXT,   -- JSON array
        config_hash         TEXT,
        config_version      TEXT
    )
    """,

    # ---- axis classifications --------------------------------------------- #
    # Six INDEPENDENT axes. There is deliberately NO composite regime column
    # and NO decision column (bias / size multiplier / exit verdict). The
    # engine describes; strategies decide. See contracts.py.
    """
    CREATE TABLE IF NOT EXISTS mc_regime (
        ts                        TEXT PRIMARY KEY,

        trend_state               TEXT,
        trend_score               REAL,
        trend_confidence          REAL,
        trend_direction           TEXT,
        trend_dwell_minutes       INTEGER,
        trend_transition_prob     REAL,
        trend_event               TEXT,

        volatility_state          TEXT,
        volatility_score          REAL,
        volatility_confidence     REAL,
        volatility_direction      TEXT,
        volatility_dwell_minutes  INTEGER,
        volatility_transition_prob REAL,

        liquidity_state           TEXT,
        liquidity_score           REAL,
        liquidity_confidence      REAL,
        liquidity_direction       TEXT,
        liquidity_dwell_minutes   INTEGER,
        liquidity_transition_prob REAL,

        participation_state       TEXT,
        participation_score       REAL,
        participation_confidence  REAL,
        participation_direction   TEXT,
        participation_dwell_minutes INTEGER,
        participation_transition_prob REAL,

        positioning_state         TEXT,
        positioning_score         REAL,
        positioning_confidence    REAL,
        positioning_direction     TEXT,
        positioning_dwell_minutes INTEGER,
        positioning_transition_prob REAL,

        breadth_state             TEXT,
        breadth_score             REAL,
        breadth_confidence        REAL,
        breadth_direction         TEXT,
        breadth_dwell_minutes     INTEGER,
        breadth_transition_prob   REAL,

        axes_available            TEXT,   -- JSON array of axis names
        axis_inputs               TEXT,   -- JSON {axis: {feature: value}}
        reasons                   TEXT,   -- JSON {axis: [reason, ...]}
        data_quality              REAL,
        config_hash               TEXT,
        config_version            TEXT,
        schema_version            INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_mc_regime_ts ON mc_regime(ts DESC)",

    # ---- dynamic subscription requests (cross-process) --------------------- #
    # order_manager and the strategies run in OTHER containers, so they cannot
    # call the feed client in-process. They register interest here; the
    # market-context service reconciles the socket against this table.
    #
    # Ref-counted by owner: two strategies wanting the same instrument must not
    # be able to unsubscribe each other.
    """
    CREATE TABLE IF NOT EXISTS mc_subscription_requests (
        instrument_key TEXT NOT NULL,
        owner          TEXT NOT NULL,
        mode           TEXT NOT NULL,
        requested_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        expires_at     TEXT,
        active         INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (instrument_key, owner)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS ix_mc_subreq_active ON mc_subscription_requests(active)",

    # ---- live quotes for dynamically-subscribed instruments --------------- #
    # ONE row per instrument_key, overwritten on a throttled cadence (see
    # cache.py's "NEVER WRITE PER TICK" principle — this mirrors that, just at
    # a few seconds instead of a minute, since beating the old 5-minute REST
    # poll for open-position monitoring is the whole point). Sole writer stays
    # market_context/service.py, same as every other mc_* table — only the
    # REQUEST side (mc_subscription_requests above) is cross-process-writable.
    """
    CREATE TABLE IF NOT EXISTS mc_live_quotes (
        instrument_key TEXT PRIMARY KEY,
        ltp            REAL,
        ts             TEXT NOT NULL
    ) WITHOUT ROWID
    """,

    # ---- feed gap register ------------------------------------------------ #
    # Research integrity, not an ops nicety: without this, a 20-minute outage
    # looks in the data like 20 minutes of a perfectly stable regime.
    """
    CREATE TABLE IF NOT EXISTS mc_feed_gaps (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at           TEXT NOT NULL,
        ended_at             TEXT,
        duration_sec         REAL,
        reason               TEXT,
        instruments_affected INTEGER,
        resynced             INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_mc_gaps_started ON mc_feed_gaps(started_at DESC)",
]


#: Substrings that mean "this file is not a usable SQLite database" as opposed
#: to a transient lock or permission problem. Only these trigger quarantine —
#: a busy database must NEVER be thrown away.
_FATAL_DB_ERRORS = (
    "file is not a database",
    "database disk image is malformed",
    "file is encrypted",
    "not a database",
)


def _is_fatal_db_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _FATAL_DB_ERRORS)


def quarantine(db_path: str | None = None) -> str | None:
    """Rename an unusable database (and its -wal/-shm) out of the way.

    Returns the quarantine path, or None if nothing was moved. The file is
    RENAMED, never deleted: a corrupt database can still contain recoverable
    pages, and silently destroying evidence of a corruption event is the wrong
    default for a measurement system.
    """
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = f"{path}.corrupt-{stamp}"
    try:
        os.replace(path, target)
    except OSError:
        logger.exception("market_context: could not quarantine %s", path)
        return None
    # The sidecars belong to the old file; leaving them would corrupt the new
    # database the moment it is created.
    for suffix in ("-wal", "-shm"):
        sidecar = path + suffix
        if os.path.exists(sidecar):
            try:
                os.replace(sidecar, target + suffix)
            except OSError:
                try:
                    os.remove(sidecar)
                except OSError:
                    logger.warning("market_context: stale %s left behind", sidecar)
    logger.error("market_context: quarantined unusable database to %s", target)
    return target


def _apply_schema(db_path: str | None) -> None:
    with closing(connect(db_path)) as conn:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO mc_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT INTO mc_meta(key, value) VALUES('config_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (cfg.CONFIG_VERSION,),
        )
        conn.commit()


def init_db(db_path: str | None = None, on_quarantine=None) -> None:
    """Create every table and index. Idempotent; safe to call on every start.

    If the file turns out not to be a usable database, it is quarantined and
    recreated ONCE rather than being allowed to kill the process. A corrupt
    file previously crash-looped the container every ~60s until someone
    renamed it by hand (2026-08-04); recovery is mechanical, so the service
    does it itself and reports what it did.

    `on_quarantine(path)` is called after a successful recovery so the caller
    can alert. A second failure re-raises: at that point the problem is the
    filesystem, not the file, and retrying would just hide it.
    """
    path = db_path or DB_PATH
    try:
        _apply_schema(db_path)
    except sqlite3.DatabaseError as exc:
        if not _is_fatal_db_error(exc):
            raise
        logger.error("market_context: database at %s is unusable (%s) — "
                     "quarantining and recreating", path, exc)
        moved = quarantine(db_path)
        _apply_schema(db_path)              # second failure propagates
        if on_quarantine is not None:
            try:
                on_quarantine(moved)
            except Exception:
                logger.debug("quarantine callback raised", exc_info=True)
    logger.info("market_context: schema ready at %s (v%d)", path, SCHEMA_VERSION)


def set_meta(key: str, value, db_path: str | None = None) -> None:
    """Upsert a small key/value fact (detected plan tier, probe timestamps)."""
    try:
        with closing(connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO mc_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)),
            )
            conn.commit()
    except sqlite3.Error:
        logger.debug("market_context: set_meta(%s) failed", key, exc_info=True)


def get_meta(key: str, default=None, db_path: str | None = None):
    if not db_exists(db_path):
        return default
    try:
        with closing(connect(db_path, read_only=True)) as conn:
            row = conn.execute("SELECT value FROM mc_meta WHERE key=?",
                               (str(key),)).fetchone()
        return row["value"] if row else default
    except sqlite3.Error:
        return default


def schema_version(db_path: str | None = None) -> int | None:
    try:
        with closing(connect(db_path, read_only=True)) as conn:
            row = conn.execute(
                "SELECT value FROM mc_meta WHERE key='schema_version'"
            ).fetchone()
        return int(row["value"]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Read path — the ONLY thing Phase 0 wires up
# --------------------------------------------------------------------------- #
def db_exists(db_path: str | None = None) -> bool:
    """True when the database file is already present.

    Checked before every read so that a consumer calling get() does NOT
    create an empty market_context.db as a side effect. sqlite3.connect()
    creates the file on open, and Phase 0 must change nothing about a
    strategy container — including its filesystem.
    """
    return os.path.exists(db_path or DB_PATH)


def latest_regime_row(db_path: str | None = None) -> dict | None:
    """Newest mc_regime row as a plain dict, or None.

    Fail-open by design: a missing DB, a missing table (fresh install, or the
    market-context service not deployed yet) and a malformed row all return
    None rather than raising. get() then yields NEUTRAL_CONTEXT.
    """
    if not db_exists(db_path):
        return None
    try:
        with closing(connect(db_path, read_only=True)) as conn:
            row = conn.execute(
                "SELECT * FROM mc_regime ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        # OperationalError covers both 'no such table' and 'unable to open
        # database file' — the normal state before the service is deployed.
        logger.debug("market_context: no regime row available", exc_info=True)
        return None


def latest_features_row(db_path: str | None = None) -> dict | None:
    if not db_exists(db_path):
        return None
    try:
        with closing(connect(db_path, read_only=True)) as conn:
            row = conn.execute(
                "SELECT * FROM mc_features ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def open_feed_gap(started_at: str, reason: str, instruments: int = 0,
                  db_path: str | None = None) -> int | None:
    """Record the start of a feed outage. Returns the gap id."""
    try:
        with closing(connect(db_path)) as conn:
            cur = conn.execute(
                "INSERT INTO mc_feed_gaps(started_at, reason, instruments_affected) "
                "VALUES(?,?,?)",
                (started_at, reason, int(instruments)),
            )
            conn.commit()
            return cur.lastrowid
    except sqlite3.Error:
        logger.debug("market_context: could not open feed gap", exc_info=True)
        return None


def close_feed_gap(gap_id: int, ended_at: str, duration_sec: float,
                   resynced: bool = False, db_path: str | None = None) -> None:
    """Close an open gap row. The one permitted UPDATE in this schema."""
    try:
        with closing(connect(db_path)) as conn:
            conn.execute(
                "UPDATE mc_feed_gaps SET ended_at=?, duration_sec=?, resynced=? "
                "WHERE id=? AND ended_at IS NULL",
                (ended_at, float(duration_sec), 1 if resynced else 0, int(gap_id)),
            )
            conn.commit()
    except sqlite3.Error:
        logger.debug("market_context: could not close feed gap", exc_info=True)


def recent_gap_seconds(db_path: str | None = None, within_minutes: int = 30) -> float:
    """Total seconds of feed outage in the last `within_minutes`.

    Feeds the data_quality term: confidence must be suppressed while the
    picture is still rebuilding after an outage.
    """
    try:
        with closing(connect(db_path, read_only=True)) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(duration_sec), 0) AS s FROM mc_feed_gaps "
                "WHERE started_at >= datetime('now','localtime',?)",
                (f"-{int(within_minutes)} minutes",),
            ).fetchone()
        return float(row["s"]) if row else 0.0
    except (sqlite3.Error, TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
def prune(db_path: str | None = None) -> dict:
    """Drop bar/futures rows past their retention window.

    mc_features and mc_regime are NEVER pruned — ~375 rows/day each, and they
    are the research asset this whole subsystem exists to produce.
    """
    deleted = {}
    try:
        with closing(connect(db_path)) as conn:
            cur = conn.execute(
                "DELETE FROM mc_bars_1m WHERE ts < datetime('now','localtime',?)",
                (f"-{cfg.RETAIN_BARS_DAYS} days",),
            )
            deleted["mc_bars_1m"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM mc_futures WHERE ts < datetime('now','localtime',?)",
                (f"-{cfg.RETAIN_FUTURES_DAYS} days",),
            )
            deleted["mc_futures"] = cur.rowcount
            conn.commit()
            conn.execute("PRAGMA optimize")
    except sqlite3.Error:
        logger.exception("market_context: prune failed (non-fatal)")
    return deleted


def _json_or_none(value):
    """Parse a JSON column, tolerating NULL and malformed content."""
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Dynamic subscriptions — the loose-coupling seam
# --------------------------------------------------------------------------- #
#: Modes ranked weakest -> richest. When several owners want the same
#: instrument the RICHEST wins, so a caller needing depth is never silently
#: downgraded by another that only wanted LTP.
_MODE_RANK = {"ltpc": 1, "option_greeks": 2, "full": 3, "full_d30": 4}


def request_subscription(instrument_keys, owner: str, mode: str = "ltpc",
                         ttl_minutes: int | None = None,
                         db_path: str | None = None) -> int:
    """Register interest in instruments. Safe to call from any container.

    Idempotent per (instrument_key, owner). `ttl_minutes` sets an expiry so a
    crashed caller cannot leak a subscription forever — strongly recommended
    for position-monitoring use, where the natural TTL is the trading session.
    """
    keys = [str(k) for k in (instrument_keys or []) if k]
    if not keys or not owner:
        return 0
    expires = None
    if ttl_minutes:
        expires = (datetime.now() + timedelta(minutes=int(ttl_minutes))
                   ).strftime("%Y-%m-%d %H:%M:%S")
    rows = [(k, str(owner), str(mode), expires) for k in keys]
    try:
        with closing(connect(db_path)) as conn:
            conn.executemany(
                "INSERT INTO mc_subscription_requests "
                "(instrument_key, owner, mode, expires_at, active) "
                "VALUES (?,?,?,?,1) "
                "ON CONFLICT(instrument_key, owner) DO UPDATE SET "
                "  mode=excluded.mode, expires_at=excluded.expires_at,"
                "  requested_at=datetime('now','localtime'), active=1",
                rows)
            conn.commit()
        return len(rows)
    except Exception:
        logger.exception("request_subscription failed (non-fatal)")
        return 0


def release_subscription(owner: str, instrument_keys=None,
                         db_path: str | None = None) -> int:
    """Drop this owner's interest. Other owners' claims are untouched."""
    if not owner:
        return 0
    try:
        with closing(connect(db_path)) as conn:
            if instrument_keys:
                keys = [str(k) for k in instrument_keys if k]
                marks = ",".join("?" * len(keys))
                cur = conn.execute(
                    f"UPDATE mc_subscription_requests SET active=0 "
                    f"WHERE owner=? AND instrument_key IN ({marks})",
                    [str(owner)] + keys)
            else:
                cur = conn.execute(
                    "UPDATE mc_subscription_requests SET active=0 WHERE owner=?",
                    (str(owner),))
            conn.commit()
            return cur.rowcount
    except Exception:
        logger.exception("release_subscription failed (non-fatal)")
        return 0


def active_subscriptions(db_path: str | None = None) -> dict:
    """{instrument_key: mode} for every live request, richest mode winning.

    Expired rows are ignored rather than deleted, so the request history stays
    auditable alongside the trades that caused it.
    """
    if not db_exists(db_path):
        return {}
    try:
        with closing(connect(db_path, read_only=True)) as conn:
            rows = conn.execute(
                "SELECT instrument_key, mode FROM mc_subscription_requests "
                "WHERE active=1 AND (expires_at IS NULL "
                "  OR expires_at > datetime('now','localtime'))"
            ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict = {}
    for row in rows:
        key, mode = row["instrument_key"], str(row["mode"] or "ltpc")
        if _MODE_RANK.get(mode, 0) > _MODE_RANK.get(out.get(key, ""), 0):
            out[key] = mode
    return out


# --------------------------------------------------------------------------- #
# Live quotes (for dynamically-subscribed instruments — open positions etc.)
# --------------------------------------------------------------------------- #
def upsert_live_quotes(rows, db_path: str | None = None) -> int:
    """Batch-overwrite mc_live_quotes. `rows` is [(instrument_key, ltp, ts), ...].

    Called on a throttled cadence with everything currently subscribed, not
    per tick — same reasoning as cache.py's bar aggregation. Never raises."""
    rows = [(str(k), (float(p) if p is not None else None), str(t))
            for k, p, t in rows if k]
    if not rows:
        return 0
    try:
        with closing(connect(db_path)) as conn:
            conn.executemany(
                "INSERT INTO mc_live_quotes (instrument_key, ltp, ts) VALUES (?,?,?) "
                "ON CONFLICT(instrument_key) DO UPDATE SET ltp=excluded.ltp, ts=excluded.ts",
                rows,
            )
            conn.commit()
        return len(rows)
    except sqlite3.Error:
        logger.exception("upsert_live_quotes failed (non-fatal)")
        return 0


def latest_quote(instrument_key: str, db_path: str | None = None) -> dict | None:
    """{ltp, ts} for one instrument, or None. Fail-open: any error -> None,
    same convention as latest_regime_row — a missing quote must never raise
    into a strategy's monitoring loop."""
    if not instrument_key or not db_exists(db_path):
        return None
    try:
        with closing(connect(db_path, read_only=True)) as conn:
            row = conn.execute(
                "SELECT ltp, ts FROM mc_live_quotes WHERE instrument_key=?",
                (str(instrument_key),),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row or row["ltp"] is None:
        return None
    return {"ltp": float(row["ltp"]), "ts": row["ts"]}
