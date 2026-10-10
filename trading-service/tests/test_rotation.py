"""Momentum rotation bots: what they hold, when they rebalance, how the orders go out, and the API around them."""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from conftest import FakeBroker, at, make_bot
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, rotation, scheduler
from app.brokers import BrokerError, BrokerOrder
from app.brokers import catalog as real_catalog
from app.models import ROTATION, EquitySnapshot, Event, Holding, Order
from app.rotation import MAX_ROUNDS, due, momentum, next_rebalance_at, rebalance, reconcile, step, watch
from app.trader import holdings, sync_pending

T1 = at(19, 30)  # Monday June 2 2025, 15:30 New York
T2 = at(19, 30, day=30)  # Monday June 30: June's last trading day
IDX = pd.bdate_range("2024-01-01", "2025-07-31")
PRICES = {"A": 100.0, "B": 50.0, "C": 20.0, "D": 10.0}  # the broker's quotes (the ranking uses the daily closes)


def closes(**yearly):
    """Daily closes, each symbol growing at its yearly rate."""
    t = np.arange(len(IDX)) / 252
    return pd.DataFrame({s: 100 * (1 + r) ** t for s, r in yearly.items()}, index=IDX)


STANDARD = closes(A=0.6, B=0.3, C=0.1, D=-0.2)  # 12-1 momentum: A, then B, C; D fell


def feed(df):
    """A stand-in for the Yahoo download."""
    return lambda symbols, start: df.loc[pd.Timestamp(start):].reindex(columns=symbols)


def make_rotation(session, **kw):
    return make_bot(session, **{"symbol": "ROTATION", "strategy": ROTATION, "universe": ["A", "B", "C", "D"], "top_n": 2,
                                "lookback_months": 12, "skip_months": 1, "abs_filter": True,
                                "benchmark_prices": dict(PRICES), "benchmark_price": 10_000.0, "last_price": 10_000.0, **kw})


def _hold(session, bot, symbol, shares, cost):
    session.add(Holding(bot_id=bot.id, symbol=symbol, shares=shares, cost_basis=cost, last_price=cost / shares))
    session.commit()


def _held(session, bot):
    return {h.symbol: h.shares for h in holdings(session, bot)}


@pytest.fixture(autouse=True)
def reset_pacing():
    scheduler._last_watch.clear()
    scheduler._retry_after.clear()
    scheduler._last_error.clear()


# --- What it holds -------------------------------------------------------------------------------

def test_momentum_is_the_backtests_and_never_sees_the_latest_month():
    df = closes(A=0.6, B=0.1)
    day = date(2025, 6, 27)
    m = momentum(df, day, 12, 1)
    assert m["A"] == pytest.approx(0.6, abs=0.04) and m["A"] > m["B"]  # about a year at 60%
    changed = df.copy()
    changed.loc["2025-05-28":, "B"] *= 10  # everything after "a month ago"
    assert momentum(changed, day, 12, 1).equals(m)


def test_the_first_rebalance_buys_the_strongest_in_equal_parts(session):
    bot = make_rotation(session)
    broker = FakeBroker(now=T1, prices=PRICES)
    d = rebalance(session, bot, broker, T1, "scheduled", feed(STANDARD))
    assert broker.sent == [("BUY", "A", 50), ("BUY", "B", 100)]  # $5,000 each
    assert _held(session, bot) == {"A": 50, "B": 100}
    assert bot.cash == 0 and bot.rotation_plan is None and bot.last_decision_at == T1
    assert d.action == "ROTATE" and d.reasoning.startswith("Hold A +") and "buy A, B" in d.reasoning
    assert d.outcome == "Trades: buy 50 A; buy 100 B. Done: holding 50 A, 100 B, cash $0.00."
    assert d.steps[1].startswith("A +") and d.steps[1].endswith("← hold") and not d.steps[3].endswith("← hold")
    orders = list(session.scalars(select(Order).order_by(Order.id)))
    assert [(o.symbol, o.reason, o.decision_id) for o in orders] == [("A", "rotation", d.id), ("B", "rotation", d.id)]


def test_only_what_rose_the_empty_slots_stay_in_cash(session):
    bot = make_rotation(session, top_n=4)
    d = rebalance(session, bot, FakeBroker(now=T1, prices=PRICES), T1, "scheduled", feed(STANDARD))
    assert _held(session, bot) == {"A": 25, "B": 50, "C": 125}  # D fell: its $2,500 stays in cash
    assert bot.cash == 2_500 and "1 of 4 slots stay in cash" in d.reasoning
    off = make_rotation(session, top_n=4, abs_filter=False)
    rebalance(session, off, FakeBroker(now=T1, prices=PRICES), T1, "scheduled", feed(STANDARD))
    assert _held(session, off)["D"] == 250  # without the filter it holds the least bad too


