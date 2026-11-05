"""A real account's money history: start, moves, and the sum that checks them against the account."""

from datetime import date

import pytest
from conftest import FakeBroker, at, make_bot
from fastapi.testclient import TestClient

from app import ledger, main, withdraw
from app.brokers import catalog as real_catalog
from app.models import AccountFee, Event, MoneyMove
from app.trader import add_capital

NOW = at(23, 0)  # Monday 19:00 New York
LIVE = "alpaca-live"


@pytest.fixture
def account(monkeypatch):
    """The live account usable, its cash set by the test (None: it can't be read)."""
    cash = {"cash": 0.0}
    monkeypatch.setattr(main, "catalog", lambda: [{**b, "available": True} for b in real_catalog()])
    monkeypatch.setattr(main, "_account_reads", {})
    monkeypatch.setattr(main, "utcnow", lambda: NOW)
    monkeypatch.setattr(main, "_funds", lambda name: (({"cash": cash["cash"]}, "", NOW) if cash["cash"] is not None
                                                      else (None, "Alpaca 500", NOW)))
    return cash


@pytest.fixture
def client(account):
    with TestClient(main.app) as c:
        yield c


def _cash_bot(session, cash, **kw):
    """A live bot holding only cash: its equity is its cash."""
    return make_bot(session, broker=LIVE, cash=cash, allocated_cash=kw.pop("allocated_cash", cash), peak_equity=cash, **kw)


def _ledger(client):
    r = client.get(f"/accounts/{LIVE}/ledger")
    assert r.status_code == 200, r.text
    return r.json()


