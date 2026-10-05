"""Scans: every chosen strategy on every chosen symbol, on a practice window, then (for the combinations that
passed) on an unseen exam window after it. Like studying with old exam papers before the real exam: a strategy
that only passes the years it was picked on was memorising, not learning.

Runs in the background, so closing the browser doesn't stop it. Kept in memory like the runs: restarting
backtest-service forgets every scan."""

import asyncio
import os
import uuid
from datetime import datetime, timezone

from .models import RunConfig, ScanConfig
from .runner import RUNS, Run, _execute

# Scans use at most this many runs at once (of backtest-service's MAX_PARALLEL_RUNS), so a backtest or comparison
# started meanwhile still gets a slot instead of waiting hours behind a scan. Shared by every scan.
SCAN_PARALLEL_RUNS = int(os.getenv("SCAN_PARALLEL_RUNS", "3"))
_scan_slots = asyncio.Semaphore(SCAN_PARALLEL_RUNS)

# A summary row carries these result numbers; the full result stays at GET /runs/{id}.
SUMMARY_KEYS = ("total_return_pct", "buy_hold_return_pct", "alpha_vs_buy_hold_pct", "max_drawdown_pct", "sharpe",
                "buy_hold_sharpe", "num_trades", "win_rate_pct", "profit_factor", "avg_trade_pct", "exposure_pct",
                "decision_errors", "breaker_tripped_on")


def _gross(r: dict) -> dict:
    """The closed trades' total won and total lost ($), so the frontend can pool a strategy's trades across symbols
    (a profit factor can't be averaged: it needs the sums)."""
    pnls = [t["pnl"] for t in r.get("trades", []) if "pnl" in t]
    return {"gross_win": round(sum(p for p in pnls if p > 0), 2), "gross_loss": round(-sum(p for p in pnls if p <= 0), 2)}


class Cell:
    """One (symbol, strategy) combination and its two runs."""

    def __init__(self, symbol: str, index: int):
        self.symbol = symbol
        self.index = index  # which of the scan's run settings (one per strategy)
        self.practice: Run | None = None
        self.exam: Run | None = None
        self.exam_note: str | None = None  # why there's no exam run (skipped, no exam period, practice failed)


class Scan:
    def __init__(self, cfg: ScanConfig):
        self.id = uuid.uuid4().hex[:12]
        self.cfg = cfg
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.status = "running"  # running | done | stopped
        self.cells = [Cell(sym, i) for sym in cfg.symbols for i in range(len(cfg.runs))]
        self.tasks: list[asyncio.Task] = []

    def run_config(self, cell: Cell, period) -> RunConfig:
        # Only what the scan set: a cost it left out keeps the symbol's own default (crypto pays Alpaca's crypto fee)
        settings = self.cfg.runs[cell.index].model_dump(exclude_unset=True)
        return RunConfig(symbol=cell.symbol, start=period.start, end=period.end, **settings)

    def stop(self) -> None:
        if self.status == "running":
            self.status = "stopped"
            for t in self.tasks:
                t.cancel()


SCANS: dict[str, Scan] = {}


def passed(run: Run | None) -> bool:
    return bool(run and run.status == "done" and run.result and run.result["verdict"]["grade"] == "paper")


def _new_run(scan: Scan, cell: Cell, period) -> Run:
    run = Run(scan.run_config(cell, period), keep_steps=False)  # listed as "pending" until a slot frees up
    RUNS[run.id] = run
    return run


async def _backtest(run: Run) -> None:
    async with _scan_slots:
        await _execute(run)


async def _cell(scan: Scan, cell: Cell) -> None:
    try:
        cell.practice = _new_run(scan, cell, scan.cfg.practice)
        await _backtest(cell.practice)
        if scan.cfg.exam is None:
            cell.exam_note = "no exam period"
        elif cell.practice.status != "done":
            cell.exam_note = "the practice run failed"
        elif not passed(cell.practice) and not scan.cfg.exam_all:
            cell.exam_note = "skipped: didn't pass practice"
        else:
            cell.exam = _new_run(scan, cell, scan.cfg.exam)
            await _backtest(cell.exam)
    except asyncio.CancelledError:
        for run in (cell.practice, cell.exam):
            if run and run.status in ("pending", "running"):
                run.status = "stopped"
        if cell.exam is None and cell.exam_note is None:
            cell.exam_note = "scan stopped"
        raise


async def _supervise(scan: Scan) -> None:
    await asyncio.gather(*scan.tasks, return_exceptions=True)
    if scan.status == "running":
        scan.status = "done"


def start_scan(cfg: ScanConfig) -> Scan:
    scan = Scan(cfg)
    SCANS[scan.id] = scan
    # One task per combination, in order: they queue for the scan's slots, so practice runs go first and an exam
    # run joins the queue as soon as its practice run passed
    scan.tasks = [asyncio.create_task(_cell(scan, c)) for c in scan.cells]
    asyncio.create_task(_supervise(scan))
    return scan


def _run_view(run: Run | None) -> dict | None:
    if run is None:
        return None
    step = run.last_step
    r = run.result
    return {
        "run_id": run.id,
        "status": run.status,
        "progress": round(step["i"] / step["total"], 3) if step and step.get("total") else 0.0,
        "error": next((e["message"] for e in reversed(run.events) if e["type"] == "error"), None),
        "summary": {k: r.get(k) for k in SUMMARY_KEYS} | _gross(r) if r else None,
        "verdict": r["verdict"] if r else None,
    }


def scan_view(scan: Scan) -> dict:
    def finished(c: Cell) -> bool:  # nothing more will run for this combination
        return c.exam is not None and c.exam.status not in ("pending", "running") or c.exam_note is not None

    return {
        "scan_id": scan.id,
        "created_at": scan.created_at,
        "status": scan.status,
        "config": scan.cfg.model_dump(mode="json"),
        "total": len(scan.cells),
        "finished": sum(finished(c) for c in scan.cells),
        "cells": [
            {"symbol": c.symbol, "strategy": scan.cfg.runs[c.index].strategy, "practice": _run_view(c.practice),
             "exam": _run_view(c.exam), "exam_note": c.exam_note}
            for c in scan.cells
        ],
    }


def scan_summary(scan: Scan) -> dict:
    v = scan_view(scan)
    return {k: v[k] for k in ("scan_id", "created_at", "status", "total", "finished")} | {
        "symbols": scan.cfg.symbols, "strategies": [r.strategy for r in scan.cfg.runs],
        "news": all(r.news for r in scan.cfg.runs)}
