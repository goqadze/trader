"""Taking money out of a running bot (withdraw.py, POST /bots/{id}/withdraw): the cash first, then the same share of each
holding; the return and buy & hold don't move; what the sales don't raise stays in the bot; refusals change nothing."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import FakeBroker, at, make_bot
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, notify, scheduler, withdraw
from app.brokers import BrokerOrder
from app.brokers import catalog as real_catalog
from app.config import settings
from app.models import DIP, ROTATION, Bot, EquitySnapshot, Event, Holding, Notification, Order, WatchItem
from app.trader import protect

OPEN, CLOSED = at(15, 0), at(23, 0)  # Monday June 2 2025, 11:00 and 19:00 New York


@pytest.fixture
def api(monkeypatch):
    clock = {"t": OPEN}
    broker = FakeBroker(now=OPEN, prices={"A": 20.0, "B": 75.0, "C": 5.0})
    monkeypatch.setattr(main, "utcnow", lambda: clock["t"])
    monkeypatch.setattr(main, "get_broker", lambda bot: broker)
    with TestClient(main.app) as c:
        yield c, clock, broker


def dip_bot(session, cash: float, held: dict[str, tuple[float, float]], put_in: float = 400.0, **kw) -> Bot:
    """A dip bot with `cash` and holdings {symbol: (shares, price)}, each bought at that price; `put_in` at the start."""
    bot = make_bot(session, **{
        "name": "dip", "symbol": "DIP", "strategy": DIP, "interval": "15m", "drop_pct": 0.05, "lookback": 1,
        "lookback_unit": "hours", "drop_from": "high", "target_mode": "reference", "rise_pct": 0.05, "stop_pct": 0.05,
        "max_positions": 4, "max_hold_days": 0, "news": False, "trend_filter": False, "universe": list(held),
        "benchmark_prices": {s: p for s, (_, p) in held.items()}, "allocated_cash": put_in, "cash": cash,
        "peak_equity": put_in, "benchmark_price": put_in, "last_price": put_in, **kw})
    for s, (n, p) in held.items():
        session.add(WatchItem(bot_id=bot.id, symbol=s, status="watching"))
        session.add(Holding(bot_id=bot.id, symbol=s, shares=n, cost_basis=n * p, entry_price=p, reference_price=p * 1.1,
                            stop_price=round(p * 0.95, 2), target_price=round(p * 1.1, 2), last_price=p, opened_at=OPEN))
    session.commit()
    return bot


def held(session, bot) -> dict[str, float]:
    session.expire_all()
    return {h.symbol: h.shares for h in session.scalars(select(Holding).where(Holding.bot_id == bot.id))}


def events(session, bot) -> list[Event]:
    session.expire_all()
    return list(session.scalars(select(Event).where(Event.bot_id == bot.id).order_by(Event.id)))


def orders(session, bot) -> list[Order]:
    session.expire_all()
    return list(session.scalars(select(Order).where(Order.bot_id == bot.id).order_by(Order.id)))


# --- How the sales are sized -----------------------------------------------------------------------------------------

def test_the_sales_take_the_same_share_of_each_holding_rounded_as_the_broker_sells_them():
    none = SimpleNamespace(slippage_pct=0.0, fee_pct=0.0)
    part, sales = withdraw.size(none, [("A", 10, 20.0), ("B", 2, 75.0)], 140, {"A", "B"})
    assert part == pytest.approx(0.4) and [(x.symbol, x.qty) for x in sales] == [("A", 4), ("B", 0.8)]
    # Whole shares: each share rounded down (A 0.9 -> 0, B 0.3 -> 0, C 3), then one more share from the holding nearest
    # to its next one (A) and so on while it is short (B): 165 raised for 120, the 45 left stays in cash
    part, sales = withdraw.size(none, [("A", 3, 100.0), ("B", 1, 50.0), ("C", 10, 5.0)], 120, set())
    assert part == pytest.approx(0.3) and [(x.symbol, x.qty) for x in sales] == [("A", 1), ("B", 1), ("C", 3)]
    # ... not every holding rounded up: $100 out of four 1-share holdings of $100 sells one of them, not all four
    _, sales = withdraw.size(none, [(s, 1, 100.0) for s in "ABCD"], 100, set())
    assert [(x.symbol, x.qty) for x in sales] == [("A", 1)]
    _, sales = withdraw.size(none, [("A", 10, 10.0), ("B", 10, 10.0)], 50, set())  # 2.5 each: 2 + 2, and 1 more
    assert [(x.symbol, x.qty) for x in sales] == [("A", 3), ("B", 2)]
    # Fractions to a millionth, $1 at least (the broker's smallest fractional order)
    _, sales = withdraw.size(none, [("A", 1, 500.0), ("B", 100, 1.0)], 2, {"A", "B"})
    assert [(x.symbol, x.qty) for x in sales] == [("A", 0.003334), ("B", 1.0)]
    # Sized on the quote less the bot's own slippage estimate: 99 needs 1 share of $100 at 1%, 100 needs 2
    slip = SimpleNamespace(slippage_pct=0.01, fee_pct=0.0)
    assert [x.qty for x in withdraw.size(slip, [("A", 10, 100.0)], 99, set())[1]] == [1]
    assert [x.qty for x in withdraw.size(slip, [("A", 10, 100.0)], 100, set())[1]] == [2]
    # Never more than a holding: a fraction held in a symbol sold whole goes all at once; more than all of it sells all
    assert [x.qty for x in withdraw.size(none, [("A", 2.5, 100.0)], 240, set())[1]] == [2.5]
    part, sales = withdraw.size(none, [("A", 3, 100.0)], 500, set())
    assert part == 1 and [x.qty for x in sales] == [3]
    assert withdraw.size(none, [("A", 3, 100.0)], 0, set()) == (0.0, [])


# --- Through the API --------------------------------------------------------------------------------------------------

def test_the_cash_pays_first_and_taking_money_out_moves_neither_return(api, session):
    c, clock, broker = api
    clock["t"] = CLOSED  # the cash covers it: no sale, so the market may be closed
    bot = dip_bot(session, cash=200.0, held={"A": (2, 125.0)}, last_price=420.0, peak_equity=460.0)
    before = c.get(f"/bots/{bot.id}").json()
    assert (before["equity"], before["return_pct"], before["buy_hold_return_pct"]) == (450, 12.5, 5.0)

    plan = c.get(f"/bots/{bot.id}/withdraw", params={"amount": 100}).json()
    assert (plan["from_cash"], plan["sales"], plan["left_in_cash"], plan["most"], plan["problem"]) == (100, [], 100, 350, None)
    r = c.post(f"/bots/{bot.id}/withdraw", json={"amount": 100})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["cash"], out["equity"], out["capital"], out["withdrawn_cash"], out["withdrawing"]) == (100, 350, 400, 100, None)
    assert (out["return_pct"], out["buy_hold_return_pct"]) == (12.5, 5.0)  # (350 + 100) / 400, buy & hold (320 + 100) / 400
    assert out["peak_equity"] == 357.78  # 460 x 350/450: the same drawdown in %
    assert broker.sent == [] and held(session, bot) == {"A": 2}
    snap = session.scalar(select(EquitySnapshot).where(EquitySnapshot.bot_id == bot.id))
    assert (snap.equity, snap.benchmark) == (350, 320)
    ev = events(session, bot)[-1]
    assert (ev.kind, ev.level) == ("withdrawal", "info")
    assert ev.message.startswith("Took out $100.00 from its cash ($200.00). Now $350.00 (cash $100.00), from $400.00 put in "
                                 "and $100.00 taken out in all: the return counts what was taken out. New buys get slots of "
                                 "about $87.50 (1/4 of the equity)")


def test_what_the_cash_doesnt_cover_comes_from_part_of_each_holding(api, session):
    c, _, broker = api
    bot = dip_bot(session, cash=50.0, held={"A": (10, 20.0), "B": (2, 75.0)}, fractional=True)
    plan = c.get(f"/bots/{bot.id}/withdraw", params={"amount": 190}).json()
    assert (plan["from_cash"], plan["part"], plan["raises"], plan["left_in_cash"], plan["problem"]) == (50, 0.4, 140, 0, None)
    assert [(s["symbol"], s["held"], s["qty"], s["raises"]) for s in plan["sales"]] == [("A", 10, 4, 80), ("B", 2, 0.8, 60)]
    assert broker.sent == [] and held(session, bot) == {"A": 10, "B": 2}  # a preview sends nothing

    r = c.post(f"/bots/{bot.id}/withdraw", json={"amount": 190})
    assert r.status_code == 200, r.text
    out = r.json()
    assert broker.sent == [("SELL", "A", 4), ("SELL", "B", 0.8)]
    assert [(o.symbol, o.reason, o.status, o.pnl) for o in orders(session, bot)] == [
        ("A", "withdrawal", "filled", 0), ("B", "withdrawal", "filled", 0)]
    assert (out["cash"], out["equity"], out["withdrawn_cash"], out["withdrawing"], out["return_pct"]) == (0, 210, 190, None, 0)
    a, b = sorted(out["holdings"], key=lambda h: h["symbol"])
    assert (a["shares"], a["stop_price"], a["target_price"], b["shares"], b["stop_price"]) == (6, 19.0, 22.0, 1.2, 71.25)
    assert [w.sold_on for w in session.scalars(select(WatchItem).where(WatchItem.bot_id == bot.id))] == [None, None]
    assert events(session, bot)[-1].message.startswith(
        "Took out $190.00: $50.00 from its cash, $140.00 from selling about 40.0% of each holding. Sales: 4 A @ $20.00; "
        "0.8 B @ $75.00. Now $210.00 (cash $0.00)")


def test_whole_shares_sell_a_little_more_and_a_holding_sold_whole_waits_until_tomorrow(api, session):
    c, _, broker = api
    broker.prices = {"A": 100.0, "B": 50.0, "C": 5.0}
    bot = dip_bot(session, cash=0.0, held={"A": (3, 100.0), "B": (1, 50.0), "C": (10, 5.0)}, top_up=True)
    out = c.post(f"/bots/{bot.id}/withdraw", json={"amount": 120}).json()
    assert broker.sent == [("SELL", "A", 1), ("SELL", "B", 1), ("SELL", "C", 3)]
    assert held(session, bot) == {"A": 2, "C": 7}
    assert (out["cash"], out["equity"], out["return_pct"], out["top_up"]) == (45, 280, 0, None)  # 165 raised for 120
    b = session.scalar(select(WatchItem).where(WatchItem.bot_id == bot.id, WatchItem.symbol == "B"))
    assert str(b.sold_on) == "2025-06-02"
    message = events(session, bot)[-1].message
    assert "Sold whole: B, not bought again before tomorrow. The top-up still waiting from Add money is dropped." in message


def test_a_sale_filling_under_its_estimate_takes_out_less(api, session):
    c, _, broker = api
    broker.slippage_pct = 0.01  # A fills at $19.80, the bot estimated none
    bot = dip_bot(session, cash=0.0, held={"A": (10, 20.0)}, put_in=200.0, fractional=True)
    out = c.post(f"/bots/{bot.id}/withdraw", json={"amount": 100}).json()
    assert broker.sent == [("SELL", "A", 5)]
    assert (out["cash"], out["withdrawn_cash"], out["withdrawing"], out["equity"]) == (0, 99, None, 99)
    assert (out["return_pct"], out["buy_hold_return_pct"]) == (-1.0, 0.0)  # the slippage is the bot's loss, not buy & hold's
    assert ("The sales raised $1.00 less than estimated, which stays in the bot: $99.00 came out."
            in events(session, bot)[-1].message)


def test_a_sale_still_working_is_settled_by_the_scheduler_and_what_didnt_fill_stays_in(api, session):
    c, _, broker = api
    broker.prices = {"A": 20.0, "B": 50.0}
    broker.submit_mode = "pending"
    bot = dip_bot(session, cash=0.0, held={"A": (10, 20.0), "B": (4, 50.0)}, fractional=True)
    out = c.post(f"/bots/{bot.id}/withdraw", json={"amount": 150}).json()
    assert broker.sent == [("SELL", "A", 3.75), ("SELL", "B", 1.5)]
    assert (out["cash"], out["equity"], out["withdrawn_cash"], out["withdrawing"], out["pending_order"]) == (-150, 250, 150, 150, True)
    assert "The sale of A, B is still working at the broker: the withdrawal is settled once it has finished" in events(session, bot)[-1].message
    assert c.post(f"/bots/{bot.id}/withdraw", json={"amount": 10}).status_code == 409  # one at a time

    a, b = orders(session, bot)
    scheduler.process_bot(bot.id, CLOSED, broker_factory=lambda _: broker)
    assert c.get(f"/bots/{bot.id}").json()["withdrawing"] == 150  # nothing finished yet
    broker.orders[a.client_order_id] = BrokerOrder(status="filled", filled_qty=3.75, avg_price=20.0)
    broker.orders[b.client_order_id] = BrokerOrder(status="canceled")  # a day order the close ended unfilled
    scheduler.process_bot(bot.id, CLOSED, broker_factory=lambda _: broker)
    out = c.get(f"/bots/{bot.id}").json()
    assert (out["cash"], out["withdrawn_cash"], out["withdrawing"], out["equity"], out["return_pct"]) == (0, 75, None, 325, 0)
    assert out["peak_equity"] == 325  # back in proportion: no drawdown came from it
    ev = events(session, bot)[-1]
    assert (ev.kind, ev.level) == ("withdrawal", "warning")
    assert ev.message.startswith("Withdrawal of $150.00 settled: its sales raised $75.00 less than estimated")
    assert "$75.00 came out; cash $0.00." in ev.message


def test_a_single_symbol_bot_moves_its_stop_order_to_the_shares_left(api, session):
    c, _, broker = api
    broker.supports_stop_orders = True
    broker.price = 100.0
    bot = make_bot(session, symbol="ZZ", allocated_cash=1_000.0, cash=0.0, peak_equity=1_000.0, shares=10, entry_price=100.0,
                   cost_basis=1_000.0, stop_price=96.0, target_price=108.0, last_price=100.0)
    protect(session, bot, broker, OPEN)
    assert broker.stops == [(10, 96.0)]
    out = c.post(f"/bots/{bot.id}/withdraw", json={"amount": 250}).json()
    assert len(broker.canceled) == 1 and broker.sent == [("SELL", "ZZ", 3)]  # 2.5 -> 2 whole shares, short: 3
    assert broker.stops == [(10, 96.0), (7, 96.0)]
    assert (out["shares"], out["cash"], out["cost_basis"], out["stop_price"], out["target_price"], out["stop_at_broker"]) == (
        7, 50, 700, 96, 108, True)
    assert events(session, bot)[-1].message.endswith("Its next buy uses 100% of its cash; the shares left keep their stop and target.")


def test_a_rotation_bot_sells_part_of_each_holding_but_not_during_a_rebalance(api, session):
    c, _, broker = api
    broker.prices = {"A": 20.0, "B": 50.0}
    bot = dip_bot(session, cash=0.0, held={"A": (10, 20.0), "B": (4, 50.0)}, strategy=ROTATION, top_n=2, lookback_months=12,
                  skip_months=1, abs_filter=True, rotation_plan={"decision_id": 1, "targets": [], "split": [], "rounds": 1})
    r = c.post(f"/bots/{bot.id}/withdraw", json={"amount": 100})
    assert r.status_code == 409 and "a rebalance is still in progress" in r.text
    session.get(Bot, bot.id).rotation_plan = None
    session.commit()
    out = c.post(f"/bots/{bot.id}/withdraw", json={"amount": 100}).json()
    assert broker.sent == [("SELL", "A", 3), ("SELL", "B", 1)]  # a quarter each: 2.5 -> 2 (+1 short) and 1
    assert (out["cash"], out["equity"], out["return_pct"]) == (10, 300, 0)
    assert "Its next rebalance sizes each pick at 1/2 of the equity." in events(session, bot)[-1].message


def test_a_withdrawal_never_trips_the_drawdown_breaker(api, session):
    c, clock, _ = api
    clock["t"] = CLOSED
    # 410 is 18% under its peak of 500 (the breaker trips at 20%); taking 200 out leaves the same 18%
    bot = dip_bot(session, cash=300.0, held={"A": (2, 55.0)}, put_in=500.0, peak_equity=500.0, max_drawdown_pct=0.2)
    out = c.post(f"/bots/{bot.id}/withdraw", json={"amount": 200}).json()
    assert (out["status"], out["equity"], out["peak_equity"]) == ("active", 210, 256.1)


def test_a_withdrawal_is_refused_before_anything_changes(api, session):
    c, clock, broker = api
    bot = dip_bot(session, cash=50.0, held={"A": (10, 20.0), "B": (2, 75.0)}, fractional=True)
    url = f"/bots/{bot.id}/withdraw"
    assert c.post(url, json={"amount": 0}).status_code == 422
    assert c.post(url, json={"amount": -5}).status_code == 422
    r = c.post(url, json={"amount": 300.01})
    assert r.status_code == 422 and r.json()["detail"] == (
        "at most $300.00: it has $400.00 ($50.00 of it cash) and keeps at least $100 to trade with. To take everything out, "
        "close its positions and archive it")
    assert c.get(url, params={"amount": 300.01}).json()["problem"].startswith("at most $300.00")

    clock["t"] = CLOSED
    r = c.post(url, json={"amount": 60})
    assert r.status_code == 409 and r.json()["detail"] == (
        "the market is closed: only its cash ($50.00) can come out now. Selling part of its holdings for the rest needs "
        "regular hours")
    clock["t"] = OPEN
    broker.quote_at = OPEN.replace(hour=13)  # two hours old
    r = c.post(url, json={"amount": 60})
    assert r.status_code == 409 and "no fresh quote to size the sales (quote for A is stale" in r.text
    broker.quote_at = None
    session.add(Order(bot_id=bot.id, side="BUY", symbol="C", qty=1, reason="dip", status="submitted",
                      client_order_id="working", created_at=OPEN, updated_at=OPEN))
    session.commit()
    r = c.post(url, json={"amount": 10})
    assert r.status_code == 409 and "still working at the broker" in r.text

    assert broker.sent == [] and held(session, bot) == {"A": 10, "B": 2}
    out = c.get(f"/bots/{bot.id}").json()
    assert (out["cash"], out["withdrawn_cash"], out["equity"], out["peak_equity"]) == (50, None, 400, 400)
    assert not [e for e in events(session, bot) if e.kind == "withdrawal"]

    small = dip_bot(session, cash=90.0, held={}, put_in=100.0)
    r = c.post(f"/bots/{small.id}/withdraw", json={"amount": 10})
    assert r.status_code == 422 and r.json()["detail"].startswith("nothing can come out: it has $90.00")
    gone = dip_bot(session, cash=500.0, held={}, put_in=500.0, status="archived")
    assert c.post(f"/bots/{gone.id}/withdraw", json={"amount": 10}).status_code == 409


def test_real_money_needs_a_yes_and_the_withdrawal_is_emailed(api, session, monkeypatch):
    c, clock, _ = api
    clock["t"] = CLOSED
    monkeypatch.setattr(main, "catalog", lambda: [{**b, "available": True} for b in real_catalog()])
    monkeypatch.setattr(notify, "settings", replace(settings, smtp_host="smtp.test", smtp_username="bot@test.dev"))
    notify.save(session, {"enabled": True, "recipients": ["me@test.dev"], "categories": ["trades"]}, OPEN)
    session.commit()
    bot = dip_bot(session, cash=200.0, held={}, broker="alpaca-live")
    url = f"/bots/{bot.id}/withdraw"
    r = c.post(url, json={"amount": 50})
    assert r.status_code == 422 and "confirm_live" in r.text
    assert c.get(f"/bots/{bot.id}").json()["cash"] == 200  # refused: nothing changed
    r = c.post(url, json={"amount": 50, "confirm_live": True})
    assert r.status_code == 200 and r.json()["cash"] == 150
    ev = events(session, bot)[-1]
    assert ev.level == "warning" and ev.message.startswith("Took out $50.00 of REAL MONEY from its cash ($200.00).")
    assert ev.message.endswith("No money leaves the alpaca-live account: it is the account's free cash now, to withdraw at "
                               "Alpaca or give to another bot.")
    n = session.scalar(select(Notification).where(Notification.bot_id == bot.id))
    assert (n.category, n.kind, n.subject) == ("trades", "withdrawal", "dip: Took out $50.00 of REAL MONEY from its cash ($200.00)")