def _start(client, amount=1000, **kw):
    r = client.post(f"/accounts/{LIVE}/money", json={"kind": "start", "amount": amount, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_the_sum_counts_from_the_start_not_from_what_the_running_bots_hold(client, session, account):
    """What made the card wrong: bots ran through money, some archived at a loss, money moved between them. What was put
    in the running ones says nothing about the $1,000 it started with: the sum does."""
    _start(client)
    _cash_bot(session, 360.0, allocated_cash=400.0, status="archived")  # lost $40, archived
    _cash_bot(session, 100.0, allocated_cash=200.0, withdrawn_cash=70.0)  # lost $30, $70 taken out
    _cash_bot(session, 270.0, allocated_cash=200.0, added_cash=70.0)  # that $70 added here
    account["cash"] = 1000.0 - 40.0 - 30.0  # what is really at the broker

    r = _ledger(client)

    assert r["start"] == 1000.0 and r["capital"] == 1000.0  # not the $470 put in the running bots
    assert r["running_result"] == -30.0 and r["archived_result"] == -40.0 and r["result"] == -70.0
    assert r["result_pct"] == -7.0  # of the $1,000
    assert r["expected"] == 930.0  # 1000 - 70
    assert r["bots_equity"] == 370.0
    assert r["free_cash"] == 560.0  # the account's cash, less the running bots'
    assert r["equity"] == 930.0 and r["difference"] == 0.0


def test_moving_money_between_bots_changes_neither_capital_nor_result(client, session, account):
    _start(client, 300)
    a = _cash_bot(session, 200.0)
    b = _cash_bot(session, 100.0)
    account["cash"] = 300.0
    before = _ledger(client)

    withdraw.take_out(session, a, 50.0, NOW)
    add_capital(session, b, 50.0, NOW)
    session.commit()
    after = _ledger(client)

    assert (after["capital"], after["result"], after["equity"], after["difference"]) == (300.0, before["result"], 300.0, 0.0)
    assert after["added"] == 50.0 and after["taken_out"] == 50.0


def test_a_real_deposit_is_capital_not_gain_and_one_not_written_down_shows_as_the_difference(client, session, account):
    _start(client, 100)
    _cash_bot(session, 100.0)
    account["cash"] = 150.0  # $50 reached the account

    r = _ledger(client)
    assert r["difference"] == 50.0 and r["result"] == 0.0  # not written down: not a gain either

    assert client.post(f"/accounts/{LIVE}/money", json={"kind": "deposit", "amount": 50}).status_code == 201
    r = _ledger(client)
    assert (r["deposits"], r["capital"], r["expected"], r["difference"]) == (50.0, 150.0, 150.0, 0.0)

    account["cash"] = 120.0  # and $30 went back to the bank
    assert client.post(f"/accounts/{LIVE}/money", json={"kind": "withdrawal", "amount": 30}).status_code == 201
    r = _ledger(client)
    assert (r["withdrawals"], r["capital"], r["difference"]) == (30.0, 120.0, 0.0)


def test_a_fee_no_bot_paid_is_a_loss_and_booked_fees_are_not_counted_twice(client, session, account):
    _start(client, 100)
    bot = _cash_bot(session, 99.99, allocated_cash=100.0)  # paid a cent
    session.add(AccountFee(broker=LIVE, activity_id="a", day=date(2026, 10, 20), amount=0.01, bot_id=bot.id))
    session.add(AccountFee(broker=LIVE, activity_id="b", day=date(2026, 10, 21), amount=0.02, bot_id=None))
    session.commit()
    account["cash"] = 99.97

    r = _ledger(client)
    assert (r["fees"], r["fees_no_bot"], r["result"], r["difference"]) == (0.03, 0.02, -0.03, 0.0)
    assert r["moves"][-1]["kind"] == "fee" and r["moves"][-1]["amount"] == -0.02


def test_the_history_is_every_move_and_ends_at_free_cash_by_the_records(client, session, account):
    """Started, added to, taken from, archived, each a line; the last line against the account's free cash is the same
    difference the sum shows."""
    _start(client, 500)
    old = _cash_bot(session, 90.0, allocated_cash=100.0, status="archived")
    session.add(Event(bot_id=old.id, kind="archived", message="Archived by user", created_at=at(16, 0, 7)))
    bot = _cash_bot(session, 200.0)
    add_capital(session, bot, 25.0, NOW)
    withdraw.take_out(session, bot, 40.0, NOW)
    session.commit()
    account["cash"] = 500.0 - 10.0 - 0.01  # the $10 the archived bot lost, and a cent nobody explains

    r = _ledger(client)
    lines = [(m["kind"], m["bot_id"], m["amount"]) for m in r["moves"]]
    assert lines[0] == ("start", None, 500.0)
    assert set(lines[1:]) == {("new_bot", old.id, -100.0), ("new_bot", bot.id, -200.0), ("add", bot.id, -25.0),
                              ("take_out", bot.id, 40.0), ("archived", old.id, 90.0)}
    assert r["moves"][-1]["free_after"] == 305.0  # 500 - 100 - 200 - 25 + 40 + 90
    assert r["free_cash"] == 304.99
    assert r["difference"] == -0.01 == round(r["free_cash"] - r["moves"][-1]["free_after"], 2)


def test_add_money_and_take_out_each_leave_a_row_that_adds_up_to_the_bots_totals(session):
    bot = _cash_bot(session, 200.0)
    add_capital(session, bot, 30.0, NOW)
    withdraw.take_out(session, bot, 50.0, NOW)
    withdraw.take_out(session, bot, -0.05, NOW)  # what settle does when the sales raised less
    session.commit()

    rows = session.query(MoneyMove).filter_by(bot_id=bot.id).all()
    assert sum(m.amount for m in rows if m.kind == "add") == bot.added_cash == 30.0
    assert round(sum(m.amount for m in rows if m.kind == "take_out"), 2) == -bot.withdrawn_cash == -49.95


def test_a_withdrawal_whose_sales_raised_less_reads_as_one_line(client, session, account):
    _start(client, 200)
    bot = _cash_bot(session, 200.0)
    withdraw.take_out(session, bot, 50.0, NOW)
    withdraw.take_out(session, bot, -0.05, NOW)
    session.commit()
    account["cash"] = 200.0

    lines = [m for m in _ledger(client)["moves"] if m["kind"] == "take_out"]
    assert [m["amount"] for m in lines] == [49.95]


# The events' wording as withdraw._story and main.add_bot_capital write it (other amounts), which the backfill reads
TOOK_OUT = ("Took out $1.00 of REAL MONEY: $0.00 from its cash, $1.00 from selling about 1.0% of each holding. Sales: "
            "0.002 AMD @ $500.00. Now $100.00 (cash $3.00), from $100.00 put in and $1.00 taken out in all: the return "
            "counts what was taken out.")
TOOK_LESS = ("Took out $50.00 of REAL MONEY: $0.20 from its cash, $49.80 from selling about 30.0% of each holding. Sales: "
             "0.3 AR @ $40.00. The sales raised $0.05 less than estimated, which stays in the bot: $49.95 came out. "
             "Now $100.00 (cash $0.00), from $150.00 put in and $49.95 taken out in all: the return counts what was taken out.")
ADDED = ("Added $1,030.00 of REAL MONEY: $1,150.00 put in (started with $120.00). Its next check that can buy tops up its 4 "
         "holdings to a full slot of about $40.00 (1/4 of the equity), then buys new dips with the same slots.")
WORKING = ("Took out $80.00 of REAL MONEY: $40.00 from its cash, $40.00 from selling about 20.0% of each holding. The sale of "
           "HUBB is still working at the broker: the withdrawal is settled once it has finished (if the sales raise less "
           "than estimated, less comes out). Now $100.00 (cash $0.00).")
SETTLED_LESS = ("Withdrawal of $80.00 settled: its sales raised $0.03 less than estimated (a fill under the quote, or one that "
                "didn't fill: see Orders), which stays in the bot. $79.97 came out; cash $0.03.")
SETTLED = "Withdrawal of $10.00 settled: its sales have filled; cash $5.00."


def _events(session, bot, *rows):
    for n, (kind, message) in enumerate(rows):
        session.add(Event(bot_id=bot.id, kind=kind, message=message, created_at=at(16, n)))
    session.commit()


def test_backfill_reads_every_old_add_and_withdrawal_from_the_events_once(session):
    bot = _cash_bot(session, 50.0, allocated_cash=120.0, added_cash=1030.0, withdrawn_cash=140.92)  # 1 + 49.95 + 79.97 + 10
    _events(session, bot, ("capital", ADDED), ("withdrawal", TOOK_OUT), ("withdrawal", TOOK_LESS), ("withdrawal", WORKING),
            ("withdrawal", SETTLED_LESS), ("withdrawal", "Took out $10.00: $10.00 from its cash."), ("withdrawal", SETTLED),
            ("order", "SELL 0.3 AR @ $40.00 (withdrawal), P&L $-0.50"))

    assert ledger.backfill(session) == 6

    rows = session.query(MoneyMove).filter_by(bot_id=bot.id).all()
    assert [m.amount for m in rows if m.kind == "add"] == [1030.0]
    assert sorted(m.amount for m in rows if m.kind == "take_out") == [-80.0, -49.95, -10.0, -1.0, 0.03]
    assert round(sum(m.amount for m in rows if m.kind == "take_out"), 2) == -bot.withdrawn_cash
    assert all(not m.note for m in rows)  # the events explained it all
    assert ledger.backfill(session) == 0  # a second start writes nothing


def test_backfill_says_so_when_the_events_dont_explain_the_money(session):
    """A bot's total without an event for it (changed by a script, say): it still adds up, marked as of no known day."""
    bot = _cash_bot(session, 50.0, allocated_cash=120.0, added_cash=30.0, withdrawn_cash=20.0)
    _events(session, bot, ("capital", "Added $25.00: $145.00 put in."))

    ledger.backfill(session)

    rows = session.query(MoneyMove).filter_by(bot_id=bot.id).all()
    assert sorted((m.kind, m.amount) for m in rows) == [("add", 5.0), ("add", 25.0), ("take_out", -20.0)]
    assert sum(1 for m in rows if m.note) == 2
    assert ledger.backfill(session) == 0


def test_backfill_leaves_a_bot_with_rows_alone(session):
    bot = _cash_bot(session, 200.0)
    add_capital(session, bot, 30.0, NOW)
    session.commit()
    _events(session, bot, ("capital", "Added $30.00: $230.00 put in."))

    assert ledger.backfill(session) == 0
    assert session.query(MoneyMove).filter_by(bot_id=bot.id).count() == 1


def test_the_start_defaults_to_the_first_bots_day_and_there_is_only_one(client, session):
    bot = _cash_bot(session, 100.0)
    bot.created_at = at(12, 0, 7)
    session.commit()

    r = _start(client)
    assert r["start"] == 1000.0 and r["first_bot_day"] == "2025-06-07" and r["moves"][0]["at"].startswith("2025-06-07T")
    assert client.post(f"/accounts/{LIVE}/money", json={"kind": "start", "amount": 2000}).status_code == 409


def test_a_written_line_can_be_deleted_and_a_bots_move_cannot(client, session):
    bot = _cash_bot(session, 100.0)
    add_capital(session, bot, 30.0, NOW)
    session.commit()
    r = _start(client)
    line = r["moves"][0]
    assert line["kind"] == "start" and line["id"]

    r = client.delete(f"/accounts/{LIVE}/money/{line['id']}")
    assert r.status_code == 200 and r.json()["start"] is None and r.json()["difference"] is None

    its_move = session.query(MoneyMove).filter_by(bot_id=bot.id).one()
    assert client.delete(f"/accounts/{LIVE}/money/{its_move.id}").status_code == 404
    assert session.query(MoneyMove).filter_by(bot_id=bot.id).count() == 1


@pytest.mark.parametrize("body", [{"kind": "start", "amount": 0}, {"kind": "start", "amount": -5}, {"kind": "gift", "amount": 5},
                                  {"kind": "deposit", "amount": 5, "day": "2099-01-01"}])
def test_bad_lines_are_refused(client, body):
    assert client.post(f"/accounts/{LIVE}/money", json=body).status_code == 422


@pytest.mark.parametrize("broker", ["paper", "alpaca-paper", "nope"])
def test_only_a_real_money_account_has_a_history(client, broker):
    assert client.get(f"/accounts/{broker}/ledger").status_code == 404
    assert client.post(f"/accounts/{broker}/money", json={"kind": "start", "amount": 5}).status_code == 404


def test_paper_bots_stay_out_of_the_live_sum(client, session, account):
    _start(client, 100)
    _cash_bot(session, 100.0)
    make_bot(session, broker="alpaca-paper", cash=7000.0, allocated_cash=9000.0)
    account["cash"] = 100.0

    r = _ledger(client)
    assert (r["result"], r["bots_equity"], r["difference"]) == (0.0, 100.0, 0.0)
    assert len(r["moves"]) == 2


def test_an_unread_account_says_why_and_the_records_still_show(client, session, account):
    _start(client, 100)
    _cash_bot(session, 90.0, allocated_cash=100.0)
    account["cash"] = None

    r = _ledger(client)
    assert (r["free_cash"], r["equity"], r["difference"], r["error"]) == (None, None, None, "Alpaca 500")
    assert (r["result"], r["expected"], r["bots_equity"]) == (-10.0, 90.0, 90.0)


def test_no_start_yet_has_nothing_to_measure_against(client, session, account):
    _cash_bot(session, 90.0, allocated_cash=100.0)
    account["cash"] = 95.0

    r = _ledger(client)
    assert (r["start"], r["expected"], r["result_pct"], r["difference"]) == (None, None, None, None)
    assert r["result"] == -10.0 and r["free_cash"] == 5.0


def test_a_bots_running_value_counts_what_it_holds(client, session, account):
    """Equity is the bot's, shares at their last price, not only its cash."""
    _start(client, 100)
    bot = _cash_bot(session, 40.0, allocated_cash=100.0)
    bot.shares, bot.last_price = 1, 55.0
    session.commit()
    account["cash"] = 40.0

    r = _ledger(client)
    assert (r["bots_equity"], r["running_result"], r["free_cash"], r["difference"]) == (95.0, -5.0, 0.0, 0.0)


def test_the_add_and_withdraw_endpoints_record_their_moves(client, session, monkeypatch):
    """The real paths, with a fake broker, on a bot that has the cash."""
    broker = FakeBroker(price=100.0, now=NOW)
    monkeypatch.setattr(main, "get_broker", lambda bot: broker)
    bot = make_bot(session, broker="paper", cash=500.0, allocated_cash=500.0, peak_equity=500.0)

    r = client.post(f"/bots/{bot.id}/withdraw", json={"amount": 40})
    assert r.status_code == 200, r.text
    r = client.post(f"/bots/{bot.id}/capital", json={"amount": 15})
    assert r.status_code == 200, r.text

    rows = session.query(MoneyMove).filter_by(bot_id=bot.id).all()
    assert sorted((m.kind, m.amount) for m in rows) == [("add", 15.0), ("take_out", -40.0)]
