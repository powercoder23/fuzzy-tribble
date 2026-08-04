# -*- coding: utf-8 -*-
"""
market_context/feed/probe.py — detect the account's WebSocket allowance.

Plan tier is a DEPLOYMENT concern, not an architectural one (operator decision
2026-08-03): the same image must run on Standard and Plus and size itself.

    Standard   1 concurrent connection   ->  ~59 breadth constituents
    Plus       5 concurrent connections  ->  full monitored universe

There is no documented REST endpoint that reports the connection allowance, so
this MEASURES it: open throwaway sockets and count how many coexist. Opening
two is enough to separate 1 from 5, so the probe costs two short-lived
connections, once per PLAN_PROBE_TTL_DAYS.

WHY THE RESULT IS CACHED
------------------------
A plan change is a billing event, not an intraday one. Re-probing on every
process start would burn connections during exactly the situation where they
are scarce — a reconnect storm. The detected value is written to mc_meta with
a timestamp and reused until it expires.

FAILURE IS CONSERVATIVE
-----------------------
Any error, timeout, or missing SDK yields `1` (Standard). Under-subscribing
degrades breadth to a subsample, which is recorded and visible. Over-
subscribing would have the socket rejected and the feed silently carry fewer
instruments than the plan claims — a far worse failure for a measurement
system.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from market_context import config as cfg
from market_context import store

logger = logging.getLogger(__name__)

_META_CONNECTIONS = "plan_probe_connections"
_META_PROBED_AT = "plan_probe_at"
_TS_FMT = "%Y-%m-%d %H:%M:%S"

#: A single, always-live instrument is enough to prove a socket works.
_PROBE_KEY = "NSE_INDEX|Nifty 50"

#: Event names across SDK builds; any of these means the socket came up.
_OPEN_EVENTS = ("open", "message")


def _api_client():
    from upstox_token_manager import load_upstox_token
    import upstox_client

    token = load_upstox_token()
    if not token:
        raise RuntimeError("no Upstox access token available")
    configuration = upstox_client.Configuration()
    configuration.access_token = token
    return upstox_client.ApiClient(configuration)


def _open_one(api_client, timeout: float):
    """Open one throwaway streamer. Returns it if it came up, else None.

    The SDK is event-driven, so success is "an open or message event arrived
    within `timeout`", not "connect() returned".
    """
    import upstox_client

    up = threading.Event()
    streamer = upstox_client.MarketDataStreamerV3(
        api_client, [_PROBE_KEY], "ltpc")
    try:
        streamer.auto_reconnect(False, 0, 0)
    except Exception:
        pass                      # older builds may not expose it

    for event in _OPEN_EVENTS:
        try:
            streamer.on(event, lambda *_a, **_kw: up.set())
        except Exception:
            logger.debug("probe: cannot bind '%s' handler", event, exc_info=True)

    try:
        streamer.connect()
    except Exception:
        logger.debug("probe: connect() raised", exc_info=True)
        return None

    if up.wait(timeout):
        return streamer

    # Timed out — tear it down so a half-open socket does not occupy a slot.
    try:
        streamer.disconnect()
    except Exception:
        pass
    return None


def probe_max_connections(max_probe: int | None = None,
                          timeout: float | None = None) -> int:
    """Number of WebSocket connections that can be held CONCURRENTLY.

    Sockets are kept open until the end of the probe on purpose: opening and
    closing them one at a time would return 1 on every plan, since a single
    connection always succeeds. Concurrency is the thing being measured.
    """
    max_probe = int(max_probe if max_probe is not None else cfg.PLAN_PROBE_MAX)
    timeout = float(timeout if timeout is not None else cfg.PLAN_PROBE_TIMEOUT_SEC)
    if max_probe < 1:
        return 1

    try:
        api_client = _api_client()
    except Exception as exc:
        logger.warning("probe: no API client (%s) — assuming Standard", exc)
        return 1

    opened = []
    try:
        for attempt in range(max_probe):
            streamer = _open_one(api_client, timeout)
            if streamer is None:
                logger.info("probe: connection %d/%d did not come up",
                            attempt + 1, max_probe)
                break
            opened.append(streamer)
    except Exception:
        logger.exception("probe: failed (assuming Standard)")
    finally:
        for streamer in opened:
            try:
                streamer.disconnect()
            except Exception:
                logger.debug("probe: teardown raised (ignored)", exc_info=True)

    count = max(len(opened), 1)
    logger.info("probe: %d concurrent connection(s) available", count)
    return count


def _cached(db_path: str | None) -> int | None:
    raw = store.get_meta(_META_CONNECTIONS, db_path=db_path)
    probed_at = store.get_meta(_META_PROBED_AT, db_path=db_path)
    if not raw or not probed_at:
        return None
    try:
        when = datetime.strptime(str(probed_at), _TS_FMT)
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if datetime.now() - when > timedelta(days=cfg.PLAN_PROBE_TTL_DAYS):
        logger.info("probe: cached result from %s expired", probed_at)
        return None
    return value


def detect_connections(db_path: str | None = None, force: bool = False) -> int:
    """Resolve the concurrent-connection allowance for this deployment.

    Order: explicit config -> cached probe -> live probe -> Standard.
    Never raises.
    """
    tier = (cfg.PLAN_TIER or "auto").strip().lower()
    if tier == "standard":
        return cfg.WS_CONNECTIONS_STANDARD
    if tier == "plus":
        return cfg.WS_CONNECTIONS_PLUS
    if not cfg.PLAN_PROBE_ENABLED:
        logger.info("probe: disabled — assuming Standard")
        return cfg.WS_CONNECTIONS_STANDARD

    if not force:
        cached = _cached(db_path)
        if cached is not None:
            logger.info("probe: using cached allowance of %d connection(s)", cached)
            return cached

    count = probe_max_connections()
    # If two coexisted, the plan allows more than Standard — take the full
    # Plus allowance rather than the probe count, since we only probed 2.
    resolved = cfg.WS_CONNECTIONS_PLUS if count > 1 else cfg.WS_CONNECTIONS_STANDARD
    try:
        store.set_meta(_META_CONNECTIONS, resolved, db_path=db_path)
        store.set_meta(_META_PROBED_AT, datetime.now().strftime(_TS_FMT),
                       db_path=db_path)
    except Exception:
        logger.debug("probe: could not cache result", exc_info=True)
    logger.info("probe: resolved plan as %s (%d connection(s))",
                "plus" if resolved > cfg.WS_CONNECTIONS_STANDARD else "standard",
                resolved)
    return resolved
