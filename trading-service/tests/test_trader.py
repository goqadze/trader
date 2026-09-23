"""The money logic: entries, exits, accounting, safety checks, and order-state recovery."""

from datetime import timedelta

from conftest import FakeBroker, at, make_bot, signal
from sqlalchemy import select

from app.decision_client import SignalError
from app.models import Decision, EquitySnapshot, Event, Order
from app.trader import evaluate, reconcile, submit_order, sync_pending, watch

T = at(19, 30)  # Monday 15:30 New York, market open


def _orders(session, bot):
    return list(session.scalars(select(Order).where(Order.bot_id == bot.id).order_by(Order.id)))


# --- Entries -------------------------------------------------------------------------------------

def test_buy_uses_position_pct_of_cash_and_sets_stop_target_from_the_fill(session):
    bot = make_bot(session, position_pct=0.5, slippage_pct=0.001, fee_pct=0.001)
    broker = FakeBroker(price=100.0, now=T, slippage_pct=0.001, fee_pct=0.001)

    d = evaluate(session, bot, broker, T, "scheduled", signal("BUY", 0.8))

    # budget 5000 / (100 * 1.001 * 1.001) = 49.9 -> 49 whole shares (same formula as the backtest)
    assert broker.submitted == [("BUY", 49)]
    fill = 100.1
    fee = round(49 * fill * 0.001, 2)
    assert bot.shares == 49
    assert bot.entry_price == fill
    assert bot.cash == round(10_000 - 49 * fill - fee, 2)
    assert bot.cost_basis == round(49 * fill + fee, 2)
    assert bot.stop_price == round(fill * 0.96, 2)
    assert bot.target_price == round(fill * 1.08, 2)
    assert bot.last_decision_date == T.date()
    assert d.outcome.startswith("BUY 49 sh filled")
    assert _orders(session, bot)[0].decision_id == d.id


def test_buy_below_min_confidence_does_nothing(session):
    bot = make_bot(session, min_confidence=0.7)
    broker = FakeBroker(now=T)
    d = evaluate(session, bot, broker, T, "scheduled", signal("BUY", 0.65))
    assert broker.submitted == []
    assert "below minimum" in d.outcome
    assert bot.last_decision_date == T.date()  # it DID decide today; it just chose not to trade


def test_buy_while_already_long_is_ignored(session):
    bot = make_bot(session, shares=10, cash=9_000, cost_basis=1_000, entry_price=100, stop_price=96, target_price=108)
    broker = FakeBroker(now=T)
    d = evaluate(session, bot, broker, T, "scheduled", signal("BUY", 0.9))
    assert broker.submitted == []
    assert "Already holding" in d.outcome


def test_buy_is_capped_by_the_real_accounts_buying_power(session):
    bot = make_bot(session)
    broker = FakeBroker(now=T)
    broker.bp = 1_000.0
    evaluate(session, bot, broker, T, "scheduled", signal("BUY"))
    assert broker.submitted == [("BUY", 10)]


def test_stale_quote_blocks_the_buy(session):
    bot = make_bot(session)
    broker = FakeBroker(now=T, quote_at=T - timedelta(hours=2))
    d = evaluate(session, bot, broker, T, "scheduled", signal("BUY"))
    assert broker.submitted == []
    assert "stale" in d.outcome


# --- Exits ---------------------------------------------------------------------------------------

def _long(session, **kw):
    return make_bot(session, shares=50, cash=5_000.0, entry_price=100.0, cost_basis=5_000.0,
                    stop_price=96.0, target_price=108.0, **kw)


def test_sell_signal_closes_everything_and_books_round_trip_pnl(session):
    bot = _long(session)
    broker = FakeBroker(price=110.0, now=T, fee_pct=0.001)
    d = evaluate(session, bot, broker, T, "scheduled", signal("SELL", 0.8))

    order = _orders(session, bot)[0]
    fee = round(50 * 110 * 0.001, 2)
    assert order.pnl == round(50 * 110 - fee - 5_000, 2)
    assert bot.shares == 0 and bot.entry_price is None and bot.stop_price is None and bot.cost_basis == 0
    assert bot.cash == round(5_000 + 50 * 110 - fee, 2)
    assert bot.realized_pnl == order.pnl
    assert d.outcome.startswith("SELL 50 sh filled")


def test_sell_when_flat_does_nothing(session):
    bot = make_bot(session)
    d = evaluate(session, bot, FakeBroker(now=T), T, "scheduled", signal("SELL", 0.9))
    assert "nothing to sell" in d.outcome


def test_watch_sells_on_stop_loss(session):
    bot = _long(session)
    order = watch(session, bot, FakeBroker(price=95.0, now=T), T)
    assert order.reason == "stop-loss" and order.status == "filled"
    assert bot.shares == 0


def test_watch_sells_on_target(session):
    bot = _long(session)
    order = watch(session, bot, FakeBroker(price=108.5, now=T), T)
    assert order.reason == "target"


def test_watch_inside_the_band_just_records_equity(session):
    bot = _long(session)
    assert watch(session, bot, FakeBroker(price=101.0, now=T), T) is None
    session.commit()
    snap = session.scalar(select(EquitySnapshot).where(EquitySnapshot.bot_id == bot.id))
    assert snap.equity == 5_000 + 50 * 101


