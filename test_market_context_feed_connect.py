# -*- coding: utf-8 -*-
"""
test_market_context_feed_connect.py — regression tests for the 2026-08-05
connect/subscribe race and the reconnect storm it masked.

WHAT HAPPENED
-------------
Every single connect logged, one second apart:

    ERROR | market_context feed: subscribe(ltpc, 131 keys) failed
      ... WebSocketConnectionClosedException: socket is already closed.
    INFO  | market_context feed: connected - full:67, ltpc:131

ROOT CAUSE 1 - the subscribe raced the handshake
------------------------------------------------
`MarketDataStreamerV3.connect()` only STARTS the feeder; the WS handshake
completes on a websocket-client thread and the SDK signals it by calling
`handle_open` -> `subscribe_to_initial_keys()` -> `emit("open")`. The client
called `streamer.subscribe(...)` inline right after `connect()`, so the send
hit a socket that had never opened. "already closed" is websocket-client's
message for "no live socket", not evidence of a close.

Two things made it worse than a retry:

  * the SDK's `subscribe()` sends BEFORE recording the keys in
    `self.subscriptions`, so the failed group never entered the SDK's own
    replay set either - `subscribe_to_initial_keys()` could not recover it.
    Only our `reconcile()` did, up to one service tick later, leaving 131
    breadth instruments off the socket meanwhile.
  * the success log printed `keys_by_mode` (DESIRED state), so it cheerfully
    listed the very keys that had just been rejected.

ROOT CAUSE 2 - a dead socket reset the backoff
----------------------------------------------
`_connect_once()` returned True without ever confirming the socket opened, and
`_run_forever` set `attempt = 0` on that. On 2026-08-04 11:17 the socket was
closing immediately (`socket closed (None, None)`), so every cycle looked like
a fresh success: attempt never exceeded 1, the delay stayed at ~1s, and the
service reconnected roughly once a second for as long as the rejection lasted.
Exponential backoff existed but was unreachable.
"""

import sys
import threading

import pytest

from market_context import config as cfg
from market_context import instruments as inst
from market_context import store
from market_context.feed import client as mc_client
from market_context.feed import subscription as sub
from market_context.feed.cache import TickCache
from market_context.feed.client import UpstoxFeedClient


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "market_context.db")
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(cfg, "DB_PATH", path)
    store.init_db(path)
    return path


# --------------------------------------------------------------------------- #
# A stand-in for MarketDataStreamerV3 that reproduces the SDK's real timing:
# connect() returns immediately, and sending before `open` raises.
# --------------------------------------------------------------------------- #
class SocketNotOpen(Exception):
    """Stands in for websocket's WebSocketConnectionClosedException."""


class FakeStreamer:
    #: open_delay=None means the handshake never lands.
    open_delay: float | None = 0.0
    #: modes whose subscribe() should fail even once the socket is open
    fail_modes: tuple = ()

    def __init__(self, api_client=None, instrumentKeys=(), mode="ltpc"):
        self.subscriptions = {mode: set(instrumentKeys)}
        self.listeners = {}
        self.is_open = False
        self.sent = []            # (mode, [keys]) in call order
        self.auto_reconnect_arg = None
        self._timer = None

    # -- SDK surface -------------------------------------------------------- #
    def on(self, event, listener):
        self.listeners.setdefault(event, []).append(listener)

    def auto_reconnect(self, enable, interval=1, retry_count=5):
        self.auto_reconnect_arg = enable

    def connect(self):
        if self.open_delay is None:
            return                              # handshake never completes
        self._timer = threading.Timer(self.open_delay, self._handle_open)
        self._timer.daemon = True
        self._timer.start()

    def subscribe(self, instrumentKeys, mode):
        if not self.is_open:
            raise SocketNotOpen("socket is already closed.")
        if mode in self.fail_modes:
            raise SocketNotOpen("socket is already closed.")
        self.sent.append((mode, list(instrumentKeys)))
        self.subscriptions.setdefault(mode, set()).update(instrumentKeys)

    def unsubscribe(self, instrumentKeys):
        for keys in self.subscriptions.values():
            keys.difference_update(instrumentKeys)

    def disconnect(self):
        self.is_open = False

    # -- what the real handle_open does ------------------------------------- #
    def _handle_open(self):
        self.is_open = True
        for mode, keys in self.subscriptions.items():
            if keys:
                self.sent.append((mode, sorted(keys)))
        for listener in self.listeners.get("open", []):
            listener()


