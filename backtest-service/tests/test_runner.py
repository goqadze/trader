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
        async def run(self, cfg, prices, decide, emit, bars=None, slots=None):
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.01)
            active["now"] -= 1
            return {}

    async def fake_load(symbol, start, end, fetch=None):
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


def _half_hours(day: str, last_start: str = "15:30"):
    """A regular session of 30-minute bars (indexed by start) from 9:30 to last_start; close = 100 + bar number."""
    start = pd.Timestamp(f"{day} 09:30", tz="America/New_York")
    n = int((pd.Timestamp(f"{day} {last_start}", tz="America/New_York") - start) / pd.Timedelta(minutes=30)) + 1
    idx = pd.DatetimeIndex([start + pd.Timedelta(minutes=30 * k) for k in range(n)])
    close = [100.0 + k for k in range(n)]
    return pd.DataFrame({"Open": close, "High": [c + 0.5 for c in close], "Low": [c - 0.5 for c in close], "Close": close}, index=idx)


def test_slots_are_the_bots_two_decision_moments(monkeypatch):
    from app import runner

    monkeypatch.setattr(runner, "_intraday_bars", lambda *a: _half_hours("2026-09-24"))
    row = runner._fetch_slots("AAPL", date(2026, 9, 24), date(2026, 9, 24)).loc[date(2026, 9, 24)]
    assert row["open_price"] == 100.0  # the 9:30 bar's close = the price at 10:00
    assert (row["a_high"], row["a_low"]) == (100.5, 99.5)
    assert (row["close_at"], row["close_price"]) == ("15:30", 111.0)  # the 15:00 bar (#11) closes at 15:30
    assert (row["b_high"], row["b_low"]) == (111.5, 100.5)  # 10:00 to 15:30
    assert (row["c_high"], row["c_low"]) == (112.5, 111.5)  # 15:30 to the close


def test_a_half_day_closes_its_slot_30_minutes_before_the_early_close(monkeypatch):
    from app import runner

    monkeypatch.setattr(runner, "_intraday_bars", lambda *a: _half_hours("2026-11-27", last_start="12:30"))  # 13:00 close
    row = runner._fetch_slots("AAPL", date(2026, 11, 27), date(2026, 11, 27)).loc[date(2026, 11, 27)]
    assert (row["close_at"], row["close_price"]) == ("12:30", 105.0)


def test_slots_without_data_are_a_clear_error(monkeypatch):
    import pytest

    from app import runner

    monkeypatch.setattr(runner, "_intraday_bars", lambda *a: pd.DataFrame(columns=["Open", "High", "Low", "Close"]))
    with pytest.raises(ValueError, match="No 30-minute prices"):
        runner._fetch_slots("AAPL", date(2025, 1, 1), date(2025, 2, 1))


def test_intraday_bars_come_from_decision_service(monkeypatch):
    from app import runner

    class Resp:
        status_code = 200

        def json(self):  # across the switch to summer time: two different UTC offsets
            return [{"t": "2025-03-07T09:30:00-05:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
                    {"t": "2025-03-10T09:30:00-04:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.6, "volume": 10.0}]

    asked = {}
    monkeypatch.setattr(runner.httpx, "get", lambda url, params, timeout: asked.update(url=url, **params) or Resp())
    df = runner._intraday_bars("AAPL", date(2025, 3, 3), date(2025, 3, 3))
    assert asked["url"].endswith("/bars/intraday") and asked["start"] == "2025-03-03"
    assert [str(t) for t in df.index] == ["2025-03-07 09:30:00-05:00", "2025-03-10 09:30:00-04:00"]
