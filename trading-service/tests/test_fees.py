"""Alpaca's daily fees on the live account (fees.py): read from its activity list (mocked HTTP, no network) and booked
once to the bot that caused most of each, so the bots' cash stays what the account has."""

from datetime import date, datetime, timezone
from itertools import count

import httpx
import pytest
from conftest import make_bot
from sqlalchemy import select

from app import fees
from app.brokers import alpaca
from app.brokers.alpaca import AlpacaBroker
from app.brokers.base import BrokerError
from app.models import AccountFee, Bot, Event, Order
from app.trader import bot_lock

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
_ids = count(1)


def utc(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=timezone.utc)


def order(session, bot, side: str, qty: float, price: float, at: datetime, filled: bool = True) -> None:
    session.add(Order(bot_id=bot.id, side=side, symbol="AAPL", qty=qty, filled_qty=qty if filled else 0.0,
                      avg_price=price if filled else None, status="filled" if filled else "new", reason="dip",
                      client_order_id=f"f-{next(_ids)}", created_at=at))


def fee(id_: str, day: str, description: str, amount: float = 0.01) -> dict:
    return {"id": id_, "day": date.fromisoformat(day), "amount": amount, "description": description}


def test_alpacas_fee_lines_are_read_page_by_page(monkeypatch):
    monkeypatch.setattr(alpaca, "FEES_PAGE", 2)
    asked = []

    def handler(req):
        asked.append((req.url.host, req.url.path, dict(req.url.params)))
        if "page_token" not in req.url.params:
            return httpx.Response(200, json=[
                {"id": "a1", "activity_type": "FEE", "date": "2026-10-20", "net_amount": "-0.01", "status": "executed",
                 "description": "CAT fee for proceed of 25 trades on 2026-10-20 by 376259812"},
                {"id": "a2", "activity_type": "FEE", "date": "2026-10-20", "net_amount": "-0.01", "status": "canceled",
                 "description": "REG fee for proceed of $452.6 on 2026-10-20 by 376259812"}])
        return httpx.Response(200, json=[{"id": "a3", "activity_type": "FEE", "date": "2026-10-21", "net_amount": "0.01",
                                          "status": "executed", "description": "REG fee refund"}])

    out = AlpacaBroker("key", "secret", live=True, transport=httpx.MockTransport(handler)).fees(date(2026, 10, 20))
    assert out == [{"id": "a1", "day": date(2026, 10, 20), "amount": 0.01,
                    "description": "CAT fee for proceed of 25 trades on 2026-10-20 by 376259812"},
                   {"id": "a3", "day": date(2026, 10, 21), "amount": -0.01, "description": "REG fee refund"}]  # canceled: out
    assert asked == [
        ("api.alpaca.markets", "/v2/account/activities/FEE", {"after": "2026-10-19", "direction": "asc", "page_size": "2"}),
        ("api.alpaca.markets", "/v2/account/activities/FEE",
         {"after": "2026-10-19", "direction": "asc", "page_size": "2", "page_token": "a2"})]

    with pytest.raises(BrokerError, match="Alpaca fees 403"):
        AlpacaBroker("key", "secret", live=True,
                     transport=httpx.MockTransport(lambda req: httpx.Response(403, text="forbidden"))).fees(date(2026, 10, 1))


@pytest.fixture
def bots(session) -> dict[str, Bot]:
    """Live bots a (most orders on Oct 20), b (most sales that day), c (archived since); a paper bot p trading more."""
    a = make_bot(session, broker="alpaca-live", symbol="A")
    b = make_bot(session, broker="alpaca-live", symbol="B")
    c = make_bot(session, broker="alpaca-live", symbol="C", status="archived")
    p = make_bot(session, broker="alpaca-paper", symbol="P")
    for _ in range(3):
        order(session, a, "BUY", 0.5, 40.0, utc(20, 15))
    order(session, a, "BUY", 1, 40.0, utc(20, 15), filled=False)  # never filled: not a trade
    order(session, b, "SELL", 2, 100.0, utc(21, 2))  # 10 pm New York: still Oct 20 there
    order(session, b, "BUY", 1, 30.0, utc(20, 3))  # 11 pm New York on Oct 19
    order(session, c, "SELL", 1, 50.0, utc(20, 18))
    order(session, c, "BUY", 1, 50.0, utc(22, 15))
    for _ in range(5):
        order(session, p, "BUY", 1, 10.0, utc(20, 15))  # the paper account's: its own fees, not these
    session.commit()
    return {"a": a, "b": b, "c": c, "p": p}


