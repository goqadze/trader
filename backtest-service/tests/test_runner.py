"""Tests for the Run pub/sub bookkeeping."""

import asyncio
from datetime import date

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
