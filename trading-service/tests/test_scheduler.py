"""The scheduler: decides at the right moment, exactly once per day, and keeps protecting positions."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from conftest import FakeBroker, at, make_bot, signal
from sqlalchemy import select

from app import rotation, scheduler
from app.db import SessionLocal
from app.decision_client import SignalError
from app.market import ny_date
from app.models import DIP, Bot, Event, Holding, Order, WatchItem
from app.trader import evaluate


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


def test_a_tick_checks_the_real_money_bots_first(session, monkeypatch):
    for broker in ("paper", "alpaca-live", "alpaca-paper", "alpaca-live"):
        make_bot(session, broker=broker)
    make_bot(session, broker="alpaca-live", status="archived")
    order = []
    monkeypatch.setattr(scheduler, "process_bot", lambda bot_id, *a: order.append(bot_id))
    monkeypatch.setattr(scheduler, "ThreadPoolExecutor", lambda max_workers: contextlib.nullcontext(SimpleNamespace(map=map)))
    scheduler.tick(at(15, 0))
    assert order == [2, 4, 1, 3]


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

    def flaky(bot, as_of, at=None):
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



# --- Decision slots: after the open and/or before the close -----------------------------------------
# June 2025 = New York summer time (UTC-4): 10:00 New York = 14:00 UTC, 15:30 New York = 19:30 UTC.

def _ticks(bot_id, times, sig, broker=None):
    for t in times:
        scheduler.process_bot(bot_id, t, sig, lambda b: broker or FakeBroker(now=t))


def test_a_both_bot_decides_after_the_open_and_again_before_the_close(session):
    bot = make_bot(session, decide_at="both")
    sig = signal("HOLD")
    _ticks(bot.id, [at(13, 45), at(14, 0), at(14, 5), at(14, 40), at(19, 0), at(19, 30), at(19, 45)], sig)
    assert sig.times == [at(14, 0), at(19, 30)]  # 10:00 and 15:30 New York, once each


def test_an_open_bot_decides_only_in_the_morning(session):
    bot = make_bot(session, decide_at="open")
    sig = signal("HOLD")
    _ticks(bot.id, [at(14, 0), at(19, 30), at(14, 0, day=3)], sig)
    assert sig.times == [at(14, 0), at(14, 0, day=3)]


def test_the_morning_slot_is_skipped_if_missed_but_the_close_still_happens(session):
    bot = make_bot(session, decide_at="both")
    sig = signal("HOLD")
    _ticks(bot.id, [at(14, 30), at(19, 30)], sig)  # service was down 10:00-10:30 New York
    assert sig.times == [at(19, 30)]


def test_rebalance_days_picks_the_days_and_decide_at_the_times(session):
    bot = make_bot(session, decide_at="both", rebalance_days=2)
    sig = signal("HOLD")
    for day in (2, 3, 4):  # Mon, Tue, Wed
        _ticks(bot.id, [at(14, 0, day=day), at(19, 30, day=day)], sig)
    assert sig.times == [at(14, 0), at(19, 30), at(14, 0, day=4), at(19, 30, day=4)]  # Tuesday skipped


def test_restart_between_slots_neither_repeats_the_morning_nor_skips_the_close(session):
    bot = make_bot(session, decide_at="both")
    sig = signal("HOLD")
    _ticks(bot.id, [at(14, 0)], sig)
    scheduler._last_watch.clear()
    scheduler._retry_after.clear()  # simulate a restart
    _ticks(bot.id, [at(14, 10), at(19, 30)], sig)
    assert sig.times == [at(14, 0), at(19, 30)]


def test_run_now_counts_as_the_next_decision_of_the_day(session):
    """A manual decision at 11:00 is today's close decision made early (as before slots existed)."""
    bot = make_bot(session)  # decide_at="close"
    evaluate(session, bot, FakeBroker(now=at(15, 0)), at(15, 0), "manual", signal("HOLD"))
    sig = signal("HOLD")
    _ticks(bot.id, [at(19, 30)], sig)
    assert sig.times == []