def test_at_the_month_end_it_rotates_out_and_trims_what_grew_too_big(session):
    bot = make_rotation(session, cash=0.0, last_decision_at=T1)
    _hold(session, bot, "A", 50, 5_000.0)
    _hold(session, bot, "B", 100, 5_000.0)
    broker = FakeBroker(now=T2, prices={"A": 130.0, "B": 55.0, "C": 20.0})
    d = rebalance(session, bot, broker, T2, "scheduled", feed(closes(A=0.6, B=-0.1, C=0.4, D=-0.2)))  # B turned down
    # Equity 50 x 130 + 100 x 55 = $12,000: $6,000 a slot. A is worth $6,500: trim 4 shares ($520, over 2% of a slot)
    assert broker.sent == [("SELL", "A", 4), ("SELL", "B", 100), ("BUY", "C", 300)]
    sells = {o.symbol: o for o in session.scalars(select(Order).where(Order.side == "SELL"))}
    assert sells["B"].reason == "rotation" and sells["B"].pnl == 500.0  # its whole stay: bought for $5,000, sold for $5,500
    assert sells["A"].reason == "rebalance" and sells["A"].pnl == 120.0  # 4 of its 50 shares: $520 for $400 of cost
    assert _held(session, bot) == {"A": 46, "C": 300} and bot.cash == 20.0
    assert bot.realized_pnl == 620.0 and d.action == "ROTATE" and "sell B" in d.reasoning


def test_the_same_picks_at_about_the_same_weights_trade_nothing(session):
    bot = make_rotation(session)
    broker = FakeBroker(now=T1, prices=PRICES)
    rebalance(session, bot, broker, T1, "scheduled", feed(STANDARD))
    broker.prices, broker.now = {**PRICES, "B": 50.5}, T2  # B drifted 1%: a 1-share trim isn't worth its slippage
    d = rebalance(session, bot, broker, T2, "scheduled", feed(STANDARD))
    assert len(broker.sent) == 2 and d.action == "HOLD" and d.outcome.startswith("No trades needed.")


def test_a_preview_shows_the_trades_without_sending_them(session):
    bot = make_rotation(session)
    broker = FakeBroker(now=T1, prices=PRICES)
    d = rebalance(session, bot, broker, T1, "preview", feed(STANDARD))
    assert broker.sent == [] and bot.last_decision_at is None
    assert d.outcome == "Preview only (market closed or bot paused): no order sent. It would buy 50 A; buy 100 B."


def test_no_trade_on_a_failed_or_incomplete_download(session):
    bot = make_rotation(session)
    broker = FakeBroker(now=T1, prices=PRICES)

    def down(symbols, start):
        raise BrokerError("Yahoo down")

    d = rebalance(session, bot, broker, T1, "scheduled", down)
    assert d.outcome == "Ranking failed, no trade: Yahoo down" and bot.last_decision_at is None  # retried later
    d = rebalance(session, bot, broker, T1, "scheduled", feed(STANDARD.assign(A=np.nan, B=np.nan, C=np.nan)))
    assert "incomplete prices" in d.outcome and broker.sent == []


# --- When it rebalances --------------------------------------------------------------------------

def test_it_rebalances_on_its_first_day_then_at_each_month_end(session):
    bot = make_rotation(session)
    assert not due(bot, at(19, 29)) and due(bot, at(19, 30))
    assert next_rebalance_at(bot, at(14, 0)) == at(19, 30)  # never rebalanced: today's close
    bot.last_decision_at = at(19, 31)
    assert not due(bot, at(19, 45)) and not due(bot, at(19, 30, day=27))  # Friday June 27 isn't the month's last day
    assert due(bot, T2)
    assert next_rebalance_at(bot, at(19, 45)) == T2


def test_a_missed_month_end_is_made_up_at_the_next_close(session):
    bot = make_rotation(session, last_decision_at=datetime(2025, 5, 15, 19, 30, tzinfo=timezone.utc))
    assert due(bot, T1)  # May 30 was missed (service down): Monday June 2 catches up
    assert next_rebalance_at(make_rotation(session, status="paused"), at(14, 0)) is None


