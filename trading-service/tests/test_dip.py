"""Dip buyer bots: when they check, what they buy and sell, the blacklist, the news switch, the signals, and the API."""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from conftest import FakeBroker, at, make_bot
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import dip, main, rotation, scheduler
from app.brokers import BrokerError
from app.brokers import catalog as real_catalog
from app.models import DIP, Decision, Event, Holding, Order, Signal, WatchItem
from app.trader import holdings

T = datetime(2025, 6, 2, 15, 0, 20, tzinfo=timezone.utc)  # Monday June 2 2025, 11:00:20 New York
T15 = T + timedelta(minutes=15)  # the next check (11:15)
NY_DAY = date(2025, 6, 2)


def bars(day: int = 2, **series) -> pd.DataFrame:
    """15-minute closes on June <day> 2025, the first closing at 9:45 New York; one list per symbol (shorter ones are
    padded with their last price)."""
    n = max(len(v) for v in series.values())
    idx = pd.date_range(f"2025-06-{day:02d} 09:45", periods=n, freq="15min", tz="America/New_York")
    return pd.DataFrame({s: list(v) + [v[-1]] * (n - len(v)) for s, v in series.items()}, index=idx)


def feed(df: pd.DataFrame):
    """A stand-in for the Yahoo download: the closes asked for."""
    return lambda symbols, interval, start: df.reindex(columns=symbols)


# By 11:00 New York: A was 110 all morning and just fell to 103 (6.4% under its 1-hour high); B is flat at 50
FALL = bars(A=[110, 110, 110, 110, 110, 103], B=[50] * 6)
QUOTES = {"A": 103.0, "B": 50.0, "C": 20.0}


def make_dip(session, symbols=("A", "B"), **kw):
    bot = make_bot(session, **{
        "symbol": "DIP", "strategy": DIP, "interval": "15m", "drop_pct": 0.05, "lookback": 1, "lookback_unit": "hours",
        "drop_from": "high", "target_mode": "reference", "rise_pct": 0.05, "stop_pct": 0.05, "max_positions": 2,
        "max_hold_days": 0, "news": False, "trend_filter": False, "universe": list(symbols),
        "benchmark_prices": {s: QUOTES[s] for s in symbols}, "benchmark_price": 10_000.0, "last_price": 10_000.0, **kw})
    for s in symbols:
        session.add(WatchItem(bot_id=bot.id, symbol=s, status="watching"))
    session.commit()
    return bot


def item(session, bot, symbol) -> WatchItem:
    return session.scalar(select(WatchItem).where(WatchItem.bot_id == bot.id, WatchItem.symbol == symbol))


def signals(session, bot) -> list[tuple[str, str]]:
    return [(s.symbol, s.kind) for s in session.scalars(select(Signal).where(Signal.bot_id == bot.id).order_by(Signal.id))]


def no_news(*a):
    raise AssertionError("asked the news while it is off")


def check(session, bot, broker, now=T, kind="scheduled", df=FALL, news=no_news, trend=None, moves=None):
    return dip.check(session, bot, broker, now, kind, bars_fn=feed(df), news_fn=news, trend_fn=trend, moves_fn=moves)


@pytest.fixture(autouse=True)
def reset_pacing():
    scheduler._last_watch.clear()
    scheduler._retry_after.clear()
    scheduler._last_error.clear()
    dip._trend_cache.clear()
    dip._move_cache.clear()


# --- The reference: the backtest's -----------------------------------------------------------------

def test_the_reference_uses_only_the_bars_before_the_one_that_just_closed(session):
    bot = make_dip(session)
    moment = datetime(2025, 6, 2, 15, 0, tzinfo=timezone.utc)  # 11:00 New York
    s = bars(A=[100, 120, 110, 105, 104, 103, 200])["A"]  # the 11:15 bar (200) is still forming: never in the window
    ref, price = dip.stand(s, bot, moment, NY_DAY)
    assert ref == 120 and price == 200  # the highest of the four bars that closed 10:00-10:45
    ref, _ = dip.stand(s, make_dip(session, drop_from="start"), moment, NY_DAY)
    assert ref == 120  # the window's first close (10:00)
    assert dip.stand(s.iloc[:3], bot, moment, NY_DAY)[0] is None  # not enough bars yet


def test_a_daily_bot_measures_against_the_days_before_today(session):
    bot = make_dip(session, interval="1d", lookback=3, lookback_unit="days")
    s = pd.Series([100.0, 110.0, 105.0, 104.0, 99.0],
                  index=pd.DatetimeIndex(["2025-05-27", "2025-05-28", "2025-05-29", "2025-05-30", "2025-06-02"]))
    assert dip.stand(s, bot, T, NY_DAY) == (110.0, 99.0)


def test_out_of_hours_the_latest_bar_is_the_price_not_part_of_its_window(session):
    bot = make_dip(session)
    s = bars(A=[110, 110, 110, 110, 110, 103])["A"]  # the session ended at 11:00 in this make-believe
    evening = datetime(2025, 6, 2, 23, 0, tzinfo=timezone.utc)
    assert dip.stand(s, bot, evening, NY_DAY) == (110, 103)
    daily = make_dip(session, interval="1d", lookback=3, lookback_unit="days")
    closes = pd.Series([100.0, 110.0, 105.0, 99.0], index=pd.DatetimeIndex(["2025-05-28", "2025-05-29", "2025-05-30", "2025-06-02"]))
    before_open = datetime(2025, 6, 3, 12, 0, tzinfo=timezone.utc)  # June 3, 8:00 New York: no bar for today yet
    assert dip.stand(closes, daily, before_open, date(2025, 6, 3)) == (110.0, 99.0)


def test_window_bars_match_the_backtest(session):
    assert dip.window_bars(make_dip(session)) == 4
    assert dip.window_bars(make_dip(session, lookback=5, lookback_unit="days")) == 5 * 26
    assert dip.window_bars(make_dip(session, interval="1h", lookback=3, lookback_unit="days")) == 21
    assert dip.window_bars(make_dip(session, interval="1d", lookback=10, lookback_unit="days")) == 10


# --- When ---------------------------------------------------------------------------------------------