def test_next_decision_at_follows_the_bots_slots_and_rebalance_days(session):
    both = make_bot(session, decide_at="both")
    assert scheduler.next_decision_at(both, at(12, 0)) == at(14, 0)  # 08:00 New York -> 10:00
    _ticks(both.id, [at(14, 0)], signal("HOLD"))
    both = _reload(both.id)
    assert scheduler.next_decision_at(both, at(14, 10)) == at(19, 30)  # morning done -> 15:30

    weekly = make_bot(session, rebalance_days=5, last_decision_date=date(2025, 6, 2), last_decision_at=at(19, 30))
    assert scheduler.next_decision_at(weekly, at(20, 0)) == at(19, 30, day=9)  # 5 trading days later

    paused = make_bot(session, status="paused")
    assert scheduler.next_decision_at(paused, at(12, 0)) is None


def test_a_pass_reads_alpaca_once_for_every_symbol_its_bots_may_price(session, monkeypatch):
    from app.brokers import alpaca
    from app.models import Holding, WatchItem

    live = make_bot(session, broker="alpaca-live", symbol="DIP", strategy="dip_buyer")
    paper = make_bot(session, broker="alpaca-paper", symbol="DIP", strategy="dip_buyer")
    make_bot(session, broker="alpaca-paper", symbol="MSFT")  # a single-symbol bot: its one symbol
    sim = make_bot(session, broker="paper", symbol="DIP", strategy="dip_buyer")  # the simulator prices through Yahoo
    gone = make_bot(session, broker="alpaca-paper", symbol="DIP", strategy="dip_buyer", status="archived")
    for bot, symbol in ((live, "GLD"), (live, "URA"), (paper, "URA"), (paper, "BE"), (sim, "TSLA"), (gone, "META")):
        session.add(WatchItem(bot_id=bot.id, symbol=symbol, status="watching"))
    session.add(Holding(bot_id=paper.id, symbol="NVT", shares=1.0, cost_basis=150.0))  # off the list, still held
    session.add(Holding(bot_id=sim.id, symbol="AMD", shares=1.0, cost_basis=150.0))
    session.commit()
    assert scheduler.round_symbols(session) == ["BE", "GLD", "MSFT", "NVT", "URA"]

    seen = []
    monkeypatch.setattr(scheduler, "process_bot", lambda bot_id, *a: seen.append(alpaca._round.symbols))
    scheduler.tick(at(15, 0))
    assert seen and all(s == ["BE", "GLD", "MSFT", "NVT", "URA"] for s in seen)
    assert alpaca._round is None  # over with the pass


def test_a_pass_looks_up_the_paper_orders_still_working_from_one_read_of_the_account(session, monkeypatch):
    from app.brokers import alpaca
    from app.models import OPEN_ORDER_STATUSES

    paper = make_bot(session, broker="alpaca-paper", symbol="DIP", strategy="dip_buyer")
    live = make_bot(session, broker="alpaca-live", symbol="DIP", strategy="dip_buyer")
    single = make_bot(session, broker="alpaca-paper", symbol="MSFT")
    now = at(15, 0)

    def order(bot, created, status="submitted", kind="market"):
        session.add(Order(bot_id=bot.id, side="BUY", symbol="A", qty=1, reason="dip", status=status, order_type=kind,
                          client_order_id=f"bot{bot.id}-{created:%H%M}-{status}-{kind}", created_at=created, updated_at=created))
        session.commit()

    assert scheduler.round_orders_since(session, now) is None  # nothing working: no list to read
    order(live, at(14, 0))  # the live account's: looked up on its own
    order(single, at(13, 50), kind="stop")  # a stop resting for days: on its own too
    order(paper, at(14, 10), status="filled")
    assert scheduler.round_orders_since(session, now) is None
    order(paper, at(14, 45))
    order(single, at(14, 50), status="new")
    assert scheduler.round_orders_since(session, now) == at(14, 44)  # the oldest working one, a minute earlier
    order(paper, at(12, 0))  # one stuck for hours: the list goes back an hour at most, older ones on their own
    assert scheduler.round_orders_since(session, now) == at(13, 59)
    assert "submitted" in OPEN_ORDER_STATUSES

    seen = []
    monkeypatch.setattr(scheduler, "process_bot", lambda bot_id, *a: seen.append(alpaca._round.orders_since))
    scheduler.tick(now)
    assert seen and all(s == at(13, 59) for s in seen)