def test_partial_sell_releases_a_proportional_slice_of_cost_basis(session):
    bot = _long(session)
    broker = FakeBroker(price=110.0, now=T)
    broker.submit_mode = "pending"
    order = submit_order(session, bot, broker, "SELL", 50, "signal", T)
    # The broker cancels the rest after 20 shares filled
    from app.brokers import BrokerOrder

    broker.orders[order.client_order_id] = BrokerOrder(status="partially_filled", filled_qty=20, avg_price=110.0)
    sync_pending(session, bot, broker, T + timedelta(minutes=1))
    assert order.pnl == round(20 * 110 - 2_000, 2)
    assert bot.shares == 30 and bot.cost_basis == 3_000


# --- Previews and failures -----------------------------------------------------------------------

def test_preview_records_the_signal_but_never_trades(session):
    bot = make_bot(session)
    broker = FakeBroker(now=T)
    d = evaluate(session, bot, broker, T, "preview", signal("BUY", 0.95))
    assert broker.submitted == []
    assert d.action == "BUY" and "Preview" in d.outcome
    assert bot.last_decision_date is None  # doesn't use up today's decision


def test_signal_failure_is_recorded_as_hold_and_retried_later(session):
    bot = make_bot(session)

    def boom(bot, as_of):
        raise SignalError("decision-service 500")

    d = evaluate(session, bot, FakeBroker(now=T), T, "scheduled", boom)
    assert d.action == "HOLD" and "Signal failed" in d.outcome
    assert bot.last_decision_date is None  # not marked decided -> scheduler retries
    assert session.scalar(select(Event).where(Event.level == "error")) is not None


# --- Order state recovery ------------------------------------------------------------------------

def test_order_is_saved_before_it_is_sent_and_recovered_after_a_network_drop(session):
    bot = make_bot(session)
    broker = FakeBroker(now=T)
    broker.submit_mode = "raise"
    order = submit_order(session, bot, broker, "BUY", 10, "signal", T)
    assert order.status == "new" and "network down" in order.error
    assert bot.shares == 0  # nothing booked while we don't know

    # Scenario A: the broker DID receive it and filled it -> next sync books the fill
    from app.brokers import BrokerOrder

    broker.orders[order.client_order_id] = BrokerOrder(status="filled", filled_qty=10, avg_price=100.0)
    sync_pending(session, bot, broker, T + timedelta(minutes=1))
    assert order.status == "filled" and bot.shares == 10


def test_order_the_broker_never_saw_is_marked_failed_after_a_grace_period(session):
    bot = make_bot(session)
    broker = FakeBroker(now=T)
    broker.submit_mode = "raise"
    order = submit_order(session, bot, broker, "BUY", 10, "signal", T)
    sync_pending(session, bot, broker, T + timedelta(seconds=30))
    assert order.status == "new"  # too early to give up
    sync_pending(session, bot, broker, T + timedelta(minutes=3))
    assert order.status == "failed" and bot.shares == 0


def test_pending_order_blocks_new_decisions_until_it_resolves(session):
    bot = make_bot(session)
    broker = FakeBroker(now=T)
    broker.submit_mode = "pending"
    evaluate(session, bot, broker, T, "scheduled", signal("BUY"))
    assert _orders(session, bot)[0].status == "submitted"
    d = evaluate(session, bot, broker, T, "manual", signal("BUY"))
    assert "still pending" in d.outcome
    assert len(broker.submitted) == 1


def test_rejected_order_books_nothing(session):
    bot = make_bot(session)
    broker = FakeBroker(now=T)
    broker.submit_mode = "reject"
    d = evaluate(session, bot, broker, T, "scheduled", signal("BUY"))
    assert bot.shares == 0 and bot.cash == 10_000
    assert "rejected" in d.outcome


# --- Circuit breakers ----------------------------------------------------------------------------

def test_drawdown_breaker_pauses_the_bot_but_keeps_the_position(session):
    bot = _long(session, max_drawdown_pct=0.1, peak_equity=10_000.0)
    bot.stop_price = 50.0  # far away, so only the breaker can act
    watch(session, bot, FakeBroker(price=79.0, now=T), T)  # equity 5000 + 50*79 = 8950 < 9000
    assert bot.status == "paused"
    assert bot.shares == 50


def test_reconcile_pauses_when_the_real_account_disagrees(session):
    bot = _long(session)
    broker = FakeBroker(now=T)
    broker.held = 40  # someone sold 10 shares by hand
    assert reconcile(session, bot, broker, T) is False
    assert bot.status == "paused"
    broker.held = 50
    assert reconcile(session, bot, broker, T) is True


def test_every_decision_is_kept_in_history(session):
    bot = make_bot(session)
    broker = FakeBroker(now=T)
    for action in ("HOLD", "BUY", "SELL"):
        evaluate(session, bot, broker, T, "manual", signal(action))
    rows = list(session.scalars(select(Decision).where(Decision.bot_id == bot.id)))
    assert [r.action for r in rows] == ["HOLD", "BUY", "SELL"]
