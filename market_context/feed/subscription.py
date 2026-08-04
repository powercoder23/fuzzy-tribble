# -*- coding: utf-8 -*-
"""
market_context/feed/subscription.py — tiered, budget-aware subscription planner.

Builds the set of instruments to stream, ranked by tier, and fits it into
whatever the live plan actually allows. Plan tier is a DEPLOYMENT concern, not
an architectural one (operator decision 2026-08-03): the same code runs on
Standard and Plus and scales itself.

    Standard (1 connection)  ->  NIFTY + BANKNIFTY + India VIX + sector
                                 indices + ~59 liquid stock spots + 20 stock
                                 futures
    Plus     (5 connections) ->  same tiers 1-2, tier 3/4 expand to the full
                                 monitored universe

Tier priority, and why:

  1  NIFTY spot, BANKNIFTY spot, India VIX, NIFTY fut near+next,
     BANKNIFTY fut near+next                                   mode: full
     MANDATORY. The regime engine cannot run without these. `full` because we
     need OHLC + OI + VWAP + depth, and basis needs futures and spot in the
     SAME connection so the two legs are timestamp-aligned.

  2  Sector indices                                            mode: ltpc
     Only returns are needed for relative strength and dispersion, and `ltpc`
     carries the previous close, so % change is computable. Cheapest mode.

  3  Liquid F&O stock spots (breadth)                          mode: ltpc
     Breadth needs direction-of-move only.

  4  Liquid stock futures (aggregate positioning)              mode: full
     Needs OI and basis.

Tier 4 is funded BEFORE tier 3: positioning is mechanical and high-confidence
(price x OI is measured, not forecast), whereas breadth degrades gracefully to
a partial-universe subsample. When it does, `is_subsample` is recorded on
every mc_breadth row and confidence is capped — the flaw in the legacy
breadth.py made visible instead of hidden.

The tier-3/4 constituents come from instruments.liquid_symbols(), which ranks
by RUPEE TURNOVER rather than share count. See that function for why: the
share-count metric this platform uses elsewhere puts IDEA and YESBANK above
RELIANCE, which is fine for choosing option chains to scan and wrong for
measuring market breadth.
"""

from __future__ import annotations

import logging
from contextlib import closing
from dataclasses import dataclass, field

from market_context import config as cfg
from market_context import instruments as inst
from market_context import store

logger = logging.getLogger(__name__)

MODE_FULL = "full"
MODE_LTPC = "ltpc"


@dataclass
class SubscriptionPlan:
    """The resolved instrument set, grouped by subscription mode."""

    budget: "cfg.SubscriptionBudget"
    by_tier: dict[int, list[inst.Instrument]] = field(default_factory=dict)
    mode_by_tier: dict[int, str] = field(default_factory=dict)

    # ---- views the feed client needs ------------------------------------- #
    def keys_by_mode(self) -> dict[str, list[str]]:
        """{mode: [instrument_key]} — what subscribe() is called with.

        The client replays this WHOLE mapping on every reconnect. Declarative
        desired state, never an incremental log: incremental replay is the
        classic source of silent drift where you believe you are subscribed
        and are not.
        """
        out: dict[str, list[str]] = {}
        for tier, items in sorted(self.by_tier.items()):
            mode = self.mode_by_tier.get(tier, MODE_LTPC)
            if not items:
                continue          # never emit an empty mode group
            out.setdefault(mode, []).extend(i.instrument_key for i in items)
        return out

    def all_keys(self) -> list[str]:
        return [i.instrument_key for items in self.by_tier.values() for i in items]

    def tier1_keys(self) -> list[str]:
        """Staleness is judged on tier 1 ONLY.

        A tier-3 mid-cap can legitimately go quiet for a minute at 13:00; NIFTY
        cannot go quiet for five seconds. Watching everything would produce
        false reconnects; watching tier 1 detects a genuinely dead feed.
        """
        return [i.instrument_key for i in self.by_tier.get(1, [])]

    def instrument(self, key: str) -> inst.Instrument | None:
        for items in self.by_tier.values():
            for item in items:
                if item.instrument_key == key:
                    return item
        return None

    def futures(self) -> list[inst.Instrument]:
        return [i for items in self.by_tier.values() for i in items
                if i.kind == "futures"]

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.by_tier.values())

    def summary(self) -> str:
        bits = [f"t{t}={len(v)}" for t, v in sorted(self.by_tier.items())]
        return (f"plan[{self.budget.tier}] {self.total}/{self.budget.capacity} keys "
                f"({', '.join(bits)}) subsample={self.budget.breadth_is_subsample}")