def test_a_scheduled_dip_check_sends_its_orders_without_waiting_and_books_them_from_the_passs_read(session, monkeypatch):
    from app import dip
    from app.brokers import alpaca

    bot = make_bot(session, broker="alpaca-paper", symbol="DIP", strategy="dip_buyer", interval="15m")
    waiting = []

    class Recording(FakeBroker):
        def submit(self, *a, **kw):
            waiting.append(not alpaca._scheduled.get())
            return super().submit(*a, **kw)

    broker = Recording(now=at(15, 0))
    session.add(Order(bot_id=bot.id, side="BUY", symbol="A", qty=1, reason="dip", status="submitted",
                      client_order_id="sent", created_at=at(14, 45), updated_at=at(14, 45)))
    session.commit()

    def check(session, bot, broker, now, kind, **fns):
        broker.submit("A", "BUY", 1, f"{kind}-1")

    monkeypatch.setattr(dip, "check", check)
    scheduler.process_bot(bot.id, at(15, 0), broker_factory=lambda b: broker)
    assert waiting == [False]  # the scheduled check: no wait for the fill
    assert ("sent", True) in broker.lookups  # its earlier order: from the pass's read of the account's orders
    with SessionLocal() as s:
        dip.check(s, s.get(Bot, bot.id), broker, at(15, 0), "manual")  # "Run now" from the API waits, as before
    assert waiting == [False, True] and not alpaca._scheduled.get()


def test_the_live_bots_have_a_pass_of_their_own(session, monkeypatch):
    from app.brokers import alpaca
    from app.models import Holding

    bots = {broker: make_bot(session, broker=broker, symbol=symbol)
            for broker, symbol in (("paper", "AAPL"), ("alpaca-paper", "MSFT"), ("alpaca-live", "GLD"))}
    make_bot(session, broker="alpaca-live", symbol="URA", status="archived")
    session.add(Holding(bot_id=bots["alpaca-paper"].id, symbol="NVT", shares=1.0, cost_basis=150.0))
    session.commit()
    assert scheduler.round_symbols(session, live=True) == ["GLD"]
    assert scheduler.round_symbols(session, live=False) == ["MSFT", "NVT"]
    assert scheduler.round_symbols(session) == ["GLD", "MSFT", "NVT"]

    seen, splits_for = [], []
    half = timedelta(seconds=30)
    clock = {"t": at(15, 0) + half}
    monkeypatch.setattr(scheduler, "utcnow", lambda: clock["t"])
    monkeypatch.setattr(scheduler, "process_bot", lambda bot_id, *a: seen.append((bot_id, alpaca._round.symbols)))
    monkeypatch.setattr(scheduler.splits, "apply_all", lambda now, live=None: splits_for.append(live))
    monkeypatch.setattr(scheduler, "state", dict(scheduler.state, last_tick=None, live_tick=None))
    scheduler.tick(at(15, 0), live=True)
    assert seen == [(bots["alpaca-live"].id, ["GLD"])] and splits_for == [True]
    assert (scheduler.state["live_tick"], scheduler.state["last_tick"]) == (at(15, 0) + half, None)  # when it finished
    seen.clear()
    clock["t"] = at(15, 1) + half
    scheduler.tick(at(15, 1), live=False)
    assert sorted(i for i, _ in seen) == [bots["paper"].id, bots["alpaca-paper"].id] and splits_for == [True, False]
    assert (scheduler.state["live_tick"], scheduler.state["last_tick"]) == (at(15, 0) + half, at(15, 1) + half)
    seen.clear()
    clock["t"] = at(15, 2) + half
    scheduler.tick(at(15, 2))  # every bot, the real-money one first
    assert [i for i, _ in seen][0] == bots["alpaca-live"].id and len(seen) == 3
    assert (scheduler.state["live_tick"], scheduler.state["last_tick"]) == (at(15, 2) + half, at(15, 2) + half)