def test_it_checks_at_the_end_of_each_bar_until_the_close(session):
    bot = make_dip(session)
    assert dip.check_moment(bot, at(13, 44)) is None  # 9:44: the first bar hasn't closed
    assert dip.check_moment(bot, at(13, 45)) == at(13, 45)
    assert dip.check_moment(bot, at(15, 14)) == at(15, 0)
    assert dip.check_moment(bot, at(19, 59)) == at(19, 45)  # the last check is 15:45: no order goes out at 16:00
    assert dip.check_moment(bot, at(20, 5)) is None and dip.check_moment(bot, at(15, 0, day=7)) is None  # closed, Saturday
    assert dip.due(bot, at(15, 0))
    bot.last_decision_at = at(15, 0) + timedelta(seconds=30)
    assert not dip.due(bot, at(15, 10)) and dip.due(bot, at(15, 15))
    assert dip.next_check_at(bot, at(15, 10)) == at(15, 15)
    assert dip.next_check_at(bot, at(21, 0)) == at(13, 45, day=3)  # after the close: tomorrow's first


def test_a_daily_bot_checks_once_in_the_last_half_hour(session):
    bot = make_dip(session, interval="1d", lookback=5, lookback_unit="days")
    assert not dip.due(bot, at(19, 29)) and dip.due(bot, at(19, 30))
    bot.last_decision_at = at(19, 31)
    assert not dip.due(bot, at(19, 50))
    assert dip.next_check_at(bot, at(19, 50)) == at(19, 30, day=3)
    assert dip.next_check_at(make_dip(session, status="paused"), at(15, 0)) is None


# --- Buying -------------------------------------------------------------------------------------------

def test_a_fall_is_bought_and_signalled(session):
    bot = make_dip(session)
    broker = FakeBroker(now=T, prices=QUOTES)
    d = check(session, bot, broker)
    assert broker.sent == [("BUY", "A", 48)]  # one of 2 slots: $5,000 / $103
    h = session.scalar(select(Holding).where(Holding.symbol == "A"))
    assert (h.shares, h.entry_price, h.reference_price, h.target_price, h.stop_price) == (48, 103.0, 110.0, 110.0, 97.85)
    assert signals(session, bot) == [("A", "down")]
    s = session.scalar(select(Signal))
    assert s.message == "A is 6.4% under its 1-hour high $110.00 at $103.00: buy zone"
    assert s.outcome.startswith("Bought: BUY 48 sh filled @ $103.00") and s.change_pct == pytest.approx(-6.36)
    assert d.action == "BUY" and d.reasoning == "Bought A." and bot.last_decision_at == T
    assert session.scalar(select(Order)).reference_price == 110.0
    a = item(session, bot, "A")
    assert a.in_zone and a.drop == pytest.approx(0.063636, abs=1e-6)
    assert a.dip_reference is None and a.trough_price is None  # bought: the fall isn't followed any more


def test_the_levels_come_from_the_actual_fill(session):
    bot = make_dip(session, slippage_pct=0.01)
    check(session, bot, FakeBroker(now=T, prices=QUOTES, slippage_pct=0.01))
    h = session.scalar(select(Holding))
    assert h.entry_price == pytest.approx(104.03) and h.stop_price == pytest.approx(98.83)  # 104.03 x 0.95
    assert h.target_price == 110.0


def test_the_percent_target_is_rise_pct_above_the_buy(session):
    bot = make_dip(session, target_mode="percent", rise_pct=0.03)
    check(session, bot, FakeBroker(now=T, prices=QUOTES))
    assert session.scalar(select(Holding)).target_price == pytest.approx(106.09)


def test_a_slot_smaller_than_one_share_buys_nothing_in_whole_shares(session):
    bot = make_dip(session, allocated_cash=150.0, cash=150.0, peak_equity=150.0)  # 2 slots of $75, A at $103
    broker = FakeBroker(now=T, prices=QUOTES)
    check(session, bot, broker)
    assert broker.sent == [] and "can't buy one share at $103.00" in session.scalar(select(Signal)).outcome


def test_fractional_shares_buy_a_slot_smaller_than_one_share_and_sell_it_all(session):
    bot = make_dip(session, allocated_cash=150.0, cash=150.0, peak_equity=150.0, fractional=True)
    broker = FakeBroker(now=T, prices=dict(QUOTES))
    d = check(session, bot, broker)
    assert broker.sent == [("BUY", "A", 0.728155)]  # $75 / $103, cut to a millionth of a share
    h = session.scalar(select(Holding).where(Holding.symbol == "A"))
    assert (h.shares, h.cost_basis, h.target_price, h.stop_price) == (0.728155, 75.0, 110.0, 97.85)
    assert session.scalar(select(Signal)).outcome.startswith("Bought: BUY 0.728155 sh filled @ $103.00")
    assert d.steps[0].endswith("; fractions of a share") and bot.cash == 75.0
    broker.prices["A"], broker.now = 110.0, T15
    check(session, bot, broker, now=T15, df=bars(A=[110] * 5 + [103, 110], B=[50] * 7))
    assert broker.sent[-1] == ("SELL", "A", 0.728155) and holdings(session, bot) == []
    assert bot.realized_pnl == pytest.approx(0.728155 * 7, abs=0.01) and bot.cash == pytest.approx(155.10, abs=0.01)


def test_a_symbol_the_broker_cannot_split_is_bought_in_whole_shares(session):
    bot = make_dip(session, allocated_cash=1_000.0, cash=1_000.0, peak_equity=1_000.0, fractional=True)  # $500 slots
    broker = FakeBroker(now=T, prices=QUOTES)
    broker.whole_only = {"A"}
    check(session, bot, broker)
    assert broker.sent == [("BUY", "A", 4)]
    assert session.scalar(select(Signal)).outcome.endswith("A can't be bought in fractions at fake.")


def test_a_fractional_buy_needs_a_dollar(session):
    tiny = dict(allocated_cash=1.8, cash=1.8, peak_equity=1.8, fractional=True)
    bot = make_dip(session, **tiny)  # 2 slots of 90 cents
    broker = FakeBroker(now=T, prices=QUOTES)
    check(session, bot, broker)
    assert broker.sent == [] and "under the $1 smallest fractional order" in session.scalar(select(Signal)).outcome
    check(session, make_dip(session, max_positions=1, **tiny), broker)  # one slot of $1.80
    assert broker.sent == [("BUY", "A", 0.017475)]