def build_plan(detected_connections: int | None = None,
               instrument_db: str | None = None,
               iv_db: str | None = None) -> SubscriptionPlan:
    """Resolve every tier against the live instrument master and the budget.

    Fails soft at every step: a missing instrument master yields an empty plan,
    a renamed sector index is dropped, a symbol with no listed future is
    skipped. The service logs what it got and runs with it.
    """
    budget = cfg.subscription_budget(detected_connections)
    plan = SubscriptionPlan(budget=budget)
    plan.mode_by_tier = {1: MODE_FULL, 2: MODE_LTPC, 3: MODE_LTPC, 4: MODE_FULL}

    # ---- Tier 1: indices + index futures (near & next) -------------------- #
    tier1 = inst.resolve_indices(inst.TIER1_INDICES, db_path=instrument_db)
    for underlying in inst.TIER1_FUTURES_UNDERLYINGS:
        tier1.extend(inst.futures_chain(underlying, count=2, db_path=instrument_db))
    plan.by_tier[1] = tier1

    # ---- Tier 2: sector indices ------------------------------------------- #
    plan.by_tier[2] = inst.resolve_indices(inst.SECTOR_INDICES,
                                           db_path=instrument_db)[:budget.tier2]

    # ---- Tiers 3 & 4: liquidity-ranked stocks ----------------------------- #
    # One ranked list serves both, so the futures we watch for positioning are
    # the same names we watch for breadth — otherwise the two axes would be
    # describing different universes.
    n_ranked = max(budget.tier3, budget.tier4)
    ranked = inst.liquid_symbols(n_ranked, iv_db=iv_db) if n_ranked > 0 else []
    if not ranked and n_ranked > 0:
        logger.warning("no liquidity ranking available — tiers 3/4 empty this cycle")

    plan.by_tier[4] = inst.stock_futures(ranked[:budget.tier4],
                                         db_path=instrument_db) if budget.tier4 else []
    plan.by_tier[3] = inst.equity_keys(ranked[:budget.tier3],
                                       db_path=instrument_db) if budget.tier3 else []

    _enforce_capacity(plan)
    logger.info("market_context: %s", plan.summary())
    return plan


def _enforce_capacity(plan: SubscriptionPlan) -> None:
    """Trim to the connection capacity, dropping from the LOWEST tier first.

    A tighter-than-expected cap must degrade breadth coverage, never starve
    tier 1 — the regime engine has no fallback if NIFTY or VIX is missing.
    """
    capacity = plan.budget.capacity
    if plan.total <= capacity:
        return
    over = plan.total - capacity
    for tier in (3, 4, 2):
        if over <= 0:
            break
        items = plan.by_tier.get(tier, [])
        drop = min(over, len(items))
        if drop:
            plan.by_tier[tier] = items[: len(items) - drop]
            over -= drop
            logger.warning("capacity %d exceeded — dropped %d instrument(s) from tier %d",
                           capacity, drop, tier)
    if over > 0:
        logger.error("capacity %d cannot fit mandatory tier 1 (%d keys) — "
                     "regime engine will run degraded",
                     capacity, len(plan.by_tier.get(1, [])))


def persist(plan: SubscriptionPlan, db_path: str | None = None) -> int:
    """Write the plan to mc_instruments, deactivating anything no longer in it.

    Makes the subscribed set auditable after the fact: when a regime looks
    wrong in research, you can see exactly which instruments were feeding it
    that day. Rolling the futures expiry each month becomes a data change, not
    a deploy.
    """
    rows = []
    for tier, items in sorted(plan.by_tier.items()):
        mode = plan.mode_by_tier.get(tier, MODE_LTPC)
        rows.extend(item.as_row(tier, mode) for item in items)
    if not rows:
        logger.warning("subscription plan is empty — nothing persisted")
        return 0
    try:
        with closing(store.connect(db_path)) as conn:
            conn.execute("UPDATE mc_instruments SET active = 0")
            conn.executemany(
                "INSERT INTO mc_instruments "
                "(instrument_key, symbol, kind, underlying, expiry, tier, mode,"
                " lot_size, rank_metric, active) "
                "VALUES (?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(instrument_key) DO UPDATE SET "
                "  symbol=excluded.symbol, kind=excluded.kind,"
                "  underlying=excluded.underlying, expiry=excluded.expiry,"
                "  tier=excluded.tier, mode=excluded.mode,"
                "  lot_size=excluded.lot_size, rank_metric=excluded.rank_metric,"
                "  active=1",
                rows,
            )
            conn.commit()
    except Exception:
        logger.exception("could not persist subscription plan (non-fatal)")
        return 0
    return len(rows)