def dip_bot_with(session, symbols, **fields):
    bot = make_bot(session, **{
        "symbol": "DIP", "strategy": DIP, "interval": "15m", "drop_pct": 0.05, "lookback": 5, "lookback_unit": "days",
        "drop_from": "high", "target_mode": "reference", "rise_pct": 0.05, "stop_pct": 0.05, "max_positions": 2,
        "max_hold_days": 0, "news": False, "trend_filter": False, "universe": list(symbols),
        "benchmark_prices": {s: 100.0 for s in symbols}, "benchmark_price": 10_000.0, "last_price": 10_000.0, **fields})
    for s in symbols:
        session.add(WatchItem(bot_id=bot.id, symbol=s, status="watching"))
    session.commit()
    return bot


def _passing(session, monkeypatch, symbols_per_bot, **fields):
    """Dip bots due for a check and a look at their universe at 11:00:20 New York, and what Yahoo was asked."""
    bots = [dip_bot_with(session, symbols, **fields) for symbols in symbols_per_bot]
    asked = {"bars": [], "daily": []}

    def bars(symbols, interval, start):
        asked["bars"].append((list(symbols), interval, start))
        idx = pd.date_range("2025-06-02 09:45", periods=8, freq="15min", tz="America/New_York")
        return pd.DataFrame({s: [100.0] * 8 for s in symbols}, index=idx)

    def closes(symbols, start):
        asked["daily"].append((list(symbols), start))
        return pd.DataFrame({s: [100.0] for s in symbols}, index=pd.DatetimeIndex(["2025-06-02"]))

    monkeypatch.setattr(scheduler.dip, "yahoo_bars", bars)
    monkeypatch.setattr(rotation, "yahoo_closes", closes)
    monkeypatch.setattr(scheduler.splits, "apply_all", lambda now, live=None: None)
    monkeypatch.setattr(scheduler, "state", dict(scheduler.state, last_tick=None, live_tick=None, **{"pass": None}))
    return bots, asked


T_CHECK = datetime(2025, 6, 2, 15, 0, 20, tzinfo=timezone.utc)  # Monday 11:00:20 New York: a 15-minute bar has just closed


def test_a_pass_asks_yahoo_once_for_all_its_bots(session, monkeypatch):
    """However many dip bots are due: one read of bars and one of daily closes at the pass's start, each symbol once, from
    the longest window any of them needs. The bots all read from that."""
    bots, asked = _passing(session, monkeypatch, [["A", "B"], ["B", "C"], ["C", "D"]])
    bots[2].lookback = 20
    session.add(Holding(bot_id=bots[0].id, symbol="H", shares=1.0, cost_basis=10.0, entry_price=10.0, last_price=10.0,
                        opened_at=T_CHECK - timedelta(days=1)))
    session.commit()
    today = ny_date(T_CHECK)
    want = scheduler.dip.history_start(bots[2], today)
    assert want < scheduler.dip.history_start(bots[0], today)
    broker = FakeBroker(now=T_CHECK, prices={s: 100.0 for s in "ABCDH"})
    scheduler.tick(T_CHECK, broker_factory=lambda b: broker, live=False)
    assert asked["bars"] == [(["A", "B", "C", "D", "H"], "15m", want)]
    assert asked["daily"] == [(["A", "B", "C", "D"], today - timedelta(days=rotation.UNIVERSE_DAYS))]
    with SessionLocal() as s:
        assert all(s.get(Bot, b.id).last_decision_at == T_CHECK for b in bots)  # each was checked, on those prices
        assert {i.last_price for i in s.scalars(select(WatchItem))} == {100.0}