def test_a_quiet_check_leaves_no_decision_but_counts_as_done(session):
    bot = make_dip(session)
    flat = bars(A=[110] * 6, B=[50] * 6)
    assert check(session, bot, FakeBroker(now=T, prices=QUOTES), df=flat) is None
    assert session.scalars(select(Decision)).all() == [] and bot.last_decision_at == T
    assert item(session, bot, "A").drop == 0.0 and item(session, bot, "A").reference_price == 110


def test_the_deepest_falls_get_the_free_slots(session):
    bot = make_dip(session, symbols=("A", "B", "C"))
    df = bars(A=[110] * 5 + [103], B=[50] * 5 + [44], C=[20] * 5 + [18])  # A -6.4%, B -12%, C -10%
    broker = FakeBroker(now=T, prices={"A": 103.0, "B": 44.0, "C": 18.0})
    check(session, bot, broker, df=df)
    assert [s for _, s, _ in broker.sent] == ["B", "C"]
    a = session.scalar(select(Signal).where(Signal.symbol == "A"))
    assert a.kind == "down" and a.outcome == "All 2 slots in use."


def test_a_dip_still_on_is_bought_once_a_slot_frees_up_without_a_second_down_signal(session):
    bot = make_dip(session, max_positions=1)
    session.add(Holding(bot_id=bot.id, symbol="B", shares=100, cost_basis=5_000.0, entry_price=50.0, reference_price=55.0,
                        target_price=55.0, stop_price=47.5))
    session.commit()
    broker = FakeBroker(now=T, prices={"A": 103.0, "B": 50.0})
    check(session, bot, broker)
    assert broker.sent == [] and signals(session, bot) == [("A", "down")]
    broker.prices["B"] = 55.0  # B reaches its target at 11:15: its slot frees up, A is still down
    broker.now = T15
    check(session, bot, broker, now=T15, df=bars(A=[110] * 5 + [103, 103], B=[50] * 6 + [55]))
    assert broker.sent == [("SELL", "B", 100), ("BUY", "A", 150)]  # the one slot: all $15,500
    assert signals(session, bot)[1:] == [("B", "up"), ("A", "down")]
    assert "(still in the buy zone)" in session.scalars(select(Signal).order_by(Signal.id.desc())).first().message


# --- Selling and the blacklist ----------------------------------------------------------------------

def _bought(session):
    bot = make_dip(session)
    broker = FakeBroker(now=T, prices=dict(QUOTES))
    check(session, bot, broker)
    broker.now = T15
    return bot, broker


def test_back_at_the_reference_it_sells_and_signals_up_and_waits_for_tomorrow(session):
    bot, broker = _bought(session)
    broker.prices["A"] = 110.0
    d = check(session, bot, broker, now=T15, df=bars(A=[110] * 5 + [103, 110], B=[50] * 7))
    assert broker.sent[-1] == ("SELL", "A", 48) and holdings(session, bot) == []
    assert d.action == "SELL" and signals(session, bot) == [("A", "down"), ("A", "up")]
    assert bot.realized_pnl == pytest.approx(48 * 7)
    # 11:30: down again, but it was sold today
    broker.prices["A"], broker.now = 103.0, T15 + timedelta(minutes=15)
    check(session, bot, broker, now=broker.now, df=bars(A=[110] * 5 + [103, 110, 103], B=[50] * 8))
    assert len(broker.sent) == 2


def test_a_stop_loss_sells_and_blacklists_until_re_enabled(session):
    bot, broker = _bought(session)
    broker.prices["A"] = 97.0
    check(session, bot, broker, now=T15, df=bars(A=[110] * 5 + [103, 97], B=[50] * 7))
    assert broker.sent[-1] == ("SELL", "A", 48)
    order = session.scalars(select(Order).order_by(Order.id.desc())).first()
    assert order.reason == "stop-loss"
    a = item(session, bot, "A")
    assert a.status == "blacklisted" and "stop-loss at $97.00" in a.blacklist_reason
    stop = session.scalars(select(Signal).order_by(Signal.id.desc())).first()
    assert stop.kind == "stop" and "Blacklisted until you re-enable it" in stop.outcome
    # Days later it dips again: still blacklisted, not bought, no signal
    later = datetime(2025, 6, 4, 15, 0, 20, tzinfo=timezone.utc)
    broker.now, broker.prices["A"] = later, 90.0
    check(session, bot, broker, now=later, df=bars(day=4, A=[100] * 5 + [90], B=[50] * 6))
    assert len(broker.sent) == 2 and signals(session, bot)[-1] == ("A", "stop")


def test_max_hold_days_sells_on_time(session):
    bot, broker = _bought(session)
    bot.max_hold_days = 2
    later = datetime(2025, 6, 4, 15, 0, 20, tzinfo=timezone.utc)  # two trading days after the buy
    broker.now, broker.prices["A"] = later, 104.0
    check(session, bot, broker, now=later, df=bars(day=4, A=[104] * 6, B=[50] * 6))
    assert broker.sent[-1] == ("SELL", "A", 48) and signals(session, bot)[-1] == ("A", "time")


def test_a_dip_it_did_not_buy_signals_up_when_it_is_over(session):
    bot = make_dip(session, max_positions=1)
    session.add(Holding(bot_id=bot.id, symbol="B", shares=100, cost_basis=5_000.0, entry_price=50.0, reference_price=60.0,
                        target_price=60.0, stop_price=40.0))
    session.commit()
    broker = FakeBroker(now=T, prices={"A": 103.0, "B": 50.0})
    check(session, bot, broker)
    broker.now, broker.prices["A"] = T15, 110.0
    check(session, bot, broker, now=T15, df=bars(A=[110] * 5 + [103, 110], B=[50] * 7))
    up = session.scalars(select(Signal).order_by(Signal.id.desc())).first()
    assert (up.symbol, up.kind, up.outcome) == ("A", "up", "Not held: nothing to sell.")
    assert "the dip is over" in up.message and item(session, bot, "A").dip_reference is None


# --- Waiting for the turn (rebound) ---------------------------------------------------------------------

def _at(minutes: int) -> datetime:
    return T + timedelta(minutes=minutes)


