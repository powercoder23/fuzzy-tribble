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
import time
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


def set_all_alert_flags(enabled: bool, channel: str):
    """Master override — set the routing for EVERY container × alert-type in a
    single statement. Used by the 'Set all' dropdown in the Alert Flag Matrix."""
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    with _conn() as conn:
        conn.execute(
            "UPDATE alert_flags SET enabled=?, channel=?",
            (int(enabled), channel),
        )


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


# ---------------------------------------------------------------------------
# Feature flags — UI-controllable runtime toggles.
#
# Stored in global_settings under "flag:<KEY>" (same shared settings.db every
# container mounts), so a toggle in the Settings page is picked up by the
# scanner/strategy containers on their next decision (within FLAG_CACHE_TTL).
#
# Resolution order for the effective value: DB override -> env var -> registry
# default. Fully backward compatible: with an empty DB the old env/defaults win.
# ---------------------------------------------------------------------------

FEATURE_FLAGS = [
    {"key": "STRATEGY_STRIKE_VIA_DISCOUNT", "type": "bool", "default": False,
     "env": "STRATEGY_STRIKE_VIA_DISCOUNT",
     "label": "Discount strike for strategies",
     "help": "After a strategy confirms direction, trade discount's best-value "
             "strike instead of the strategy's own ATM+offset."},
    {"key": "BREADTH_GATE_MODE", "type": "enum", "values": ["off", "soft", "hard"],
     "default": "off", "env": "BREADTH_GATE_MODE",
     "label": "Market / sector breadth gate",
     "help": "Block CE into a broadly-red tape/sector and PE into green. "
             "soft = log only, hard = drop."},
    {"key": "PMG_GATE_MODE", "type": "enum", "values": ["off", "soft", "hard"],
     "default": "hard", "env": "PMG_GATE_MODE",
     "label": "Pre-market quality gate",
     "help": "IVR / IV-HV / OTM% / PCR / position gates before booking."},
    {"key": "PORTFOLIO_GATE_MODE", "type": "enum", "values": ["off", "soft", "hard"],
     "default": "hard", "env": "PORTFOLIO_GATE_MODE",
     "label": "Concentration gate",
     "help": "Cap positions per direction and per sector."},
    {"key": "AUTO_EXIT_OI_MODE", "type": "enum", "values": ["off", "soft", "hard"],
     "default": "hard", "env": "AUTO_EXIT_OI_MODE",
     "label": "Auto-exit on OI contradiction",
     "help": "Close a position when the latest OI buildup contradicts its side "
             "(thresholds in auto_exit_config: min OI change, strong-only, "
             "winner-skip). hard = exit at market; soft = log only."},
    {"key": "BB_BREAKOUT_ALERTS", "type": "bool", "default": False,
     "env": "BB_BREAKOUT_ALERTS",
     "label": "B&B 15-min breakout alerts",
     "help": "Send the 'BREAKOUTS CONFIRMED' batch alert when 15-min breakouts "
             "are detected (step 2 of 3, before any retest/entry). Off = only "
             "entry-signal and paper-trade alerts."},
    {"key": "MAX_RISK_RUPEES", "type": "float", "default": 1500.0, "env": None,
     "label": "Max risk / trade (Rs)",
     "help": "Skip any paper-trade signal (discount, B&B, Vol-Expansion) whose "
             "1-lot risk (entry-sl)*lot exceeds this. 0 disables the cap."},
    {"key": "MAX_PER_SYMBOL_PER_DAY", "type": "int", "default": 1, "env": None,
     "label": "Max paper trades per symbol / day",
     "help": "One underlying can't take more than this many paper trades a day. "
             "0 disables the cap."},
    {"key": "DAILY_LOSS_GATE_MODE", "type": "enum", "values": ["off", "soft", "hard"],
     "default": "off", "env": "DAILY_LOSS_GATE_MODE",
     "label": "Daily-loss lockout",
     "help": "Stop booking NEW entries once the day's realized+open P&L across the "
             "whole paper book falls to -limit. soft = log only, hard = block. "
             "Open positions are still managed."},
    {"key": "DAILY_LOSS_LIMIT_RUPEES", "type": "float", "default": 5000.0,
     "env": "DAILY_LOSS_LIMIT_RUPEES",
     "label": "Daily-loss limit (Rs)",
     "help": "Book-wide loss floor in rupees. New entries stop when the day's P&L "
             "<= -this value. 0 disables the guard."},
]
_FLAG_BY_KEY = {f["key"]: f for f in FEATURE_FLAGS}

_flag_cache: dict = {}   # key -> (value, expiry_epoch)
_FLAG_TTL = float(os.getenv("FLAG_CACHE_TTL", "30"))


def _coerce(spec, raw):
    t = spec["type"]
    if t == "bool":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if t == "float":
        return float(raw)
    if t == "int":
        return int(float(raw))
    return str(raw)   # str / enum


def get_flag_raw(key: str) -> Optional[str]:
    """Raw DB override for a flag ('flag:<key>' in global_settings), or None."""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT value FROM global_settings WHERE key=?", (f"flag:{key}",)
            ).fetchone()
            return row["value"] if row else None
    except Exception:
        return None


def set_flag(key: str, value) -> None:
    """Persist a flag override. Validates enums against the registry."""
    spec = _FLAG_BY_KEY.get(key)
    if spec and spec["type"] == "enum" and str(value) not in spec["values"]:
        raise ValueError(f"{key} must be one of {spec['values']}")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO global_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"flag:{key}", str(value)),
        )
    _flag_cache.pop(key, None)


def resolve_flag(key: str):
    """Effective flag value: DB override -> env -> registry default, coerced to
    the declared type. Cached ~FLAG_CACHE_TTL. Fail-open to the default."""
    spec = _FLAG_BY_KEY.get(key)
    now = time.time()
    hit = _flag_cache.get(key)
    if hit and hit[1] > now:
        return hit[0]
    try:
        raw = get_flag_raw(key)
        if raw is None and spec and spec.get("env"):
            raw = os.getenv(spec["env"])
        if raw is None:
            val = spec["default"] if spec else None
        else:
            val = _coerce(spec, raw) if spec else raw
    except Exception:
        val = spec["default"] if spec else None
    _flag_cache[key] = (val, now + _FLAG_TTL)
    return val


def flag_bool(key: str) -> bool:
    return bool(resolve_flag(key))


def flag_str(key: str) -> str:
    return str(resolve_flag(key))


def flag_float(key: str) -> float:
    try:
        return float(resolve_flag(key))
    except (TypeError, ValueError):
        return 0.0


def flag_int(key: str) -> int:
    try:
        return int(resolve_flag(key))
    except (TypeError, ValueError):
        return 0


def list_feature_flags() -> list[dict]:
    """For the Settings UI: each flag with its effective value and source."""
    out = []
    for spec in FEATURE_FLAGS:
        db = get_flag_raw(spec["key"])
        env = os.getenv(spec["env"]) if spec.get("env") else None
        source = "db" if db is not None else ("env" if env is not None else "default")
        out.append({
            "key": spec["key"],
            "type": spec["type"],
            "label": spec["label"],
            "help": spec["help"],
            "default": spec["default"],
            "values": spec.get("values"),
            "value": resolve_flag(spec["key"]),
            "source": source,
        })
    return out