def test_the_scheduler_rebalances_once_and_a_paused_bot_not_at_all(session):
    bot = make_rotation(session)
    broker = FakeBroker(prices=PRICES)
    for t in (at(19, 30), at(19, 31), at(19, 45)):
        broker.now = t
        scheduler.process_bot(bot.id, t, broker_factory=lambda b: broker, closes_fn=feed(STANDARD))
    assert broker.sent == [("BUY", "A", 50), ("BUY", "B", 100)]
    paused = make_rotation(session, status="paused")
    broker.now = T2
    scheduler.process_bot(paused.id, T2, broker_factory=lambda b: broker, closes_fn=feed(STANDARD))
    assert len(broker.sent) == 2


# --- How the orders go out -----------------------------------------------------------------------

def test_buys_wait_until_the_sells_have_filled(session):
    bot = make_rotation(session, cash=0.0, last_decision_at=T1)
    _hold(session, bot, "A", 50, 5_000.0)
    _hold(session, bot, "B", 100, 5_000.0)
    broker = FakeBroker(now=T2, prices={"A": 100.0, "B": 50.0, "C": 20.0})
    broker.submit_mode = "pending"
    d = rebalance(session, bot, broker, T2, "scheduled", feed(closes(A=0.6, B=-0.1, C=0.4, D=-0.2)))
    assert broker.sent == [("SELL", "B", 100)] and bot.rotation_plan is not None
    later = T2 + timedelta(minutes=1)
    step(session, bot, broker, later)
    assert len(broker.sent) == 1  # still filling: no buy yet
    sell = session.scalar(select(Order).where(Order.side == "SELL"))
    broker.orders[sell.client_order_id] = BrokerOrder(status="filled", filled_qty=100, avg_price=50.0)
    sync_pending(session, bot, broker, later)
    broker.submit_mode = "fill"
    step(session, bot, broker, later)
    assert broker.sent[-1] == ("BUY", "C", 250) and bot.rotation_plan is None
    assert "Done: holding 50 A, 250 C" in d.outcome


def test_a_rebalance_gives_up_after_refused_orders(session):
    bot = make_rotation(session)
    broker = FakeBroker(now=T1, prices=PRICES)
    broker.submit_mode = "reject"
    rebalance(session, bot, broker, T1, "scheduled", feed(STANDARD))
    assert len(broker.sent) == MAX_ROUNDS * 2 and bot.rotation_plan is None
    errors = [e.message for e in session.scalars(select(Event).where(Event.level == "error"))]
    assert any(m.startswith(f"Rebalance stopped after {MAX_ROUNDS} rounds") for m in errors)


# --- Watching --------------------------------------------------------------------------------------

def test_watch_values_the_holdings_and_the_universe_and_the_breaker_pauses(session):
    bot = make_rotation(session, cash=0.0, last_decision_at=T1)
    _hold(session, bot, "A", 50, 5_000.0)
    _hold(session, bot, "B", 100, 5_000.0)
    still = feed(pd.DataFrame({**PRICES}, index=IDX))  # the universe as a whole hasn't moved
    watch(session, bot, FakeBroker(now=T1, prices={"A": 80.0, "B": 35.0}), T1, still)
    # 50 x 80 + 100 x 35 = $7,500: 25% below the $10,000 peak, past the 20% breaker
    assert bot.status == "paused" and bot.last_price == 10_000.0
    snap = session.scalar(select(EquitySnapshot))
    assert snap.equity == 7_500 and snap.price == 10_000
    assert "no more rebalances" in session.scalar(select(Event).where(Event.kind == "risk")).message


def test_a_real_account_that_disagrees_pauses_the_bot(session):
    bot = make_rotation(session, cash=5_000.0)
    _hold(session, bot, "A", 50, 5_000.0)
    broker = FakeBroker(now=T1, prices=PRICES)
    broker.held_by = {"A": 50}
    assert reconcile(session, bot, broker, T1) is True
    broker.held_by = {"A": 40}
    assert reconcile(session, bot, broker, T1) is False and bot.status == "paused"


# --- Whole shares or fractions --------------------------------------------------------------------

def test_whole_shares_leave_a_slot_under_one_share_in_cash(session):
    bot = make_rotation(session, allocated_cash=150.0, cash=150.0, peak_equity=150.0)  # $75 slots; A costs $100
    broker = FakeBroker(now=T1, prices=PRICES)
    d = rebalance(session, bot, broker, T1, "scheduled", feed(STANDARD))
    assert broker.sent == [("BUY", "B", 1)] and _held(session, bot) == {"B": 1} and bot.cash == 100.0
    assert "less than one share of A: that money stays in cash (fractional shares would buy it)." in d.reasoning