def _install_fake_sdk(monkeypatch, streamer_cls=FakeStreamer):
    """Put a fake `upstox_client` where _connect_once's local import finds it."""
    module = type(sys)("upstox_client")
    module.Configuration = lambda: type("C", (), {"access_token": None})()
    module.ApiClient = lambda configuration=None: object()
    module.MarketDataStreamerV3 = streamer_cls
    monkeypatch.setitem(sys.modules, "upstox_client", module)
    return module


def _plan(n_full=2, n_ltpc=3):
    plan = sub.SubscriptionPlan(budget=cfg.subscription_budget(1))
    plan.mode_by_tier = {1: "full", 3: "ltpc"}
    plan.by_tier = {
        1: [inst.Instrument(f"NSE_INDEX|F{i}", f"F{i}", "index")
            for i in range(n_full)],
        3: [inst.Instrument(f"NSE_EQ|L{i}", f"L{i}", "equity")
            for i in range(n_ltpc)],
    }
    return plan


def _client(db_path, monkeypatch, plan=None):
    monkeypatch.setattr(UpstoxFeedClient, "_access_token", lambda self: "token")
    return UpstoxFeedClient(plan or _plan(), TickCache(), db_path=db_path)


# =========================================================================== #
# FIX 1 - subscribe only after the socket is actually open
# =========================================================================== #
def test_every_mode_is_subscribed_despite_an_async_handshake(db_path, monkeypatch):
    """THE REGRESSION: with a handshake that lands after connect() returns, the
    ltpc group used to be lost until reconcile() ran a tick later."""
    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(FakeStreamer, "open_delay", 0.05)
    client = _client(db_path, monkeypatch)

    assert client._connect_once() is True
    assert client._subscribed == {
        "NSE_INDEX|F0": "full", "NSE_INDEX|F1": "full",
        "NSE_EQ|L0": "ltpc", "NSE_EQ|L1": "ltpc", "NSE_EQ|L2": "ltpc",
    }


def test_nothing_is_sent_before_the_open_event(db_path, monkeypatch):
    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(FakeStreamer, "open_delay", 0.05)
    client = _client(db_path, monkeypatch)
    client._connect_once()

    # The construction-mode replay is the SDK's own, fired from handle_open;
    # our ltpc subscribe must come after it, never before.
    assert [mode for mode, _ in client._streamer.sent] == ["full", "ltpc"]


def test_connect_fails_when_the_handshake_never_lands(db_path, monkeypatch):
    """Previously this returned True and the caller went to STREAMING on a
    socket that did not exist."""
    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(FakeStreamer, "open_delay", None)
    monkeypatch.setattr(cfg, "WS_CONNECT_TIMEOUT_SEC", 0.1)
    client = _client(db_path, monkeypatch)

    assert client._connect_once() is False
    assert client._subscribed == {}


def test_a_failed_subscribe_leaves_keys_for_reconcile(db_path, monkeypatch):
    """_subscribed is the record of what the socket ACTUALLY carries, so a
    failed group must stay out of it — that is what makes reconcile() re-send
    it. Recording it optimistically would strand those instruments forever."""
    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(FakeStreamer, "open_delay", 0.0)
    monkeypatch.setattr(FakeStreamer, "fail_modes", ("ltpc",))
    client = _client(db_path, monkeypatch)

    assert client._connect_once() is True          # socket IS open
    assert set(client._subscribed) == {"NSE_INDEX|F0", "NSE_INDEX|F1"}

    monkeypatch.setattr(FakeStreamer, "fail_modes", ())
    client.state = mc_client.STATE_STREAMING
    added, removed = client.reconcile()
    assert (added, removed) == (3, 0)
    assert len(client._subscribed) == 5


