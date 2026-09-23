"""The scheduler: decides at the right moment, exactly once per day, and keeps protecting positions."""

from datetime import date

import pytest
from conftest import FakeBroker, at, make_bot, signal
from sqlalchemy import select

from app import scheduler
from app.db import SessionLocal
from app.decision_client import SignalError
from app.models import Bot, Order


@pytest.fixture(autouse=True)
def reset_pacing():
    scheduler._last_watch.clear()
    scheduler._retry_after.clear()
    scheduler._last_error.clear()


def _reload(bot_id):
    with SessionLocal() as s:
        return s.get(Bot, bot_id)


@pytest.mark.parametrize("now,expected", [
    (at(19, 29), False),  # 15:29 New York: too early
    (at(19, 30), True),  # 15:30: decision time
    (at(19, 59), True),  # still before the close
    (at(20, 0), False),  # closed
])
def test_decision_window(session, now, expected):
    bot = make_bot(session)
    assert scheduler.decision_due(bot, now) is expected


def test_not_due_twice_on_the_same_day_or_before_rebalance_days(session):
    bot = make_bot(session, last_decision_date=date(2025, 6, 2))
    assert scheduler.decision_due(bot, at(19, 45)) is False
    bot = make_bot(session, rebalance_days=5, last_decision_date=date(2025, 5, 30))  # Friday
    assert scheduler.decision_due(bot, at(19, 45)) is False  # Monday is only 1 trading day later
    bot = make_bot(session, rebalance_days=1, last_decision_date=date(2025, 5, 30))
    assert scheduler.decision_due(bot, at(19, 45)) is True


def test_not_due_on_a_holiday(session):
    bot = make_bot(session)
    christmas_1530 = at(20, 30).replace(month=12, day=25)
    assert scheduler.decision_due(bot, christmas_1530) is False


def test_tick_decides_once_then_leaves_the_bot_alone_for_the_day(session):
    bot = make_bot(session)
    broker = FakeBroker(now=at(19, 30))
    sig = signal("BUY", 0.9)
    scheduler.process_bot(bot.id, at(19, 30), sig, lambda b: broker)
    scheduler.process_bot(bot.id, at(19, 31), sig, lambda b: broker)
    scheduler.process_bot(bot.id, at(19, 45), sig, lambda b: broker)
    assert len(sig.calls) == 1
    assert broker.submitted == [("BUY", 100)]
    assert _reload(bot.id).shares == 100


def test_failed_signal_is_retried_after_a_pause_not_every_tick(session):
    bot = make_bot(session)
    broker = FakeBroker(now=at(19, 30))
    calls = []

    def flaky(bot, as_of):
        calls.append(as_of)
        if len(calls) == 1:
            raise SignalError("timeout")
        return {"action": "HOLD", "confidence": 0.5}

    scheduler.process_bot(bot.id, at(19, 30), flaky, lambda b: broker)
    scheduler.process_bot(bot.id, at(19, 31), flaky, lambda b: broker)  # too soon: skipped
    assert len(calls) == 1
    scheduler.process_bot(bot.id, at(19, 36), flaky, lambda b: broker)  # 5+ minutes later: retried
    assert len(calls) == 2
    assert _reload(bot.id).last_decision_date == date(2025, 6, 2)


def test_paused_bot_makes_no_decisions_but_its_stop_loss_still_fires(session):
    bot = make_bot(session, status="paused", shares=50, cash=5_000, entry_price=100, cost_basis=5_000,
                   stop_price=96, target_price=108)
    broker = FakeBroker(price=90.0, now=at(15, 0))
    sig = signal("BUY")
    scheduler.process_bot(bot.id, at(15, 0), sig, lambda b: broker)
    assert sig.calls == []
    assert broker.submitted == [("SELL", 50)]


def test_nothing_happens_outside_market_hours(session):
    bot = make_bot(session, shares=50, cash=5_000, entry_price=100, cost_basis=5_000, stop_price=96, target_price=108)
    broker = FakeBroker(price=50.0, now=at(22, 0))  # would hit the stop -- but the market is closed
    sig = signal("SELL")
    scheduler.process_bot(bot.id, at(22, 0), sig, lambda b: broker)
    assert sig.calls == [] and broker.submitted == []


def test_one_broken_bot_does_not_stop_the_scheduler(session):
    bot = make_bot(session)

    def broken_broker(b):
        raise RuntimeError("boom")

    scheduler.process_bot(bot.id, at(19, 30), signal(), broken_broker)  # must not raise
    scheduler.process_bot(bot.id, at(19, 31), signal(), broken_broker)  # same error: logged once per hour
    with SessionLocal() as s:
        from app.models import Event

        errors = list(s.scalars(select(Event).where(Event.bot_id == bot.id, Event.level == "error")))
    assert len(errors) == 1 and "boom" in errors[0].message


def test_restart_does_not_decide_twice(session):
    """The 'already decided today' flag lives in the database, so in-memory state loss is harmless."""
    bot = make_bot(session)
    broker = FakeBroker(now=at(19, 30))
    sig = signal("BUY")
    scheduler.process_bot(bot.id, at(19, 30), sig, lambda b: broker)
    scheduler._last_watch.clear()
    scheduler._retry_after.clear()  # simulate a restart
    scheduler.process_bot(bot.id, at(19, 40), sig, lambda b: broker)
    assert len(sig.calls) == 1
    with SessionLocal() as s:
        assert len(list(s.scalars(select(Order)))) == 1


def test_rebalance_days_spaces_out_decisions(session):
    bot = make_bot(session, rebalance_days=2)
    sig = signal("HOLD")
    for day in (2, 3, 4, 5, 6):  # Mon..Fri
        t = at(19, 30, day=day)
        scheduler.process_bot(bot.id, t, sig, lambda b: FakeBroker(now=t))
    assert sig.calls == [date(2025, 6, 2), date(2025, 6, 4), date(2025, 6, 6)]