def test_fractional_shares_buy_each_slot_exactly(session):
    bot = make_rotation(session, allocated_cash=150.0, cash=150.0, peak_equity=150.0, fractional=True)
    broker = FakeBroker(now=T1, prices=PRICES)
    d = rebalance(session, bot, broker, T1, "scheduled", feed(STANDARD))
    assert broker.sent == [("BUY", "A", 0.75), ("BUY", "B", 1.5)] and _held(session, bot) == {"A": 0.75, "B": 1.5}
    assert bot.cash == pytest.approx(0) and d.outcome.endswith("Done: holding 0.75 A, 1.5 B, cash $0.00.")
    assert d.steps[-1] == "Fractions of a share" and "less than one share" not in d.reasoning
    # A month later B turned down and C came in: B's 1.5 shares are sold whole, C bought in fractions
    broker.prices, broker.now = {"A": 120.0, "B": 55.0, "C": 20.0}, T2
    rebalance(session, bot, broker, T2, "scheduled", feed(closes(A=0.6, B=-0.1, C=0.4, D=-0.2)))
    # Equity 0.75 x 120 + 1.5 x 55 = $172.50: $86.25 a slot. A is worth $90: a $3.75 trim, over 2% of a slot
    assert broker.sent[2:] == [("SELL", "A", 0.031250), ("SELL", "B", 1.5), ("BUY", "C", 4.3125)]
    assert _held(session, bot) == {"A": 0.71875, "C": 4.3125}


def test_a_symbol_the_broker_cant_split_is_bought_in_whole_shares(session):
    bot = make_rotation(session, allocated_cash=250.0, cash=250.0, peak_equity=250.0, fractional=True)  # $125 slots
    broker = FakeBroker(now=T1, prices=PRICES)
    broker.whole_only = {"A"}
    d = rebalance(session, bot, broker, T1, "scheduled", feed(STANDARD))
    assert broker.sent == [("BUY", "A", 1), ("BUY", "B", 2.5)] and bot.cash == 25.0
    assert d.steps[-1] == "Fractions of a share (A can't be bought in fractions at fake: whole shares)"
    assert bot.rotation_plan is None


def test_a_fractional_trade_under_a_dollar_isnt_sent(session):
    bot = make_rotation(session, allocated_cash=1.5, cash=1.5, peak_equity=1.5, fractional=True)  # $0.75 slots
    broker = FakeBroker(now=T1, prices=PRICES)
    d = rebalance(session, bot, broker, T1, "scheduled", feed(STANDARD))
    assert broker.sent == [] and "less than one share of A, B: that money stays in cash." in d.reasoning


# --- The API ---------------------------------------------------------------------------------------

CLOSED, OPEN = at(23, 0), at(15, 0)  # Monday 19:00 and 11:00 New York
BODY = {"universe": ["a", "b", "c", "d"], "top_n": 2, "allocated_cash": 10_000, "slippage_pct": 0}  # exact share counts


@pytest.fixture
def api(monkeypatch):
    clock = {"t": CLOSED}
    broker = FakeBroker(now=CLOSED, prices=PRICES)
    monkeypatch.setattr(main, "utcnow", lambda: clock["t"])
    monkeypatch.setattr(main, "get_broker", lambda bot: broker)
    monkeypatch.setattr(rotation, "yahoo_closes", feed(STANDARD))
    with TestClient(main.app) as c:
        yield c, clock, broker


def test_create_a_rotation_bot_and_trade_it_through_the_api(api):
    c, clock, broker = api
    r = c.post("/bots/rotation", json=BODY)
    assert r.status_code == 201, r.text
    bot = r.json()
    assert bot["strategy"] == "momentum_rotation" and bot["universe"] == ["A", "B", "C", "D"]
    assert bot["name"] == "Momentum top 2 of 4" and bot["equity"] == 10_000 and bot["holdings"] == []
    assert bot["buy_hold_return_pct"] == 0.0 and bot["next_decision_at"] is not None

    d = c.post(f"/bots/{bot['id']}/run").json()  # closed: a preview
    assert d["kind"] == "preview" and d["action"] == "ROTATE" and "buy 50 A; buy 100 B" in d["outcome"]
    clock["t"] = broker.now = OPEN
    d = c.post(f"/bots/{bot['id']}/run").json()
    assert d["kind"] == "manual" and "Done: holding 50 A, 100 B" in d["outcome"]

    detail = c.get(f"/bots/{bot['id']}").json()
    assert [(h["symbol"], h["shares"], h["weight_pct"]) for h in detail["holdings"]] == [("A", 50, 50.0), ("B", 100, 50.0)]
    assert detail["equity"] == 10_000 and detail["rebalancing"] is False
    assert {o["symbol"] for o in c.get(f"/bots/{bot['id']}/orders").json()} == {"A", "B"}

    assert c.post(f"/bots/{bot['id']}/archive").status_code == 409  # still holding
    sold = c.post(f"/bots/{bot['id']}/close").json()
    assert [(o["side"], o["symbol"], o["qty"]) for o in sold] == [("SELL", "A", 50), ("SELL", "B", 100)]
    assert c.post(f"/bots/{bot['id']}/archive").json()["status"] == "archived"


