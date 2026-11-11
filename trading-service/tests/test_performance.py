"""A bot's gain chart and worst drawdowns: money added and taken out moves its equity, but isn't a gain or a loss."""

from datetime import date

import pytest
from conftest import at, make_bot
from fastapi.testclient import TestClient

from app import main, performance, withdraw
from app.models import EquitySnapshot, MoneyMove
from app.trader import add_capital, mark_to_market


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _bot(session, **kw):
    """A bot holding only cash, started with $100 on Monday June 2 2025 at 10:00 New York."""
    return make_bot(session, **{**dict(allocated_cash=100.0, cash=100.0, peak_equity=100.0, benchmark_price=100.0,
                                       created_at=at(14, 0, 2)), **kw})


def _day(session, bot, day, equity, price=100.0, benchmark=None):
    """The day's equity point as the scheduler leaves it: last updated at 16:00 New York."""
    session.add(EquitySnapshot(bot_id=bot.id, day=date(2025, 6, day), equity=equity, cash=equity, shares=0, price=price,
                               benchmark=benchmark, updated_at=at(20, 0, day)))


def _move(session, bot, day, amount, hour=15):
    session.add(MoneyMove(broker=bot.broker, bot_id=bot.id, kind="add" if amount > 0 else "take_out", amount=amount,
                          at=at(hour, 0, day)))


def test_adding_and_taking_out_money_moves_the_equity_but_not_the_gain(client, session):
    """What made the chart wrong: $50 added showed as a jump up, $40 taken out as a fall. Through the real Add money
    and withdrawal, the gain moves only with the bot's own gains and losses, and ends at the Money history's result."""
    bot = _bot(session)
    for day, cash, price in ((2, 100.0, 100.0), (3, 110.0, 105.0)):  # Tuesday: it made $10, its basket 5%
        bot.cash, bot.last_price = cash, price
        mark_to_market(session, bot, price, at(20, 0, day))
    add_capital(session, bot, 50.0, at(15, 0, 4))  # Wednesday: $50 in, at the basket's 105
    bot.cash = 155.0  # then lost $5
    mark_to_market(session, bot, 105.0, at(20, 0, 4))
    withdraw.take_out(session, bot, 40.0, at(15, 0, 5))  # Thursday: $40 out
    bot.cash = 120.0  # then made $5
    mark_to_market(session, bot, 105.0, at(20, 0, 5))
    session.commit()

    pts = client.get(f"/bots/{bot.id}/equity").json()
    assert [p["equity"] for p in pts] == [100.0, 110.0, 155.0, 120.0]  # what the chart drew: up $45, down $35
    assert [p["put_in"] for p in pts] == [100.0, 100.0, 150.0, 110.0]
    assert [p["gain"] for p in pts] == [0.0, 10.0, 5.0, 10.0]
    assert pts[-1]["gain"] == client.get(f"/bots/{bot.id}/ledger").json()["result"]
    # Buy & hold bought its basket with the $50 and sold some for the $40: its gain is its basket's 5% on $100
    assert [p["benchmark_gain"] for p in pts] == [0.0, 5.0, 5.0, 5.0]


def test_a_move_counts_from_the_first_point_measured_after_it(session):
    """Money added after the day's last update isn't in that day's equity yet: it's in the next day's."""
    bot = _bot(session)
    _day(session, bot, 2, 100.0)
    _move(session, bot, 2, 50.0, hour=21)  # 17:00 New York, after the close
    _day(session, bot, 3, 151.0)
    session.commit()
    assert [(p.put_in, p.gain) for p in performance.points(session, bot)] == [(100.0, 0.0), (150.0, 1.0)]


def test_days_from_before_buy_and_hold_kept_its_value_use_the_starting_capital(session):
    bot = _bot(session)
    _day(session, bot, 2, 100.0, price=90.0)  # no benchmark saved that day
    session.commit()
    p = performance.points(session, bot)[0]
    assert (p.benchmark, p.benchmark_gain) == (90.0, -10.0)


def test_drawdowns_are_falls_of_the_return_not_of_the_dollars(client, session):
    """Up 20%, down 25% from there, $90 added, back above the high, $150 taken out, then down 20%. In dollars it would
    read as a fall from $250 to $80 (-68%), and $90 added as a climb back: neither is what the bot did."""
    bot = _bot(session)
    for day, equity, price in ((2, 120.0, 110.0), (3, 90.0, 88.0), (4, 180.0, 95.0), (5, 230.0, 120.0), (6, 250.0, 118.0),
                               (9, 100.0, 115.0), (10, 80.0, 108.0)):
        _day(session, bot, day, equity, price)
    _move(session, bot, 4, 90.0)
    _move(session, bot, 9, -150.0)
    session.commit()

    r = client.get(f"/bots/{bot.id}/drawdowns").json()
    assert r["worst"] == [
        # Thursday's $230 was above Monday's $120 in dollars, but still 4% under that high in return
        dict(peak_day="2025-06-02", bottom_day="2025-06-03", recovered_day="2025-06-06", depth_pct=-25.0, loss=-30.0, days=4),
        # The withdrawal's day (June 9) neither gained nor lost: it's the high the bot fell from
        dict(peak_day="2025-06-09", bottom_day="2025-06-10", recovered_day=None, depth_pct=-20.0, loss=-20.0, days=1),
    ]
    assert r["now_pct"] == -20.0
    assert r["buy_hold_max_pct"] == -20.0  # its basket: 110 down to 88


def test_a_days_move_counts_for_half_of_that_day(session):
    """$100 in, $50 taken out during the day and $10 lost: the $50 was in the bot for part of the day only, so the
    loss is measured against $75, not $100 (-10%) or $50 (-20%)."""
    bot = _bot(session)
    _day(session, bot, 2, 100.0)
    _move(session, bot, 3, -50.0)
    _day(session, bot, 3, 40.0)
    session.commit()
    d = performance.drawdowns(bot, performance.points(session, bot))
    assert [(x.depth_pct, x.loss) for x in d.worst] == [(-13.33, -10.0)]


def test_a_bot_that_lost_from_its_first_day_fell_from_its_start(session):
    bot = _bot(session)
    _day(session, bot, 2, 95.0)
    _day(session, bot, 3, 101.0)
    session.commit()
    d = performance.drawdowns(bot, performance.points(session, bot))
    assert [(x.peak_day, x.bottom_day, x.recovered_day, x.depth_pct) for x in d.worst] == [
        (date(2025, 6, 2), date(2025, 6, 2), date(2025, 6, 3), -5.0)]
    assert d.now_pct == 0.0


def test_a_fall_too_small_to_show_isnt_listed(session):
    """A fraction of a cent under the high (a fill's rounding) would read as a -0.00% drawdown."""
    bot = _bot(session)
    _day(session, bot, 2, 99.999)
    _day(session, bot, 3, 101.0)
    session.commit()
    assert performance.drawdowns(bot, performance.points(session, bot)).worst == []


def test_no_points_no_drawdowns(client, session):
    bot = _bot(session, benchmark_price=None)
    assert client.get(f"/bots/{bot.id}/drawdowns").json() == dict(worst=[], now_pct=0.0, buy_hold_max_pct=None)
    assert client.get("/bots/999999/drawdowns").status_code == 404