def test_with_rebound_it_follows_the_fall_and_buys_on_the_turn_up(session):
    bot = make_dip(session, rebound=True, rebound_pct=0.01)
    broker = FakeBroker(now=T, prices={"A": 103.0, "B": 50.0})
    d = check(session, bot, broker)  # 11:00: fell 6.4% -> followed, not bought
    assert broker.sent == [] and signals(session, bot) == [("A", "down")]
    down = session.scalar(select(Signal))
    assert "fell into the buy zone" in down.message and down.outcome.startswith("Waiting for it to turn up 1.0% from its low ($104.03")
    a = item(session, bot, "A")
    assert (a.dip_reference, a.trough_price, a.turned_at) == (110.0, 103.0, None)
    assert d.reasoning == "Waiting for the turn: A."

    broker.now, broker.prices["A"] = _at(15), 100.0  # 11:15: still falling: the low moves down
    check(session, bot, broker, now=_at(15), df=bars(A=[110] * 5 + [103, 100], B=[50] * 7))
    assert broker.sent == [] and item(session, bot, "A").trough_price == 100.0

    broker.now, broker.prices["A"] = _at(30), 101.0  # 11:30: +1% off the low of 100: bearish turned bullish, bought
    d = check(session, bot, broker, now=_at(30), df=bars(A=[110] * 5 + [103, 100, 101], B=[50] * 8))
    assert broker.sent == [("BUY", "A", 49)] and d.action == "BUY"
    turn = session.scalars(select(Signal).order_by(Signal.id.desc())).first()
    assert (turn.kind, turn.symbol) == ("rebound", "A")
    assert turn.message == "A turned up +1.0% from its low $100.00 to $101.00: bearish turned bullish, buy"
    assert turn.outcome.startswith("Bought: BUY 49 sh filled @ $101.00")
    h = session.scalar(select(Holding))
    assert (h.reference_price, h.target_price, h.entry_price) == (110.0, 110.0, 101.0)  # back to where the fall started
    assert item(session, bot, "A").dip_reference is None


def test_a_fall_back_at_its_start_before_turning_is_over(session):
    bot = make_dip(session, rebound=True, rebound_pct=0.01)
    broker = FakeBroker(now=T, prices={"A": 103.0, "B": 50.0})
    check(session, bot, broker)
    broker.now, broker.prices["A"] = _at(15), 111.0  # jumped straight back over 110 between two checks
    check(session, bot, broker, now=_at(15), df=bars(A=[110] * 5 + [103, 111], B=[50] * 7))
    assert broker.sent == []
    over = session.scalars(select(Signal).order_by(Signal.id.desc())).first()
    assert over.kind == "up" and over.outcome == "Not bought: it never turned up 1.0% from its low first."
    assert item(session, bot, "A").dip_reference is None


def test_a_turn_without_a_free_slot_is_signalled_once_and_bought_later(session):
    bot = make_dip(session, rebound=True, rebound_pct=0.01, max_positions=1)
    session.add(Holding(bot_id=bot.id, symbol="B", shares=100, cost_basis=5_000.0, entry_price=50.0, reference_price=55.0,
                        target_price=55.0, stop_price=40.0))
    session.commit()
    broker = FakeBroker(now=T, prices={"A": 103.0, "B": 50.0})
    check(session, bot, broker)
    # 11:15 turned up 1.5% off the low of 103, 11:30 still up: no free slot either time
    for m, a_path in ((15, [103, 104.5]), (30, [103, 104.5, 104.6])):
        broker.now, broker.prices["A"] = _at(m), a_path[-1]
        check(session, bot, broker, now=_at(m), df=bars(A=[110] * 5 + a_path, B=[50] * (5 + len(a_path))))
    assert broker.sent == [] and [k for _, k in signals(session, bot)] == ["down", "rebound"]
    assert session.scalars(select(Signal).order_by(Signal.id.desc())).first().outcome == "All 1 slots in use."
    broker.prices.update(A=104.6, B=55.0)  # 11:45: B reaches its target, the slot frees up, A is still up from its low
    broker.now = _at(45)
    check(session, bot, broker, now=_at(45), df=bars(A=[110] * 5 + [103, 104.5, 104.6, 104.6], B=[50] * 8 + [55]))
    assert broker.sent[-1][:2] == ("BUY", "A")
    assert session.scalars(select(Signal).order_by(Signal.id.desc())).first().message.endswith("(still up from its low): bearish turned bullish, buy")


def test_old_bots_without_the_setting_buy_at_once(session):
    bot = make_dip(session, rebound=None)
    broker = FakeBroker(now=T, prices=QUOTES)
    check(session, bot, broker)
    assert broker.sent == [("BUY", "A", 48)]


def test_new_bots_wait_for_the_turn_by_default_and_it_can_be_switched(api):
    c, clock, broker = api
    bot = c.post("/bots/dip", json={**BODY, "rebound": True}).json()
    assert (bot["rebound"], bot["rebound_pct"]) == (True, 0.01)
    assert c.post("/bots/dip", json={k: v for k, v in BODY.items() if k != "rebound"}).json()["rebound"] is True
    clock["t"] = broker.now = OPEN
    d = c.post(f"/bots/{bot['id']}/run").json()
    assert d["action"] == "HOLD" and d["reasoning"] == "Waiting for the turn: A."
    a = {w["symbol"]: w for w in c.get(f"/bots/{bot['id']}").json()["watchlist"]}["A"]
    assert (a["dip_reference"], a["trough_price"], a["rebound_at"]) == (110.0, 103.0, 104.03)
    r = c.patch(f"/bots/{bot['id']}/dip", json={"rebound": False, "rebound_pct": 0.02})
    assert (r.json()["rebound"], r.json()["rebound_pct"]) == (False, 0.02)
    assert c.patch(f"/bots/{bot['id']}/dip", json={"rebound_pct": 0}).status_code == 422


def test_fractional_shares_are_off_by_default_and_can_be_switched(api):
    c, clock, broker = api
    bot = c.post("/bots/dip", json=BODY).json()
    assert bot["fractional"] is False
    bot = c.post("/bots/dip", json={**BODY, "fractional": True}).json()
    assert bot["fractional"] is True and "buys fractions of a share" in c.get(f"/bots/{bot['id']}/events").text
    assert c.patch(f"/bots/{bot['id']}/dip", json={"fractional": False}).json()["fractional"] is False


# --- Each symbol's own fall (drop_mode) ---------------------------------------------------------------