def test_open_callback_never_raises_into_the_socket_thread(db_path, monkeypatch):
    """An exception escaping on_open tears down the socket the SDK just built."""
    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(FakeStreamer, "open_delay", 0.0)
    monkeypatch.setattr(FakeStreamer, "fail_modes", ("ltpc",))
    client = _client(db_path, monkeypatch)
    client._streamer = FakeStreamer(None, ["NSE_INDEX|F0"], "full")
    client._on_open({"full": ["NSE_INDEX|F0"], "ltpc": ["NSE_EQ|L0"]})
    assert client._socket_open.is_set()


def test_dynamic_requests_are_replayed_on_connect(db_path, monkeypatch):
    """Desired state is declarative: another container's request must survive a
    reconnect without anyone re-issuing it."""
    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(FakeStreamer, "open_delay", 0.0)
    store.request_subscription(["NSE_FO|999"], owner="order_manager",
                               mode="ltpc", db_path=db_path)
    client = _client(db_path, monkeypatch)

    assert client._connect_once() is True
    assert client._subscribed.get("NSE_FO|999") == "ltpc"


# =========================================================================== #
# FIX 2 - a socket that dies instantly is a failed connect
# =========================================================================== #
def _drive_reconnect_loop(client, monkeypatch, cycles=4):
    """Run _run_forever with a socket that dies the moment it opens, recording
    the attempt counter handed to _backoff. No real sleeping: _backoff returns
    0 and stops the loop after `cycles`."""
    attempts = []
    monkeypatch.setattr(mc_client, "market_is_open", lambda *a, **kw: True)
    monkeypatch.setattr(client, "_connect_once",
                        lambda: (client._force_reconnect.set(), True)[1])
    monkeypatch.setattr(client, "_disconnect_streamer", lambda: None)
    monkeypatch.setattr(client, "_open_gap", lambda reason: None)
    monkeypatch.setattr(client, "_close_gap", lambda resync=True: None)

    def fake_backoff(attempt):
        attempts.append(attempt)
        if len(attempts) >= cycles:
            client._stop.set()
        return 0.0

    monkeypatch.setattr(client, "_backoff", fake_backoff)
    client._run_forever()
    return attempts


def test_instant_disconnect_escalates_the_backoff(db_path, monkeypatch):
    """THE REGRESSION: attempt used to reset to 0 on every cycle, pinning the
    delay at ~1s and producing a 1/sec reconnect storm against Upstox."""
    monkeypatch.setattr(cfg, "WS_MIN_UPTIME_SEC", 60.0)
    client = _client(db_path, monkeypatch)
    assert _drive_reconnect_loop(client, monkeypatch) == [1, 2, 3, 4]


def test_a_socket_that_lasted_still_resets_the_backoff(db_path, monkeypatch):
    """The guard must not punish an ordinary end-of-session reconnect."""
    monkeypatch.setattr(cfg, "WS_MIN_UPTIME_SEC", 0.0)
    client = _client(db_path, monkeypatch)
    assert _drive_reconnect_loop(client, monkeypatch) == [1, 1, 1, 1]


def test_backoff_actually_grows_once_attempts_escalate(monkeypatch):
    """Escalation is only worth anything if _backoff responds to it."""
    monkeypatch.setattr(cfg, "RECONNECT_JITTER_PCT", 0.0)
    client = UpstoxFeedClient(_plan(), TickCache())
    assert client._backoff(1) < client._backoff(3) < client._backoff(5)
    assert client._backoff(50) == pytest.approx(cfg.RECONNECT_MAX_SEC)
