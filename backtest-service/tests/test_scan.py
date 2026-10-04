"""Scans: practice for every combination, the exam only for the ones that passed, stopping, and the views."""

import asyncio
from datetime import date

import pytest
from pydantic import ValidationError

from app import scan as scan_mod
from app.models import ScanConfig

PRACTICE = {"start": "2022-01-01", "end": "2024-12-31"}
EXAM = {"start": "2025-01-01", "end": "2025-12-31"}


def _cfg(**kw):
    return ScanConfig(**({"symbols": ["qqq", " SPY", "QQQ"], "runs": [{"strategy": "sma_rsi"}, {"strategy": "breakout", "news": False}],
                          "practice": PRACTICE, "exam": EXAM} | kw))


@pytest.fixture
def fake_backtest(monkeypatch):
    """Replace the real backtest: QQQ passes practice with sma_rsi only; every exam run passes. Records each run."""
    ran = []

    async def fake_execute(run):
        await asyncio.sleep(0)
        cfg = run.cfg
        ran.append((cfg.symbol, cfg.strategy, cfg.start.year >= 2025, cfg.news))
        good = cfg.start.year >= 2025 or (cfg.symbol, cfg.strategy) == ("QQQ", "sma_rsi")
        run.result = {"total_return_pct": 10.0 if good else -2.0, "num_trades": 12,
                      "trades": [{"side": "BUY"}, {"side": "SELL", "pnl": 300.0}, {"side": "SELL", "pnl": -100.0}],
                      "verdict": {"grade": "paper" if good else "no", "title": "", "summary": "", "checks": []}}
        run.status = "done"

    monkeypatch.setattr(scan_mod, "_execute", fake_execute)
    return ran


async def _finish(s):
    await asyncio.gather(*s.tasks, return_exceptions=True)
    await asyncio.sleep(0)  # let the supervisor mark it done


def test_symbols_are_cleaned_and_the_exam_must_come_after_practice():
    assert _cfg().symbols == ["QQQ", "SPY"]
    with pytest.raises(ValidationError, match="unseen"):
        _cfg(exam={"start": "2024-06-01", "end": "2025-06-01"})
    with pytest.raises(ValidationError, match="ticker"):
        _cfg(symbols=["not a ticker"])
    with pytest.raises(ValidationError, match="at most once"):
        _cfg(runs=[{"strategy": "sma_rsi"}, {"strategy": "sma_rsi"}])


def test_only_combinations_that_passed_practice_sit_the_exam(fake_backtest):
    async def scenario():
        monkey_slots()
        s = scan_mod.start_scan(_cfg())
        await _finish(s)
        return s

    s = asyncio.run(scenario())
    assert s.status == "done"
    exams = [(sym, strat) for sym, strat, is_exam, _ in fake_backtest if is_exam]
    assert exams == [("QQQ", "sma_rsi")]
    view = scan_mod.scan_view(s)
    assert view["total"] == view["finished"] == 4
    qqq_sma = next(c for c in view["cells"] if c["symbol"] == "QQQ" and c["strategy"] == "sma_rsi")
    assert qqq_sma["practice"]["verdict"]["grade"] == qqq_sma["exam"]["verdict"]["grade"] == "paper"
    assert (qqq_sma["practice"]["summary"]["gross_win"], qqq_sma["practice"]["summary"]["gross_loss"]) == (300.0, 100.0)
    spy = next(c for c in view["cells"] if c["symbol"] == "SPY" and c["strategy"] == "sma_rsi")
    assert spy["exam"] is None and spy["exam_note"] == "skipped: didn't pass practice"
    # each strategy keeps its own settings, news included
    assert {(strat, news) for _, strat, _, news in fake_backtest} == {("sma_rsi", True), ("breakout", False)}


def test_exam_all_examines_every_combination(fake_backtest):
    async def scenario():
        monkey_slots()
        await _finish(scan_mod.start_scan(_cfg(exam_all=True)))

    asyncio.run(scenario())
    assert sum(is_exam for _, _, is_exam, _ in fake_backtest) == 4


def test_without_an_exam_period_only_practice_runs(fake_backtest):
    async def scenario():
        monkey_slots()
        s = scan_mod.start_scan(_cfg(exam=None))
        await _finish(s)
        return s

    s = asyncio.run(scenario())
    assert len(fake_backtest) == 4 and all(c.exam_note == "no exam period" for c in s.cells)


def test_stopping_cancels_what_hasnt_finished(monkeypatch):
    async def slow(run):
        run.status = "running"
        await asyncio.sleep(3600)

    monkeypatch.setattr(scan_mod, "_execute", slow)

    async def scenario():
        monkey_slots()
        s = scan_mod.start_scan(_cfg())
        await asyncio.sleep(0.01)
        s.stop()
        await _finish(s)
        return s

    s = asyncio.run(scenario())
    assert s.status == "stopped"
    assert {c.practice.status for c in s.cells} == {"stopped"}
    assert all(c.exam_note == "scan stopped" for c in s.cells)
    assert scan_mod.scan_view(s)["finished"] == 4


def test_scan_runs_keep_only_their_latest_step():
    """Hundreds of unwatched runs: their day-by-day events would fill the memory."""
    from app.runner import Run
    from app.models import RunConfig

    run = Run(RunConfig(start=date(2025, 1, 1), end=date(2025, 2, 1)), keep_steps=False)

    async def scenario():
        for i in range(3):
            await run.emit({"type": "step", "i": i + 1, "total": 3})
        await run.emit({"type": "done", "result": {}})

    asyncio.run(scenario())
    assert [e["type"] for e in run.events] == ["done"] and run.last_step["i"] == 3


def monkey_slots():
    """A fresh semaphore inside this test's event loop (the module one may be bound to an earlier test's loop)."""
    scan_mod._scan_slots = asyncio.Semaphore(2)
