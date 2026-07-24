# -*- coding: utf-8 -*-
"""convex-engine service entry — runs the Conviction Engine every 5 minutes.

P0: observe-only strangler. Emits Decisions to engine_decisions + a single
Telegram digest per cycle (only when something was emitted). No orders.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dtime

from zoneinfo import ZoneInfo

from engine import config as cfg
from engine.pipeline import EnginePipeline

IST = ZoneInfo("Asia/Kolkata")
SCAN_START = dtime(9, 25)
SCAN_END = dtime(15, 5)
CYCLE_SEC = int(os.getenv("ENGINE_CYCLE_SEC", "300"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("engine.runner")


def _digest(result) -> str | None:
    emitted = result["emitted"]
    if not emitted:
        return None
    r = result["regime"]
    lines = [f"🧠 Convex Engine — {r.posture}"
             f" (lean {r.lean or '-'}, VIX {r.vix or '-'},"
             f" breadth {r.breadth_pct or '-'})"]
    for d in emitted[:cfg.TOP_N_ALERT]:
        dot = "🟢" if d.direction == "CE" else "🔴"
        lines.append(f"{dot} {d.grade} {d.score:.0f}  {d.why}")
    nw, nr = len(result["watch"]), len(result["rejected"])
    lines.append(f"\n{nw} on watch · {nr} rejected (journaled) · {cfg.FORMULA_VER}")
    return "\n".join(lines)


def main():
    pipeline = EnginePipeline()
    logger.info("convex-engine up | db=%s | cycle=%ss | formula=%s",
                pipeline.db_path, CYCLE_SEC, cfg.FORMULA_VER)
    while True:
        now = datetime.now(IST)
        if now.weekday() < 5 and SCAN_START <= now.time() <= SCAN_END:
            try:
                result = pipeline.run()
                if cfg.PAPER_MODE != "off":
                    from engine import paper
                    summary = paper.book_emitted(result, pipeline.db_path, now)
                    if summary["booked"]:
                        logger.info("convex paper [%s]: %s (cap left %d)",
                                    summary["mode"], ", ".join(summary["booked"]),
                                    summary["cap_left"])
                text = _digest(result)
                if text and cfg.ALERT:
                    import notifications
                    notifications.notify(text, parse_mode=None)
            except Exception:  # noqa: BLE001 — a bad cycle must not kill the service
                logger.exception("engine cycle failed")
        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    main()
