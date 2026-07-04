"""
settings_store.py — backing store for the dashboard Settings page.

Lives in DATA_DIR (same shared ./data volume every service already mounts),
so any process in the repo — dashboard, scanners, notifications.py — can
read/write it without a new volume.

Three concerns:
  1. alert_flags     — per-container × alert-type routing (enabled + channel)
  2. global_settings — kill switch
  3. startup_profile  — which services get started by "Apply Startup Profile"
                        in the Settings page (independent of compose `profiles:`,
                        which already gates momentum/directional-iv — this is a
                        manual override on top of that, not a replacement)
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "settings.db"

# Extend as new alert types are added (e.g. Convex Engine gets its own type
# once it leaves observe-only P0).
ALERT_TYPES = [
    "entry",
    "exit",
    "error",
    "heartbeat",
    "orb",
    "bnb_signal",
    "oi_auto_exit",
    "engine_decision",
]

CHANNELS = ["none", "telegram", "discord", "both"]


@contextmanager
def _conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_flags (
                container_name TEXT NOT NULL,
                alert_type     TEXT NOT NULL,
                enabled        INTEGER NOT NULL DEFAULT 1,
                channel        TEXT NOT NULL DEFAULT 'both',
                PRIMARY KEY (container_name, alert_type)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS global_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO global_settings (key, value)
            VALUES ('kill_switch', '0')
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS startup_profile (
                container_name TEXT PRIMARY KEY,
                autostart      INTEGER NOT NULL DEFAULT 1
            )
        """)


def ensure_container_rows(container_names: list[str]):
    """Called at dashboard startup with the live service list parsed from
    docker-compose.prod.yml, so new services appear with sane defaults
    instead of silently missing a row."""
    with _conn() as conn:
        for name in container_names:
            for alert_type in ALERT_TYPES:
                conn.execute("""
                    INSERT OR IGNORE INTO alert_flags
                        (container_name, alert_type, enabled, channel)
                    VALUES (?, ?, 1, 'both')
                """, (name, alert_type))
            conn.execute("""
                INSERT OR IGNORE INTO startup_profile (container_name, autostart)
                VALUES (?, 1)
            """, (name,))


def get_alert_matrix() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT container_name, alert_type, enabled, channel
            FROM alert_flags ORDER BY container_name, alert_type
        """).fetchall()
        return [dict(r) for r in rows]


def set_alert_flag(container_name: str, alert_type: str, enabled: bool, channel: str):
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    with _conn() as conn:
        conn.execute("""
            INSERT INTO alert_flags (container_name, alert_type, enabled, channel)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(container_name, alert_type)
            DO UPDATE SET enabled=excluded.enabled, channel=excluded.channel
        """, (container_name, alert_type, int(enabled), channel))


def should_alert(container_name: str, alert_type: str) -> Optional[str]:
    """Call from notifications.notify_gated() before sending. Returns the
    channel to use ('telegram' | 'discord' | 'both'), or None to suppress.
    Kill switch takes priority over per-flag settings."""
    with _conn() as conn:
        kill = conn.execute(
            "SELECT value FROM global_settings WHERE key='kill_switch'"
        ).fetchone()
        if kill and kill["value"] == "1":
            return None

        row = conn.execute("""
            SELECT enabled, channel FROM alert_flags
            WHERE container_name=? AND alert_type=?
        """, (container_name, alert_type)).fetchone()
        if not row or not row["enabled"]:
            return None
        return row["channel"]


def set_kill_switch(on: bool):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO global_settings (key, value) VALUES ('kill_switch', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (str(int(on)),))


def get_kill_switch() -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM global_settings WHERE key='kill_switch'"
        ).fetchone()
        return bool(row and row["value"] == "1")


def get_startup_profile() -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT container_name, autostart FROM startup_profile").fetchall()
        return {r["container_name"]: bool(r["autostart"]) for r in rows}


def set_startup_profile(container_name: str, autostart: bool):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO startup_profile (container_name, autostart) VALUES (?, ?)
            ON CONFLICT(container_name) DO UPDATE SET autostart=excluded.autostart
        """, (container_name, int(autostart)))