def cash(session, bots) -> dict[str, tuple[float, float]]:
    session.expire_all()
    return {k: (session.get(Bot, b.id).cash, session.get(Bot, b.id).realized_pnl) for k, b in bots.items()}


def test_each_fee_is_paid_once_by_the_bot_that_caused_most_of_it(session, bots):
    a, b, c = (bots[k].id for k in "abc")
    lines = [
        fee("f1", "2026-10-20", "CAT fee for proceed of 5 trades on 2026-10-20 by 376259812"),  # a: 3 of 5 orders
        fee("f2", "2026-10-20", "REG fee for proceed of $250 on 2026-10-20 by 376259812"),  # b: $200 of $250 sold
        fee("f3", "2026-10-22", "CAT fee for proceed of 1 trades on 2026-10-22 by 376259812"),  # c, archived since
        fee("f4", "2026-10-23", "CAT fee for proceed of 2 trades on 2026-10-23 by 376259812"),  # no bot traded
        fee("f5", "2026-10-20", "ADR fee for AAPL", 0.05),  # not on the bots' trades
        fee("f6", "2026-10-19", "REG fee for proceed of $0 on 2026-10-19 by 376259812"),  # no sales: the most orders, b
    ]
    assert fees.book(session, lines, NOW) == 6
    assert cash(session, bots) == {"a": (9_999.99, -0.01), "b": (9_999.98, -0.02), "c": (9_999.99, -0.01),
                                   "p": (10_000.0, 0.0)}
    rows = session.scalars(select(AccountFee).order_by(AccountFee.id)).all()
    assert [(r.activity_id, r.bot_id, r.day, r.amount) for r in rows] == [
        ("f1", a, date(2026, 10, 20), 0.01), ("f2", b, date(2026, 10, 20), 0.01), ("f3", c, date(2026, 10, 22), 0.01),
        ("f4", None, date(2026, 10, 23), 0.01), ("f5", None, date(2026, 10, 20), 0.05), ("f6", b, date(2026, 10, 19), 0.01)]
    said = {e.bot_id: e.message for e in session.scalars(select(Event).where(Event.kind == "fee", Event.bot_id == a))}
    assert said[a] == ("Alpaca's CAT fee for Oct 20: $0.01 (CAT fee for proceed of 5 trades on 2026-10-20 by 376259812). "
                       "Paid by this bot: it had the most orders filled that day (3 of 5).")
    msgs = [e.message for e in session.scalars(select(Event).where(Event.kind == "fee").order_by(Event.id))]
    assert "Paid by this bot: it sold the most that day ($200.00 of $250.00)." in msgs[1]
    assert msgs[3].endswith("No bot pays it: none of the bots traded that day.")
    assert msgs[4].endswith("No bot pays it: not a fee on the bots' trades.")

    assert fees.book(session, lines, NOW) == 0  # read again: each line is booked once
    assert cash(session, bots)["b"] == (9_999.98, -0.02)


def test_a_bot_busy_with_its_pass_pays_at_the_next_read(session, bots, monkeypatch):
    monkeypatch.setattr(fees, "LOCK_WAIT_S", 0.01)
    line = [fee("f1", "2026-10-20", "CAT fee for proceed of 5 trades on 2026-10-20 by 376259812")]
    lock = bot_lock(bots["a"].id)
    lock.acquire()
    try:
        assert fees.book(session, line, NOW) == 0
    finally:
        lock.release()
    assert cash(session, bots)["a"] == (10_000.0, 0.0) and session.scalar(select(AccountFee.id)) is None
    assert fees.book(session, line, NOW) == 1
    assert cash(session, bots)["a"] == (9_999.99, -0.01)


class FakeFees:
    def __init__(self, lines):
        self.lines, self.asked = lines, []

    def fees(self, since):
        self.asked.append(since)
        return self.lines


def test_the_hourly_read_asks_for_the_last_30_days_and_only_with_live_bots(session):
    broker = FakeFees([fee("f1", "2026-10-20", "CAT fee for proceed of 1 trades on 2026-10-20 by 376259812")])
    make_bot(session, broker="alpaca-paper")
    assert fees.run_once(NOW, broker) == 0 and broker.asked == []  # never a live bot here (the Mac's database)
    live = make_bot(session, broker="alpaca-live", status="archived")
    order(session, live, "BUY", 1, 10.0, utc(20, 15))
    session.commit()
    assert fees.run_once(NOW, broker) == 1 and broker.asked == [date(2026, 9, 24)]
    assert fees.run_once(NOW) == 0  # no live keys (the tests blank them): nothing asked
