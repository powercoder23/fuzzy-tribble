# -*- coding: utf-8 -*-
"""
paper_policy.py — which strategies may write into the shared paper book.

WHY THE SWITCH LIVES AT THE INSERT, NOT IN EACH STRATEGY
---------------------------------------------------------
Paper trading was meant to be momentum-only from 2026-08-04. On 2026-08-05 the
book still took 51 trades from vol-expansion, convex-engine and
break-and-bounce. Two structural reasons, neither fixable per-strategy:

  * `profiles:` in docker-compose is NOT a kill switch. It only decides what a
    fresh `up` starts; a container already running under
    `restart: unless-stopped` keeps trading until it is explicitly stopped.
  * Break & Bounce, directional-IV and IV-seller have no paper on/off flag of
    their own at all. They call OrderManager.submit_external_signal
    unconditionally, so there was nothing to turn off.

The allowlist is therefore enforced where every writer must pass:
`PaperTradeBook.open_trade`, the single INSERT into paper_trades. A strategy
that is not allowed cannot book, regardless of which container is alive, which
image it was built from, or which config it read. Per-strategy flags are still
set (see docker-compose.yml) so blocked strategies don't waste broker calls
reaching a decision that will be refused — but they are the convenience layer,
not the guarantee.

CONFIGURATION
-------------
`PAPER_STRATEGY_ALLOWLIST` — comma-separated, case-insensitive PREFIXES
matched against the signal's `strategy` tag. A prefix covers a strategy's
variants: "Momentum" allows "Momentum-ORB" and "Momentum [hedge]"; "Convex"
would allow "Convex-SONAR_BAND".

  * default: `Momentum`
  * `*`     : allow every strategy (the pre-2026-08-05 behaviour)
  * empty   : allow none — a kill switch fails CLOSED, so a blank or malformed
              value blocks rather than silently opening the book.

NOTE — momentum does not use this book. `momentum_strategy.py` journals to
data/momentum_trades.csv and never calls submit_external_signal, so with the
default allowlist paper_trades.db takes no new rows at all. The "Momentum"
entry is what makes the policy correct the day momentum is wired in, not a
claim that it is writing today.
"""

import logging
import os

logger = logging.getLogger(__name__)

ALLOW_ALL = "*"

DEFAULT_ALLOWLIST = "Momentum"


def _parse(raw) -> list:
    return [part.strip().lower() for part in str(raw).split(",") if part.strip()]


#: Module-level so tests and tooling can override it without touching os.environ.
ALLOWLIST = _parse(os.getenv("PAPER_STRATEGY_ALLOWLIST", DEFAULT_ALLOWLIST))


def allows(strategy) -> bool:
    """True if `strategy` may be booked into the shared paper book."""
    if ALLOW_ALL in ALLOWLIST:
        return True
    name = str(strategy or "").strip().lower()
    if not name:
        return False            # an untagged signal is not "Momentum"
    return any(name.startswith(prefix) for prefix in ALLOWLIST)


def describe() -> str:
    """Human-readable allowlist, for log lines and the EOD summary."""
    if ALLOW_ALL in ALLOWLIST:
        return "all strategies"
    return ", ".join(ALLOWLIST) or "none"
