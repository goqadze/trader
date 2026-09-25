"""Tests for the Run pub/sub bookkeeping."""

import asyncio
from datetime import date

from app.models import RunConfig
from app.runner import Run


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