def test_a_pass_with_nothing_due_asks_yahoo_nothing(session, monkeypatch):
    bots, asked = _passing(session, monkeypatch, [["A", "B"]])
    broker = FakeBroker(now=T_CHECK, prices={"A": 100.0, "B": 100.0})
    scheduler.tick(T_CHECK, broker_factory=lambda b: broker, live=False)
    asked["bars"].clear(), asked["daily"].clear()
    later = T_CHECK + timedelta(seconds=30)  # checked, and looked at less than RISK_CHECK_MINUTES ago
    broker.now = later
    scheduler.tick(later, broker_factory=lambda b: broker, live=False)
    assert asked == {"bars": [], "daily": []}
    closed = datetime(2025, 6, 2, 21, 30, tzinfo=timezone.utc)  # the market has closed
    broker.now = closed
    scheduler.tick(closed, broker_factory=lambda b: broker, live=False)
    assert asked == {"bars": [], "daily": []}


def test_the_live_pass_reads_only_the_live_bots_symbols(session, monkeypatch):
    bots, asked = _passing(session, monkeypatch, [["A"]])
    bots[0].broker = "alpaca-paper"
    live = dip_bot_with(session, ["L"], broker="alpaca-live")
    session.commit()
    broker = FakeBroker(now=T_CHECK, prices={"A": 100.0, "L": 100.0})
    scheduler.tick(T_CHECK, broker_factory=lambda b: broker, live=True)
    assert [a[0] for a in asked["bars"]] == [["L"]] and [a[0] for a in asked["daily"]] == [["L"]]
    with SessionLocal() as s:
        assert s.get(Bot, live.id).last_decision_at == T_CHECK and s.get(Bot, bots[0].id).last_decision_at is None


def test_a_failed_read_of_a_pass_is_one_failure_not_one_per_bot(session, monkeypatch):
    """Yahoo and Alpaca both down: the read fails once, every bot's check gets that error (their exits still run on the
    broker's quotes), and it is tried again at the retry, not at once by each bot."""
    bots, asked = _passing(session, monkeypatch, [["A", "B"], ["B", "C"]])

    def down(symbols, interval, start):
        asked["bars"].append(list(symbols))
        raise scheduler.BrokerError("no prices from Yahoo")

    monkeypatch.setattr(scheduler.dip, "yahoo_bars", down)
    broker = FakeBroker(now=T_CHECK, prices={s: 100.0 for s in "ABC"})
    scheduler.tick(T_CHECK, broker_factory=lambda b: broker, live=False)
    assert asked["bars"] == [["A", "B", "C"]]
    with SessionLocal() as s:
        for b in bots:
            assert s.get(Bot, b.id).last_decision_at is None
            assert any("Prices failed" in e.message for e in s.scalars(select(Event).where(Event.bot_id == b.id)))
    assert all(scheduler._retry_after[b.id] == T_CHECK + scheduler.SIGNAL_RETRY for b in bots)
    later = T_CHECK + timedelta(minutes=1)
    broker.now = later
    scheduler.tick(later, broker_factory=lambda b: broker, live=False)
    assert asked["bars"] == [["A", "B", "C"]]  # not yet


def test_a_broken_bot_doesnt_stop_the_others_from_getting_their_prices(session, monkeypatch):
    bots, asked = _passing(session, monkeypatch, [["A", "B"]])
    broken = make_bot(session, symbol="DIP", strategy=DIP, universe=["Z"])  # no interval: its check can't be worked out
    session.add(WatchItem(bot_id=broken.id, symbol="Y", status="watching"))
    session.commit()
    broker = FakeBroker(now=T_CHECK, prices={"A": 100.0, "B": 100.0})
    scheduler.tick(T_CHECK, broker_factory=lambda b: broker, live=False)
    assert asked["bars"] == [(["A", "B"], "15m", scheduler.dip.history_start(bots[0], ny_date(T_CHECK)))]
    with SessionLocal() as s:
        assert s.get(Bot, bots[0].id).last_decision_at == T_CHECK