def test_by_price_a_pricier_stock_needs_a_smaller_fall(session):
    # x = 3%. Both fell 2.7%: A at $1,100 (tier $1,000 and up: a third of x = 1%) is bought, B at $55 (under $100: 3%) not
    bot = make_dip(session, drop_pct=0.03, drop_mode="price")
    broker = FakeBroker(now=T, prices={"A": 1070.0, "B": 53.5})
    check(session, bot, broker, df=bars(A=[1100] * 5 + [1070], B=[55] * 5 + [53.5]))
    assert broker.sent == [("BUY", "A", 4)]
    assert session.scalar(select(Signal)).message == ("A is 2.7% under its 1-hour high $1100.00 at $1070.00: buy zone "
                                                      "(a 1.0% fall at its price)")
    assert item(session, bot, "A").buy_drop == pytest.approx(0.01) and item(session, bot, "B").buy_drop == pytest.approx(0.03)
    assert not item(session, bot, "B").in_zone


def test_the_price_tiers_scale_with_x_and_can_be_changed(session):
    bot = make_dip(session, drop_pct=0.06, drop_mode="price")
    assert [round(dip.buy_fall(bot, p), 4) for p in (99.99, 100, 499, 500, 999, 1000, 5000)] == [0.06, 0.05, 0.05, 0.04, 0.04, 0.02, 0.02]
    assert dip.fall_label(bot) == "a fall of 6% by price (under $100: 6%, $100-500: 5%, $500-1,000: 4%, $1,000 and up: 2%)"
    bot = make_dip(session, drop_pct=0.04, drop_mode="price", price_tiers=[{"up_to": 200, "share": 1}, {"up_to": None, "share": 0.5}])
    assert (dip.buy_fall(bot, 150), dip.buy_fall(bot, 250), dip.buy_fall(bot, None)) == (0.04, 0.02, None)
    assert dip.buy_fall(make_dip(session, drop_pct=0.04), None) == 0.04  # by percent, as always


def test_by_daily_move_a_calm_stock_needs_a_smaller_fall(session):
    # 2x the daily move: A moves 1% a day (needs 2%: its 2.7% fall is bought), B 4% (needs 8%: its 3% isn't)
    bot = make_dip(session, drop_mode="volatility", drop_atr=2)
    broker = FakeBroker(now=T, prices={"A": 107.0, "B": 48.5})
    asked = []
    moves = lambda symbols, today: asked.append((symbols, today)) or {"A": 0.01, "B": 0.04}  # noqa: E731
    d = check(session, bot, broker, df=bars(A=[110] * 5 + [107], B=[50] * 5 + [48.5]), moves=moves)
    assert broker.sent == [("BUY", "A", 46)] and asked == [(["A", "B"], NY_DAY)]
    assert session.scalar(select(Signal)).message.endswith("buy zone (a 2.0% fall: 2x its 1.0% daily move)")
    assert d.steps[0].startswith("Buy a fall of 2x its usual daily move under the 1-hour high")
    assert item(session, bot, "B").buy_drop == pytest.approx(0.08)


def test_without_the_daily_moves_nothing_falls_into_the_buy_zone(session):
    bot = make_dip(session, drop_mode="volatility")
    broker = FakeBroker(now=T, prices=QUOTES)

    def down(symbols, today):
        raise BrokerError("Yahoo is down")

    d = check(session, bot, broker, kind="manual", moves=down)
    assert broker.sent == [] and "Daily moves failed (Yahoo is down)" in d.steps[1]
    assert "no daily move yet" in " ".join(d.steps)
    check(session, bot, broker, now=T15, moves=lambda symbols, today: {"B": 0.01})  # A's unknown: still not bought
    assert broker.sent == [] and item(session, bot, "A").buy_drop is None


def test_the_daily_move_is_the_average_range_of_the_days_before():
    idx = pd.bdate_range("2025-05-01", periods=16)
    close = pd.Series([100.0] * 15 + [110.0], index=idx)
    day = pd.DataFrame({"High": close + 1, "Low": close - 1, "Close": close})
    assert dip.daily_move(day.iloc[:13]) is None  # fewer than 14 days
    assert dip.daily_move(day.iloc[:15]) == pytest.approx(0.02)
    # A gap up from 100 to 110: that day's range runs from the previous close (11 = 10% of 110)
    assert dip.daily_move(day) == pytest.approx((13 * 0.02 + 0.1) / 14)


def test_the_fall_by_price_or_daily_move_is_set_and_changed_through_the_api(api):
    c, clock, broker = api
    tiers = [{"up_to": 200, "share": 1}, {"up_to": None, "share": 0.5}]
    bot = c.post("/bots/dip", json={**BODY, "drop_pct": 0.04, "drop_mode": "price", "price_tiers": tiers}).json()
    assert (bot["drop_mode"], bot["price_tiers"]) == ("price", tiers)
    assert "buys a fall of 4% by price (under $200: 4%, $200 and up: 2%)" in c.get(f"/bots/{bot['id']}/events").text
    clock["t"] = broker.now = OPEN
    c.post(f"/bots/{bot['id']}/run")
    w = {x["symbol"]: x for x in c.get(f"/bots/{bot['id']}").json()["watchlist"]}
    assert (w["A"]["buy_drop"], w["A"]["buy_below"]) == (0.04, 105.6)  # 4% under its $110 high
    r = c.patch(f"/bots/{bot['id']}/dip", json={"drop_mode": "volatility", "drop_atr": 2})
    assert (r.json()["drop_mode"], r.json()["drop_atr"]) == ("volatility", 2)
    bad = [{"up_to": 500, "share": 1}, {"up_to": 100, "share": 1}, {"up_to": None, "share": 1}]
    assert c.patch(f"/bots/{bot['id']}/dip", json={"price_tiers": bad}).status_code == 422
    assert c.post("/bots/dip", json={**BODY, "price_tiers": [{"up_to": 100, "share": 1}]}).status_code == 422
    assert c.post("/bots/dip", json=BODY).json()["drop_mode"] == "percent"


# --- News, trend, paused, preview -------------------------------------------------------------------

