# -*- coding: utf-8 -*-
"""
backtest_routes.py — FastAPI router for the Backtest page.
Included from dashboard_app.py (mirrors settings_routes.py's pattern).

Runs are executed on a plain background thread (single-user NAS scale —
no need for a task queue). Status/progress live in backtest_results.db so
the frontend can poll GET /api/backtest/runs/{id} regardless of which
request handled the POST.
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backtest import results_store
from backtest.engine import STRATEGIES

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class RunRequest(BaseModel):
    strategy: str
    symbols: list[str] | None = None  # None/omitted -> full F&O universe
    start_date: str
    end_date: str
    params: dict = {}


@router.get("/strategies")
def strategies():
    return {
        "strategies": [
            {"key": key, "label": cfg["label"]} for key, cfg in STRATEGIES.items()
        ]
    }


def _default_universe() -> list[str]:
    import f_o_stocks_list
    try:
        return f_o_stocks_list.get_stock_futures()
    except Exception:
        return []


@router.post("/run")
def run(req: RunRequest):
    if req.strategy not in STRATEGIES:
        raise HTTPException(400, f"Unknown strategy: {req.strategy}")
    if req.start_date > req.end_date:
        raise HTTPException(400, "start_date must be on or before end_date")

    symbols = req.symbols or _default_universe()
    if not symbols:
        raise HTTPException(400, "No symbols to backtest (empty universe)")

    run_id = results_store.create_run(
        req.strategy, symbols, req.start_date, req.end_date, req.params)

    from backtest.engine import run_backtest
    thread = threading.Thread(
        target=run_backtest,
        args=(run_id, req.strategy, symbols, req.start_date, req.end_date, req.params),
        daemon=True,
    )
    thread.start()
    return {"run_id": run_id, "status": "queued"}


@router.get("/runs")
def runs(limit: int = 50):
    return {"runs": results_store.list_runs(limit)}


@router.get("/runs/{run_id}")
def run_detail(run_id: int):
    row = results_store.get_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    return row


@router.get("/runs/{run_id}/trades")
def run_trades(run_id: int):
    if not results_store.get_run(run_id):
        raise HTTPException(404, "run not found")
    return {"trades": results_store.get_trades(run_id)}


@router.delete("/runs/{run_id}")
def run_delete(run_id: int):
    if not results_store.get_run(run_id):
        raise HTTPException(404, "run not found")
    results_store.delete_run(run_id)
    return {"deleted": run_id}