def test_a_pass_reports_how_far_it_has_got_and_the_live_pass_leaves_that_alone(session, monkeypatch):
    """/health tells slow from stuck by the paper bots' pass: its start, its last sign of life, how many bots are done.
    The live bots' pass, going on beside it, must not look like progress of the paper bots."""
    bots, _ = _passing(session, monkeypatch, [["A"], ["B"]])
    clock = {"t": T_CHECK}
    monkeypatch.setattr(scheduler, "utcnow", lambda: clock["t"])
    seen = []

    def process(bot_id, *a):
        seen.append(dict(scheduler.state["pass"]))
        clock["t"] += timedelta(minutes=1)

    monkeypatch.setattr(scheduler, "process_bot", process)
    monkeypatch.setattr(scheduler, "ThreadPoolExecutor", lambda max_workers: ThreadPoolExecutor(max_workers=1))
    scheduler.tick(T_CHECK, live=False)
    assert seen[0]["total"] == 2 and seen[0]["started_at"] == T_CHECK and seen[0]["done"] == 0
    assert seen[1]["started_at"] == T_CHECK and seen[1]["progress_at"] > T_CHECK and seen[1]["done"] == 1
    assert scheduler.state["pass"] is None and scheduler.state["last_tick"] == clock["t"]

    going = {"started_at": T_CHECK, "progress_at": T_CHECK, "done": 7, "total": 235}
    scheduler.state["pass"] = going
    dip_bot_with(session, ["L"], broker="alpaca-live")
    monkeypatch.setattr(scheduler, "process_bot", lambda bot_id, *a: None)
    scheduler.tick(T_CHECK, live=True)
    assert scheduler.state["pass"] is going and going == {"started_at": T_CHECK, "progress_at": T_CHECK, "done": 7, "total": 235}


def test_a_failed_pass_doesnt_stay_marked_as_going(session, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("no database")

    monkeypatch.setattr(scheduler, "state", dict(scheduler.state, **{"pass": None}))
    monkeypatch.setattr(scheduler, "round_symbols", boom)
    monkeypatch.setattr(scheduler.splits, "apply_all", lambda now, live=None: None)
    with pytest.raises(RuntimeError):
        scheduler.tick(T_CHECK, live=False)
    assert scheduler.state["pass"] is None


def test_a_long_pass_of_the_other_bots_never_holds_the_live_ones_back(monkeypatch):
    """run_forever: the live account's bots get a pass every TICK_SECONDS while the others' pass (a burst of paper
    orders: minutes) is still going; the others' next pass starts once that one is over."""
    import asyncio
    import threading

    passes, release = [], threading.Event()

    def tick(now, live=None):
        passes.append(live)
        n = passes.count(False)
        if live is False and n == 1:
            release.wait(2)
        if live is False and n == 2:
            raise RuntimeError("Yahoo down")  # a failed pass is written down; the loop goes on

    monkeypatch.setattr(scheduler, "tick", tick)
    monkeypatch.setattr(scheduler, "settings", scheduler.settings.__class__(tick_seconds=0))
    monkeypatch.setattr(scheduler, "state", dict(scheduler.state))

    async def run():
        loop = asyncio.create_task(scheduler.run_forever())
        while passes.count(True) < 5:
            await asyncio.sleep(0.01)
        during = passes.count(False)
        release.set()
        while passes.count(False) < 3:
            await asyncio.sleep(0.01)
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
        return during

    assert asyncio.run(run()) == 1  # five live passes while the others' first one was still going
    assert passes[:2] == [True, False]
    assert scheduler.state["running"] is False and scheduler.state["last_error"] == "RuntimeError: Yahoo down"