def test_bearish_news_blocks_the_buy_for_the_rest_of_the_day(session):
    bot = make_dip(session, news=True)
    asked = []

    def news(symbol, day, at_):
        asked.append((symbol, day))
        return {"sentiment": "bearish" if day == NY_DAY else "neutral", "steps": ["News (3 retrieved): bearish - recall"]}

    broker = FakeBroker(now=T, prices=dict(QUOTES))
    check(session, bot, broker, news=news)
    assert broker.sent == [] and asked == [("A", NY_DAY)]
    assert signals(session, bot) == [("A", "news"), ("A", "down")]
    assert "News (3 retrieved): bearish - recall" in session.scalar(select(Signal).where(Signal.kind == "news")).message
    a = item(session, bot, "A")
    assert a.news_blocked_on == NY_DAY and a.news_sentiment == "bearish"
    broker.now = T15
    check(session, bot, broker, now=T15, news=news, df=bars(A=[110] * 5 + [103, 103], B=[50] * 7))
    assert asked == [("A", NY_DAY)] and broker.sent == []  # not asked again today
    tomorrow = datetime(2025, 6, 3, 15, 0, 20, tzinfo=timezone.utc)
    broker.now = tomorrow
    check(session, bot, broker, now=tomorrow, news=news, df=bars(day=3, A=[110] * 5 + [103], B=[50] * 6))
    assert broker.sent == [("BUY", "A", 48)] and asked[-1] == ("A", date(2025, 6, 3))


def test_news_that_fails_still_buys_on_the_prices(session):
    bot = make_dip(session, news=True)
    broker = FakeBroker(now=T, prices=QUOTES)
    check(session, bot, broker, news=lambda *a: {"sentiment": "unavailable", "error": "down"})
    assert broker.sent == [("BUY", "A", 48)] and item(session, bot, "A").news_sentiment == "unavailable"


def test_the_trend_filter_skips_dips_in_a_downtrend(session):
    bot = make_dip(session, trend_filter=True)
    broker = FakeBroker(now=T, prices=QUOTES)
    d = check(session, bot, broker, trend=lambda symbols, day: {"A": False})
    assert broker.sent == [] and item(session, bot, "A").trend_ok is False
    assert "not in an uptrend" in d.reasoning


def test_a_paused_bot_still_exits_but_buys_nothing(session):
    bot, broker = _bought(session)
    bot.status = "paused"
    broker.prices.update(A=110.0, B=44.0)
    check(session, bot, broker, now=T15, df=bars(A=[110] * 5 + [103, 110], B=[50] * 6 + [44]))
    assert broker.sent[-1] == ("SELL", "A", 48) and len(broker.sent) == 2
    b = session.scalar(select(Signal).where(Signal.symbol == "B"))
    assert b.kind == "down" and b.outcome == "The bot is paused."


def test_a_preview_trades_nothing_and_changes_no_state(session):
    bot = make_dip(session)
    broker = FakeBroker(now=T, prices=QUOTES)
    d = check(session, bot, broker, kind="preview")
    assert broker.sent == [] and signals(session, bot) == [] and bot.last_decision_at is None
    assert d.kind == "preview" and "would buy" in d.reasoning and not item(session, bot, "A").in_zone
    check(session, bot, broker)  # the real check still sees a new dip
    assert signals(session, bot) == [("A", "down")]


def test_a_failed_download_is_retried(session):
    bot = make_dip(session)
    broker = FakeBroker(now=T, prices=QUOTES)

    def broken(*a):
        raise BrokerError("no prices from Yahoo")

    assert dip.check(session, bot, broker, T, "scheduled", bars_fn=broken) is None
    assert bot.last_decision_at is None
    assert "Prices failed" in session.scalar(select(Event).where(Event.level == "error")).message


# --- The scheduler ------------------------------------------------------------------------------------

def test_the_scheduler_checks_once_per_interval(session, monkeypatch):
    bot = make_dip(session)
    broker = FakeBroker(prices=QUOTES)
    monkeypatch.setattr(rotation, "yahoo_closes", lambda symbols, start: pd.DataFrame(
        {s: [QUOTES[s]] for s in symbols}, index=pd.DatetimeIndex(["2025-06-02"])))
    calls = []

    def counting(symbols, interval, start):
        calls.append(interval)
        return FALL.reindex(columns=symbols)

    fns = {"bars_fn": counting, "news_fn": no_news}
    for t in (T, T + timedelta(seconds=30), T + timedelta(minutes=14)):
        broker.now = t
        scheduler.process_bot(bot.id, t, broker_factory=lambda b: broker, dip_fns=fns)
    assert calls == ["15m"] and broker.sent == [("BUY", "A", 48)]
    broker.now = T15
    scheduler.process_bot(bot.id, T15, broker_factory=lambda b: broker, dip_fns=fns)
    assert len(calls) == 2


# --- The API --------------------------------------------------------------------------------------------

CLOSED, OPEN = at(23, 0), T  # Monday 19:00 and 11:00 New York
BODY = {"symbols": ["a", "b"], "allocated_cash": 10_000, "slippage_pct": 0, "interval": "15m", "lookback": 1,
        "lookback_unit": "hours", "max_positions": 2, "rebound": False}  # buy at once: these tests are about the rest


@pytest.fixture
def api(monkeypatch):
    clock = {"t": CLOSED}
    broker = FakeBroker(now=CLOSED, prices=dict(QUOTES))
    monkeypatch.setattr(main, "utcnow", lambda: clock["t"])
    monkeypatch.setattr(main, "get_broker", lambda bot: broker)
    known = {"A": 103.0, "B": 50.0, "C": 20.0}
    monkeypatch.setattr(rotation, "yahoo_closes", lambda symbols, start: pd.DataFrame(
        {s: [known[s]] for s in symbols if s in known}, index=pd.DatetimeIndex(["2025-06-02"])).reindex(columns=symbols))
    monkeypatch.setattr(dip, "yahoo_bars", feed(bars(A=[110] * 5 + [103], B=[50] * 6, C=[20] * 6)))
    monkeypatch.setattr(dip, "get_news", lambda *a: {"sentiment": "neutral"})
    with TestClient(main.app) as c:
        yield c, clock, broker