def test_money_added_to_a_rotation_bot_goes_in_at_its_next_rebalance(api):
    c, clock, broker = api
    bot = c.post("/bots/rotation", json=BODY).json()
    clock["t"] = broker.now = OPEN
    c.post(f"/bots/{bot['id']}/run")  # 50 A, 100 B: half of 10,000 each
    r = c.post(f"/bots/{bot['id']}/capital", json={"amount": 10_000, "top_up": True})
    assert r.status_code == 422 and "next rebalance" in r.text
    r = c.post(f"/bots/{bot['id']}/capital", json={"amount": 10_000})
    assert r.status_code == 200 and (r.json()["capital"], r.json()["return_pct"], r.json()["buy_hold_return_pct"]) == (20_000, 0.0, 0.0)
    assert c.get(f"/bots/{bot['id']}/events").json()[0]["message"].endswith(
        "Invested at its next rebalance: each pick 1/2 of the equity.")
    d = c.post(f"/bots/{bot['id']}/run").json()
    assert "Done: holding 100 A, 200 B" in d["outcome"]  # each pick topped up to half of 20,000


def test_rotation_bots_are_checked_and_paper_only(api, monkeypatch):
    c, _, _ = api
    assert c.post("/bots/rotation", json={**BODY, "top_n": 5}).status_code == 422
    assert c.post("/bots/rotation", json={**BODY, "universe": ["A", "not one"]}).status_code == 422
    r = c.post("/bots/rotation", json={**BODY, "universe": ["A", "BTC-USD"]})
    assert r.status_code == 422 and "can't trade crypto yet: BTC-USD" in r.text
    r = c.post("/bots/rotation", json={**BODY, "universe": ["A", "B", "ZZZZ"]})
    assert r.status_code == 422 and "no prices for ZZZZ" in r.text
    monkeypatch.setattr(main, "catalog", lambda: [{**b, "available": True} for b in real_catalog()])
    r = c.post("/bots/rotation", json={**BODY, "broker": "alpaca-live"})
    assert r.status_code == 422 and "paper accounts only" in r.text


def test_one_bot_per_symbol_on_a_real_account_counts_the_whole_universe(api, monkeypatch):
    c, _, _ = api
    monkeypatch.setattr(main, "catalog", lambda: [{**b, "available": True} for b in real_catalog()])
    assert c.post("/bots", json={"symbol": "C", "broker": "alpaca-paper"}).status_code == 201
    r = c.post("/bots/rotation", json={**BODY, "broker": "alpaca-paper"})
    assert r.status_code == 409 and "already trades C" in r.text
    assert c.post("/bots/rotation", json={**BODY, "universe": ["A", "B"], "broker": "alpaca-paper"}).status_code == 201
    assert c.post("/bots", json={"symbol": "B", "broker": "alpaca-paper"}).status_code == 409
    assert c.post("/bots/rotation", json=BODY).status_code == 201  # the simulator keeps every bot apart


def test_a_rotation_bot_changes_only_its_name_costs_and_breaker(api):
    c, _, _ = api
    bot = c.post("/bots/rotation", json=BODY).json()
    r = c.patch(f"/bots/{bot['id']}", json={"strategy": "momentum"})
    assert r.status_code == 422 and "create a new bot" in r.text
    r = c.patch(f"/bots/{bot['id']}", json={"name": "Sectors", "max_drawdown_pct": 0.15})
    assert r.status_code == 200 and r.json()["name"] == "Sectors"
    assert bot["fractional"] is True  # the default, like the backtest
    r = c.patch(f"/bots/{bot['id']}", json={"fractional": False})
    assert r.status_code == 200 and r.json()["fractional"] is False
    single = c.post("/bots", json={"symbol": "C"}).json()
    r = c.patch(f"/bots/{single['id']}", json={"fractional": True})
    assert r.status_code == 422 and "whole shares only" in r.text
    assert c.get(f"/bots/{bot['id']}/decisions").json() == []
