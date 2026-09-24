"""The stop-loss held AT THE BROKER (Alpaca): it must protect the position while this service is down,
and it must never cause a double sell (which would leave a short position)."""

from datetime import timedelta

import pytest
from conftest import FakeBroker, at, make_bot, signal
from sqlalchemy import select

from app import scheduler
from app.brokers.base import BrokerOrder
from app.models import Event, Order
from app.trader import evaluate, open_orders, protect, reconcile, resting_stop, sync_pending, watch

T = at(19, 30)  # Monday 15:30 New York, market open
LATER = at(19, 30, day=3)  # Tuesday's decision time


def _broker(**kw) -> FakeBroker:
    """A broker that holds stop orders, backed by a real account (starts with no shares)."""
    b = FakeBroker(price=100.0, now=T, stops=True, **kw)
    b.held = 0
    return b


def _buy(session, broker, **kw):
    """A bot that bought 100 shares at $100: stop-loss $96, take-profit $108."""
    bot = make_bot(session, **kw)
    evaluate(session, bot, broker, T, "scheduled", signal("BUY", 0.8))
    assert bot.shares == 100
    return bot


def _last_event(session, bot) -> str:
    return session.scalars(select(Event.message).where(Event.bot_id == bot.id).order_by(Event.id.desc())).first()


# --- Placing the stop ------------------------------------------------------------------------------

def test_a_buy_puts_the_stop_loss_at_the_broker(session):
    broker = _broker()
    bot = _buy(session, broker)

    assert broker.stops == [(100, 96.0)]  # all shares, at the stop computed from the actual fill
    stop = resting_stop(session, bot)
    assert (stop.side, stop.order_type, stop.qty, stop.stop_price, stop.reason, stop.status) == \
        ("SELL", "stop", 100, 96.0, "stop-loss", "submitted")
    assert open_orders(session, bot) == []  # resting, not "pending": it blocks nothing
    assert "held at the broker" in _last_event(session, bot)


def test_the_paper_simulator_has_no_broker_stop_and_the_service_checks_it(session):
    broker = FakeBroker(price=100.0, now=T)  # the built-in paper broker can't hold orders
    bot = _buy(session, broker)
    assert resting_stop(session, bot) is None
    broker.price = 95.0
    assert watch(session, bot, broker, T + timedelta(minutes=5)).reason == "stop-loss"


def test_protect_places_one_stop_however_often_it_runs(session):
    broker = _broker()
    bot = _buy(session, broker)
    for minute in range(1, 4):
        protect(session, bot, broker, T + timedelta(minutes=minute))  # every scheduler tick calls it
    assert len(broker.stops) == 1


def test_no_stop_for_shares_the_account_does_not_hold(session):
    """A sell stop for shares that aren't there would open a SHORT position when it triggers."""
    broker = _broker()
    bot = make_bot(session, shares=100, cash=0.0, cost_basis=10_000.0, entry_price=100.0, stop_price=96.0,
                   target_price=108.0)
    broker.held = 0  # e.g. sold by hand in the broker's app
    protect(session, bot, broker, T)
    assert broker.stops == []


def test_a_refused_stop_is_retried_later_not_on_every_tick(session):
    broker = _broker()
    broker.stop_mode = "reject"
    bot = _buy(session, broker)
    assert resting_stop(session, bot) is None
    assert "refused the stop order" in _last_event(session, bot)

    protect(session, bot, broker, T + timedelta(minutes=10))
    assert len(broker.stops) == 1  # not hammering the broker every 30 seconds
    broker.stop_mode = "rest"
    protect(session, bot, broker, T + timedelta(minutes=31))
    assert len(broker.stops) == 2 and resting_stop(session, bot).status == "submitted"


def test_while_the_stop_is_refused_the_service_checks_it_itself(session):
    broker = _broker()
    broker.stop_mode = "reject"
    bot = _buy(session, broker)
    broker.price = 95.0
    assert watch(session, bot, broker, T + timedelta(minutes=5)).reason == "stop-loss"
    assert bot.shares == 0


def test_an_uncertain_stop_submit_is_resolved_then_replaced(session):
    broker = _broker()
    broker.stop_mode = "raise"  # network drop: did the broker get it?
    bot = _buy(session, broker)
    stop = resting_stop(session, bot)
    assert stop.status == "new"  # counted as resting until we know, so no duplicate is sent meanwhile
    broker.stop_mode = "rest"
    protect(session, bot, broker, T + timedelta(minutes=1))
    assert len(broker.stops) == 1

    sync_pending(session, bot, broker, T + timedelta(minutes=3))  # the broker never had it
    assert session.get(Order, stop.id).status == "failed"
    protect(session, bot, broker, T + timedelta(minutes=3))
    assert len(broker.stops) == 2 and resting_stop(session, bot).status == "submitted"


# --- The stop doing its job while the service is away ------------------------------------------