def test_create_a_dip_bot_and_trade_it_through_the_api(api):
    c, clock, broker = api
    r = c.post("/bots/dip", json=BODY)
    assert r.status_code == 201, r.text
    bot = r.json()
    assert bot["strategy"] == "dip_buyer" and bot["name"] == "Dip buyer on 2 symbols" and bot["universe"] == ["A", "B"]
    assert [w["symbol"] for w in bot["watchlist"]] == ["A", "B"] and bot["interval"] == "15m"
    assert bot["next_decision_at"] is not None and bot["equity"] == 10_000

    d = c.post(f"/bots/{bot['id']}/run").json()  # closed: a preview
    assert d["kind"] == "preview" and "would buy" in d["outcome"]
    clock["t"] = broker.now = OPEN
    d = c.post(f"/bots/{bot['id']}/run").json()
    assert d["kind"] == "manual" and d["action"] == "BUY"

    detail = c.get(f"/bots/{bot['id']}").json()
    a = detail["holdings"][0]
    assert (a["symbol"], a["shares"], a["target_price"], a["stop_price"], a["on_watchlist"]) == ("A", 48, 110.0, 97.85, True)
    w = {x["symbol"]: x for x in detail["watchlist"]}
    assert w["A"]["held"] and w["A"]["in_zone"] and w["A"]["buy_below"] == 104.5 and not w["B"]["held"]
    sig = c.get(f"/bots/{bot['id']}/signals").json()
    assert [(s["symbol"], s["kind"]) for s in sig] == [("A", "down")]
    assert c.get("/signals", params={"after_id": sig[0]["id"]}).json() == []

    sold = c.post(f"/bots/{bot['id']}/holdings/A/sell")
    assert sold.status_code == 200 and sold.json()["reason"] == "manual"
    assert c.post(f"/bots/{bot['id']}/archive").json()["status"] == "archived"


def test_the_watchlist_changes_while_it_runs(api):
    c, _, _ = api
    bot = c.post("/bots/dip", json=BODY).json()
    r = c.post(f"/bots/{bot['id']}/watchlist", json={"symbols": ["c", "a"]})
    assert r.status_code == 200 and [w["symbol"] for w in r.json()["watchlist"]] == ["A", "B", "C"]
    assert c.post(f"/bots/{bot['id']}/watchlist", json={"symbols": ["A"]}).status_code == 409
    r = c.post(f"/bots/{bot['id']}/watchlist", json={"symbols": ["ZZZZ"]})
    assert r.status_code == 422 and "no prices for ZZZZ" in r.text
    r = c.delete(f"/bots/{bot['id']}/watchlist/b")
    assert [w["symbol"] for w in r.json()["watchlist"]] == ["A", "C"]
    assert c.delete(f"/bots/{bot['id']}/watchlist/B").status_code == 404

    r = c.post(f"/bots/{bot['id']}/watchlist/A/blacklist")
    assert {w["symbol"]: w["status"] for w in r.json()["watchlist"]}["A"] == "blacklisted"
    r = c.post(f"/bots/{bot['id']}/watchlist/A/enable")
    assert {w["symbol"]: w["status"] for w in r.json()["watchlist"]}["A"] == "watching"
    assert c.post(f"/bots/{bot['id']}/watchlist/A/enable").status_code == 409
    events = [e["message"] for e in c.get(f"/bots/{bot['id']}/events").json()]
    assert "Watchlist: added C" in events and "A re-enabled by user" in events


def test_a_removed_symbol_that_is_held_still_exits(api):
    c, clock, broker = api
    bot = c.post("/bots/dip", json=BODY).json()
    clock["t"] = broker.now = OPEN
    c.post(f"/bots/{bot['id']}/run")
    r = c.delete(f"/bots/{bot['id']}/watchlist/A").json()
    assert r["holdings"][0]["symbol"] == "A" and r["holdings"][0]["on_watchlist"] is False
    assert "still held" in c.get(f"/bots/{bot['id']}/events").json()[0]["message"]


def test_the_rules_and_the_news_switch_change_while_it_runs(api):
    c, _, _ = api
    bot = c.post("/bots/dip", json=BODY).json()
    r = c.patch(f"/bots/{bot['id']}/dip", json={"news": True, "drop_pct": 0.08, "interval": "5m"})
    assert r.status_code == 200 and (r.json()["news"], r.json()["drop_pct"], r.json()["interval"]) == (True, 0.08, "5m")
    r = c.patch(f"/bots/{bot['id']}/dip", json={"interval": "1d"})  # with a window in hours
    assert r.status_code == 422 and "not hours" in r.text
    r = c.patch(f"/bots/{bot['id']}", json={"strategy": "momentum"})
    assert r.status_code == 422 and "/dip" in r.text
    assert c.patch(f"/bots/{bot['id']}", json={"name": "Dips", "stop_pct": 0.07}).status_code == 200


def test_dip_bots_are_checked_and_paper_only(api, monkeypatch):
    c, _, _ = api
    assert c.post("/bots/dip", json={**BODY, "symbols": ["A", "BTC-USD"]}).status_code == 422
    assert c.post("/bots/dip", json={**BODY, "symbols": []}).status_code == 422
    r = c.post("/bots/dip", json={**BODY, "interval": "5m", "lookback": 60, "lookback_unit": "days"})
    assert r.status_code == 422 and "at most 40 trading days" in r.text
    r = c.post("/bots/dip", json={**BODY, "symbols": ["A", "ZZZZ"]})
    assert r.status_code == 422 and "no prices for ZZZZ" in r.text
    monkeypatch.setattr(main, "catalog", lambda: [{**b, "available": True} for b in real_catalog()])
    r = c.post("/bots/dip", json={**BODY, "broker": "alpaca-live"})
    assert r.status_code == 422 and "paper accounts only" in r.text


def test_one_bot_per_symbol_on_a_real_account_counts_the_watchlist(api, monkeypatch):
    c, _, _ = api
    monkeypatch.setattr(main, "catalog", lambda: [{**b, "available": True} for b in real_catalog()])
    bot = c.post("/bots/dip", json={**BODY, "broker": "alpaca-paper"}).json()
    assert c.post("/bots", json={"symbol": "A", "broker": "alpaca-paper"}).status_code == 409
    assert c.post("/bots", json={"symbol": "C", "broker": "alpaca-paper"}).status_code == 201
    r = c.post(f"/bots/{bot['id']}/watchlist", json={"symbols": ["C"]})
    assert r.status_code == 409 and "already trades C" in r.text


SETUP = {"symbols": ["xlk", "XLF", "xlk"], "interval": "1d", "drop_pct": 0.07, "lookback": 60, "stop_pct": 0.1,
         "reenable_days": 20, "initial_cash": 25_000, "practice": ["2016-01-01", "2020-12-31"], "exam": None,
         "period_months": None}


