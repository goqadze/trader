"""Tests for the Run pub/sub bookkeeping."""

import asyncio
from datetime import date, timedelta

import pandas as pd

from app.models import RunConfig
from app.runner import Run, _finished_sessions


def test_emit_numbers_events_and_fans_out():
    run = Run(RunConfig(start=date(2025, 1, 1), end=date(2025, 2, 1)))

    async def scenario():
        q = asyncio.Queue()
        run.subscribers.add(q)
        await run.emit({"type": "start"})
        await run.emit({"type": "done"})
        return q

    q = asyncio.run(scenario())
    # Sequence numbers increment and events are recorded in order
    assert run.events[0]["seq"] == 0
    assert run.events[1]["seq"] == 1
    assert run.events[0]["type"] == "start"
    # The subscriber received the same fanned-out events
    assert q.get_nowait()["seq"] == 0
    assert q.get_nowait()["seq"] == 1


def test_run_has_a_unique_id_and_pending_status():
    a = Run(RunConfig(start=date(2025, 1, 1), end=date(2025, 2, 1)))
    b = Run(RunConfig(start=date(2025, 1, 1), end=date(2025, 2, 1)))
    assert a.id != b.id
    assert a.status == "pending"


def _closes():
    return pd.Series([100.0, 101.0, 102.0], index=[date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)])


def test_todays_bar_is_dropped_while_the_market_is_open():
    """At 11:00 New York, yfinance's row for today holds the 11:00 price, not a close."""
    during = pd.Timestamp("2026-09-25 11:00", tz="America/New_York")
    assert list(_finished_sessions(_closes(), during).index) == [date(2026, 9, 23), date(2026, 9, 24)]


def test_todays_bar_counts_once_the_market_has_closed():
    after = pd.Timestamp("2026-09-25 20:30", tz="UTC")  # 16:30 New York
    assert list(_finished_sessions(_closes(), after).index)[-1] == date(2026, 9, 25)


def test_past_windows_are_untouched():
    later = pd.Timestamp("2026-10-01 10:00", tz="America/New_York")
    assert len(_finished_sessions(_closes(), later)) == 3


def test_simultaneous_runs_share_one_price_download(monkeypatch):
    from app import runner

    calls = []

    def fake_fetch(symbol, start, end):
        calls.append(symbol)
        return _closes()

    monkeypatch.setattr(runner, "_fetch_prices", fake_fetch)
    runner._price_cache.clear()

    async def scenario():
        return await asyncio.gather(*[runner._load_prices("aapl", date(2026, 9, 1), date(2026, 9, 25)) for _ in range(5)])

    out = asyncio.run(scenario())
    assert len(calls) == 1
    assert all(s.equals(out[0]) for s in out)
    runner._price_cache.clear()


def test_only_max_parallel_runs_execute_at_once(monkeypatch):
    from app import runner

    active = {"now": 0, "peak": 0}

    class FakeEngine:
        async def run(self, cfg, prices, decide, emit, bars=None, open_slots=None):
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.01)
            active["now"] -= 1
            return {}

    async def fake_load(symbol, start, end):
        return _closes().to_frame("Close")

    monkeypatch.setattr(runner, "SimplePortfolioEngine", FakeEngine)
    monkeypatch.setattr(runner, "_load_prices", fake_load)

    async def scenario():
        monkeypatch.setattr(runner, "_slots", asyncio.Semaphore(2))
        runs = [runner.start_run(RunConfig(start=date(2026, 9, 1), end=date(2026, 9, 25))) for _ in range(5)]
        while any(r.status in ("pending", "running") for r in runs):
            await asyncio.sleep(0.005)
        return runs

    runs = asyncio.run(scenario())
    assert active["peak"] == 2
    assert all(r.status == "done" for r in runs)
    for r in runs:
        runner.RUNS.pop(r.id, None)



def test_finished_sessions_also_trims_ohlc_bars():
    bars = pd.DataFrame({"Open": [1.0, 2.0, 3.0], "Close": [1.0, 2.0, 3.0]}, index=_closes().index)
    during = pd.Timestamp("2026-09-25 11:00", tz="America/New_York")
    assert list(_finished_sessions(bars, during).index) == [date(2026, 9, 23), date(2026, 9, 24)]


def test_open_slots_split_each_day_at_10am(monkeypatch):
    from app import runner

    start = pd.Timestamp("2026-09-24 09:30", tz="America/New_York")
    idx = pd.DatetimeIndex([start + pd.Timedelta(minutes=30 * k) for k in range(4)]).tz_convert("UTC")  # Yahoo sends UTC
    half_hours = pd.DataFrame({"Open": [100.0, 102.0, 103.0, 101.0], "High": [103.0, 110.0, 104.0, 102.0],
                               "Low": [99.0, 101.0, 97.0, 100.0], "Close": [102.0, 103.0, 101.0, 101.5]}, index=idx)
    monkeypatch.setattr(runner.yf, "download", lambda *a, **k: half_hours)
    slots = runner._fetch_open_slots("AAPL", date(2026, 9, 24), date(2026, 9, 24))
    row = slots.loc[date(2026, 9, 24)]
    assert row["price"] == 102.0  # the 9:30 bar's close = the price at 10:00
    assert (row["high_before"], row["low_before"]) == (103.0, 99.0)
    assert (row["high_after"], row["low_after"]) == (110.0, 97.0)  # 10:00 to the close


def test_open_slots_without_data_explain_the_60_day_limit(monkeypatch):
    import pytest

    from app import runner

    monkeypatch.setattr(runner.yf, "download", lambda *a, **k: pd.DataFrame())
    with pytest.raises(ValueError, match="60 days"):
        runner._fetch_open_slots("AAPL", date(2025, 1, 1), date(2025, 2, 1))


def test_the_api_refuses_an_open_backtest_older_than_the_intraday_data():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.runner import earliest_intraday_start

    first = earliest_intraday_start()
    body = {"start": str(first - timedelta(days=1)), "end": str(first + timedelta(days=20)), "decide_at": "open"}
    r = TestClient(app).post("/runs", json=body)
    assert r.status_code == 422 and "60 days" in r.json()["detail"]