def test_the_stop_fills_at_the_broker_while_the_service_is_down(session):
    broker = _broker()
    bot = _buy(session, broker)
    stop = resting_stop(session, bot)
    # Overnight the price gaps down. The broker's stop becomes a market sell and fills below $96.
    broker.trigger_stop(stop.client_order_id, 100, 95.9)

    sync_pending(session, bot, broker, T + timedelta(hours=18))  # the service comes back the next morning
    stop = session.get(Order, stop.id)
    assert (stop.status, stop.filled_qty, stop.avg_price, stop.pnl) == ("filled", 100, 95.9, -410.0)
    assert (bot.shares, bot.cash, bot.realized_pnl, bot.stop_price) == (0, 9_590.0, -410.0, None)
    assert "stop order executed at the broker" in _last_event(session, bot)
    assert broker.submitted == [("BUY", 100)]  # the service itself sent no sell

    protect(session, bot, broker, T + timedelta(hours=18))
    assert len(broker.stops) == 1  # flat: nothing to protect


def test_the_service_leaves_the_stop_to_the_broker(session):
    """Price below the stop but no fill reported yet: selling here too could sell the position twice."""
    broker = _broker()
    bot = _buy(session, broker)
    broker.price = 95.0
    assert watch(session, bot, broker, T + timedelta(minutes=5)) is None
    assert broker.submitted == [("BUY", 100)] and broker.canceled == []


def test_reconcile_books_a_stop_that_just_filled_instead_of_pausing(session):
    broker = _broker()
    bot = _buy(session, broker)
    broker.trigger_stop(resting_stop(session, bot).client_order_id, 100, 95.9)  # the account is flat now
    assert reconcile(session, bot, broker, T + timedelta(minutes=1)) is True
    assert bot.status == "active" and bot.shares == 0


def test_scheduler_places_the_stop_once_a_slow_buy_fills(session):
    broker = _broker()
    broker.submit_mode = "pending"
    bot = make_bot(session)
    evaluate(session, bot, broker, T, "scheduled", signal("BUY", 0.8))
    assert broker.stops == []  # nothing to protect until the buy has filled

    buy = open_orders(session, bot)[0]
    broker.orders[buy.client_order_id] = BrokerOrder(status="filled", filled_qty=100, avg_price=100.0)
    broker.held = 100
    scheduler._last_watch.clear()
    scheduler.process_bot(bot.id, T + timedelta(minutes=1), signal_fn=signal("HOLD"), broker_factory=lambda b: broker)
    assert broker.stops == [(100, 96.0)]


# --- Every other sell cancels the stop first -------------------------------------------------------

def test_a_sell_signal_cancels_the_stop_before_selling(session):
    broker = _broker()
    bot = _buy(session, broker)
    stop_id = resting_stop(session, bot).client_order_id
    broker.now = LATER

    d = evaluate(session, bot, broker, LATER, "scheduled", signal("SELL", 0.8))
    assert broker.canceled == [stop_id]
    assert broker.submitted == [("BUY", 100), ("SELL", 100)]
    assert d.outcome.startswith("SELL 100 sh filled")
    assert bot.shares == 0 and resting_stop(session, bot) is None
    assert len(broker.stops) == 1  # flat: no new stop


def test_take_profit_is_still_done_by_the_service(session):
    broker = _broker()
    bot = _buy(session, broker)
    broker.price = 108.5
    order = watch(session, bot, broker, T + timedelta(minutes=5))
    assert order.reason == "target" and order.status == "filled"
    assert len(broker.canceled) == 1 and bot.shares == 0


@pytest.mark.parametrize("cancel_mode", ["pending", "raise"])
def test_no_sell_while_the_stop_might_still_be_live(session, cancel_mode):
    broker = _broker()
    bot = _buy(session, broker)
    broker.cancel_mode = cancel_mode  # the broker hasn't confirmed the cancel / can't be reached
    broker.now = LATER

    d = evaluate(session, bot, broker, LATER, "scheduled", signal("SELL", 0.8))
    assert broker.submitted == [("BUY", 100)]  # a second sell could fill too and leave a short position
    assert "SELL not sent" in d.outcome
    assert bot.shares == 100 and resting_stop(session, bot) is not None  # still protected


def test_if_the_stop_fills_first_the_sell_sends_nothing(session):
    broker = _broker()
    bot = _buy(session, broker)
    broker.cancel_mode = "filled"  # the price hit the stop just as we tried to cancel it
    broker.price, broker.now = 95.0, LATER

    d = evaluate(session, bot, broker, LATER, "scheduled", signal("SELL", 0.8))
    assert broker.submitted == [("BUY", 100)]
    assert "sold the position first" in d.outcome
    assert bot.shares == 0 and bot.realized_pnl == -500.0


def test_a_leftover_stop_on_a_flat_bot_is_canceled(session):
    """Defensive: a sell stop with no position behind it would open a short if it ever triggered."""
    broker = _broker()
    bot = _buy(session, broker)
    stop_id = resting_stop(session, bot).client_order_id
    bot.shares, bot.stop_price = 0, None  # records say flat (however that happened)
    session.commit()
    protect(session, bot, broker, T + timedelta(minutes=1))
    assert broker.canceled == [stop_id] and resting_stop(session, bot) is None