def test_a_setup_is_saved_under_a_name_and_replaced_by_saving_it_again(api):
    c, _, _ = api
    r = c.post("/dip/presets", json={"name": "  abc ", "config": SETUP})
    assert r.status_code == 200, r.text
    abc = r.json()
    cfg = abc["config"]
    assert abc["name"] == "abc" and cfg["symbols"] == ["XLK", "XLF"] and cfg["lookback"] == 60  # longer than a bot's feed
    assert (cfg["practice"], cfg["exam"], cfg["rebound"], cfg["rebound_pct"]) == (["2016-01-01", "2020-12-31"], None, True, 0.01)
    c.post("/dip/presets", json={"name": "Big dips", "config": {**SETUP, "drop_pct": 0.15}})
    assert [p["name"] for p in c.get("/dip/presets").json()] == ["abc", "Big dips"]  # by name, ignoring case

    again = c.post("/dip/presets", json={"name": "ABC", "config": {**SETUP, "stop_pct": 0.05, "period_months": 3}}).json()
    assert again["id"] == abc["id"] and again["name"] == "ABC" and again["config"]["stop_pct"] == 0.05
    assert again["config"]["period_months"] == 3  # "the last 3 months", recounted from the day it is loaded
    assert len(c.get("/dip/presets").json()) == 2

    assert c.delete(f"/dip/presets/{abc['id']}").status_code == 204
    assert [p["name"] for p in c.get("/dip/presets").json()] == ["Big dips"]
    assert c.delete(f"/dip/presets/{abc['id']}").status_code == 404


def test_a_saved_setup_is_checked(api):
    c, _, _ = api
    assert c.post("/dip/presets", json={"name": " ", "config": SETUP}).status_code == 422
    assert c.post("/dip/presets", json={"name": "x", "config": {**SETUP, "symbols": ["BTC-USD"]}}).status_code == 422
    assert c.post("/dip/presets", json={"name": "x", "config": {**SETUP, "drop_pct": 0}}).status_code == 422
    r = c.post("/dip/presets", json={"name": "x", "config": {**SETUP, "lookback_unit": "hours"}})
    assert r.status_code == 422 and "not hours" in r.text
    assert c.post("/dip/presets", json={"name": "x", "config": {**SETUP, "period_months": 0}}).status_code == 422
    r = c.post("/dip/presets", json={"name": "x", "config": {**SETUP, "exam": ["2021-01-01", "2020-01-01"]}})
    assert r.status_code == 422 and "end after it starts" in r.text
    assert c.get("/dip/presets").json() == []


STUDY = {"name": " Sweep  1 ", "description": "drop 3-7%", "periods": {"practice": ["2025-07-09", "2026-01-08"],
                                                                        "exam": ["2026-01-09", "2026-10-09"]}}


def one_setup(code: str, score: float | None, **kw) -> dict:
    return {"code": code, "name": f"{code} ETFs 4%/5d", "watchlist": "ETFs", "config": SETUP, "score": score,
            "results": {"practice": {"total_return_pct": 3.2}, "exam": {"total_return_pct": score}},
            "detail": {"exam": {"curve": [["2026-01-09", 400, 400]]}}, **kw}


def test_a_study_keeps_its_tests_best_first_and_their_curves_on_demand(api):
    c, _, _ = api
    r = c.put("/dip/studies", json=STUDY)
    assert r.status_code == 200, r.text
    study = r.json()
    assert (study["name"], study["tests"], study["periods"]["exam"]) == ("Sweep 1", 0, ["2026-01-09", "2026-10-09"])
    r = c.post(f"/dip/studies/{study['id']}/tests",
               json=[one_setup("T1", 1.5), one_setup("T2", None), one_setup("T3", 4.0, pick=1, extra="skip dips after earnings",
                                                                                     quarters={"won": 9, "of": 12, "worst": -4.2, "returns": [1.0] * 12})])
    assert r.status_code == 200, r.text
    assert r.json()["tests"] == 3
    tests = c.get(f"/dip/studies/{study['id']}/tests").json()
    assert [t["code"] for t in tests] == ["T3", "T1", "T2"]  # best score first, none last
    assert tests[0]["pick"] == 1 and tests[0]["config"]["symbols"] == ["XLK", "XLF"] and "detail" not in tests[0]
    assert (tests[0]["extra"], tests[1]["extra"]) == ("skip dips after earnings", None)
    assert (tests[0]["quarters"]["won"], tests[1]["quarters"]) == (9, None)
    one = c.get(f"/dip/tests/{tests[0]['id']}").json()
    assert one["detail"]["exam"]["curve"] == [["2026-01-09", 400, 400]]
    assert c.get("/dip/tests/999").status_code == 404

    # The same code again replaces that test; a study saved again under its name starts over
    c.post(f"/dip/studies/{study['id']}/tests", json=[one_setup("T1", 9.0)])
    assert [t["code"] for t in c.get(f"/dip/studies/{study['id']}/tests").json()] == ["T1", "T3", "T2"]
    again = c.put("/dip/studies", json={**STUDY, "name": "sweep 1", "description": "again"}).json()
    assert (again["id"], again["tests"], again["description"]) == (study["id"], 0, "again")
    assert [s["name"] for s in c.get("/dip/studies").json()] == ["sweep 1"]

    assert c.delete(f"/dip/studies/{study['id']}").status_code == 204
    assert c.get("/dip/studies").json() == [] and c.get(f"/dip/studies/{study['id']}/tests").status_code == 404


def test_a_study_and_its_tests_are_checked(api):
    c, _, _ = api
    assert c.put("/dip/studies", json={**STUDY, "name": " "}).status_code == 422
    assert c.put("/dip/studies", json={**STUDY, "periods": {}}).status_code == 422
    study = c.put("/dip/studies", json=STUDY).json()
    assert c.post(f"/dip/studies/{study['id']}/tests", json=[one_setup("T1", 1.0), one_setup("T1", 2.0)]).status_code == 422
    bad = one_setup("T2", 1.0, config={**SETUP, "drop_pct": 0})
    assert c.post(f"/dip/studies/{study['id']}/tests", json=[bad]).status_code == 422
    assert c.post("/dip/studies/999/tests", json=[one_setup("T1", 1.0)]).status_code == 404
