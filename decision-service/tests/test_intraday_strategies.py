"""Intraday strategies on 5-minute bars: the exact setups they take, the ones they skip, and no look-ahead."""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import intraday_strategies as ist
from app.intraday_strategies import Context, htf_closes, ict, ict_amd, orb, plans, trend_at
from app.tools import MARKET_TZ

DAY = date(2025, 3, 4)


def _bars(rows, day=DAY, start="09:30"):
    """5-minute bars from (open, high, low, close) rows, the first starting at `start` New York time."""
    t0 = datetime.combine(day, datetime.strptime(start, "%H:%M").time(), tzinfo=MARKET_TZ)
    idx = [t0 + timedelta(minutes=5 * i) for i in range(len(rows))]
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=pd.DatetimeIndex(idx)).assign(Volume=1000.0)


def _mirror_rows(rows, around=200.0):
    """The same day reflected around a price: rises become falls (high and low swap)."""
    return [(around - o, around - l, around - h, around - c) for o, h, l, c in rows]


# A textbook long: drift down, sweep yesterday's low (100) at 9:40 and close back above it, then a strong candle
# closes above the swing high (101.5) and leaves a fair value gap 100.9-101.3 in the discount half of the move.
ICT_LONG = [
    (101.0, 101.5, 100.6, 100.8),  # 9:30
    (100.8, 101.0, 100.4, 100.5),  # 9:35
    (100.5, 100.6, 99.7, 100.2),   # 9:40 sweep: low 99.7 < 100, close back above
    (100.2, 100.9, 100.1, 100.8),  # 9:45 candle 1 of the gap (high 100.9)
    (100.8, 102.2, 100.8, 102.0),  # 9:50 displacement: closes above 101.5
    (102.0, 103.0, 101.3, 102.2),  # 9:55 candle 3 (low 101.3): setup complete at 10:00
    (102.2, 102.3, 101.0, 101.2),  # 10:00 back into the gap
    (101.2, 104.0, 101.1, 103.8),  # 10:05
]
CTX = Context(prev_high=105.0, prev_low=100.0)


def test_orb_follows_the_first_candle_with_its_far_end_as_the_stop():
    up = orb(DAY, _bars([(100, 101, 99.5, 100.8), (100.8, 101.2, 100.5, 101)]), CTX)
    assert (up.side, up.order, up.stop, up.target_r) == ("long", "market", 99.5, 10.0)
    assert up.placed_at.startswith("2025-03-04T09:35") and up.exit_by.startswith("2025-03-04T15:55")
    down = orb(DAY, _bars([(100, 100.5, 99, 99.2)]), CTX)
    assert (down.side, down.stop) == ("short", 100.5)
    assert orb(DAY, _bars([(100, 100.5, 99, 99.2)]), CTX, sides="long") is None
    assert orb(DAY, _bars([(100, 100.5, 99.5, 100)]), CTX) is None  # a doji: no direction
    assert orb(DAY, _bars([(100, 101, 99.5, 100.8)], start="09:35"), CTX) is None  # no 9:30 bar


def test_ict_takes_the_textbook_long():
    p = ict(DAY, _bars(ICT_LONG), CTX)
    assert (p.side, p.order) == ("long", "limit")
    assert p.entry == 101.1  # the middle of the 100.9-101.3 gap
    assert p.stop == 99.68  # under the sweep's 99.7, by 0.02%
    assert p.target == 105.0  # today's high (103) pays < 2R; yesterday's high is the nearest that pays 2x
    assert p.placed_at.startswith("2025-03-04T10:00") and p.valid_until.startswith("2025-03-04T11:00")
    assert "Swept yesterday's low 100.00 at 09:40" in p.note and "(2.7R)" in p.note


def test_ict_never_looks_ahead():
    """The plan is the same whether the day's later bars exist or not."""
    full = ict(DAY, _bars(ICT_LONG), CTX)
    upto = ict(DAY, _bars(ICT_LONG[:6]), CTX)
    assert full == upto
    assert ict(DAY, _bars(ICT_LONG[:5]), CTX) is None  # one bar earlier the gap doesn't exist yet


def test_ict_mirrors_into_the_textbook_short():
    p = ict(DAY, _bars(_mirror_rows(ICT_LONG)), Context(prev_high=100.0, prev_low=95.0))
    assert (p.side, p.entry, p.stop, p.target) == ("short", 98.9, 100.32, 95.0)
    assert "Swept yesterday's high 100.00" in p.note and "yesterday's low 95.00" in p.note
    assert ict(DAY, _bars(_mirror_rows(ICT_LONG)), Context(prev_high=100.0, prev_low=95.0), sides="long") is None


def test_ict_skips_setups_whose_target_pays_under_2r():
    assert ict(DAY, _bars(ICT_LONG), Context(prev_high=102.5, prev_low=100.0)) is None


def test_ict_skips_a_gap_in_premium():
    premium = ICT_LONG[:5] + [(102.0, 102.4, 101.3, 102.2)]  # the move tops at 102.4: the gap's middle is above 50%
    assert ict(DAY, _bars(premium), CTX) is None


def test_ict_setups_after_the_morning_window_are_ignored():
    late = _bars(ICT_LONG, start="10:35")  # the same pattern completing at 11:05
    assert ict(DAY, late, CTX) is None


def test_plans_cover_the_range_and_give_each_day_the_previous_session():
    prev = _bars([(100.5, 105.0, 100.0, 101.0)] * 3, day=date(2025, 3, 3))  # yesterday: high 105, low 100
    bars = pd.concat([prev, _bars(ICT_LONG)])
    out = plans(bars, "ict_sweep_fvg", date(2025, 3, 3), date(2025, 3, 4))
    assert [p["date"] for p in out] == ["2025-03-04"]  # the first day has no previous session here
    assert out[0]["entry"] == 101.1


def test_the_endpoint_serves_plans_and_refuses_unknown_strategies(monkeypatch):
    from app import main

    prev = _bars([(100.5, 105.0, 100.0, 101.0)] * 3, day=date(2025, 3, 3))
    asked = []

    def bars_between(symbol, start, end, timeframe="30Min", extended=False):
        asked.append((symbol, start, timeframe, extended))
        return pd.concat([prev, _bars(ICT_LONG)])

    monkeypatch.setattr(main, "bars_between", bars_between)
    client = TestClient(main.app)
    r = client.get("/intraday/plans", params={"symbol": "qqq", "start": "2025-03-04", "end": "2025-03-04", "strategy": "ict_sweep_fvg"})
    assert r.status_code == 200 and r.json()[0]["side"] == "long"
    assert asked == [("QQQ", date(2025, 2, 22), "5Min", True)]  # a week and a half earlier, with the pre-market
    r = client.get("/intraday/plans", params={"symbol": "MNQ", "start": "2025-03-04", "end": "2025-03-04", "strategy": "ict_sweep_fvg"})
    assert r.json()[0]["entry"] == 101.0  # a future's prices on its 0.25 tick (101.10 rounds to 101.00)
    assert client.get("/intraday/plans", params={"symbol": "QQQ", "start": "2025-03-04", "end": "2025-03-04", "strategy": "nope"}).status_code == 422


@pytest.mark.parametrize("strategy", list(ist.INTRADAY_STRATEGIES))
def test_every_plan_is_placed_before_it_may_fill_and_closes_by_1555(strategy):
    prev = _bars([(100.5, 105.0, 100.0, 101.0)] * 3, day=date(2025, 3, 3))
    for p in plans(pd.concat([prev, _bars(ICT_LONG)]), strategy, date(2025, 3, 4), date(2025, 3, 4)):
        assert p["placed_at"] < p["valid_until"] <= p["exit_by"]


# --- ICT Power of 3 --------------------------------------------------------------------------------------------
# The same morning as ICT_LONG, read as the three phases: the pre-market ranged 100.00-104.50 (accumulation), the
# day opened at 101.00, in the lower half of yesterday's 98-106 range, swept the pre-market low at 9:40
# (manipulation), then shifted up at 9:50; the move's first gap is 100.6-100.8 (9:40's high to 9:50's low), known
# when the 9:50 bar closes (distribution).
AMD_CTX = Context(prev_high=106.0, prev_low=98.0, pre_high=104.5, pre_low=100.0)


def test_amd_takes_the_textbook_long_and_targets_the_other_side_of_the_accumulation():
    p = ict_amd(DAY, _bars(ICT_LONG), AMD_CTX)
    assert (p.side, p.order, p.entry, p.stop, p.target) == ("long", "limit", 100.7, 99.68, 104.5)
    assert p.placed_at.startswith("2025-03-04T09:55") and p.exit_by.startswith("2025-03-04T15:55")
    assert "Accumulation: pre-market 100.00-104.50" in p.note and "discount half" in p.note
    assert "Manipulation: swept the pre-market low at 09:40" in p.note and "the pre-market high 104.50 (3.7R)" in p.note


def test_amd_never_looks_ahead():
    assert ict_amd(DAY, _bars(ICT_LONG), AMD_CTX) == ict_amd(DAY, _bars(ICT_LONG[:5]), AMD_CTX)
    assert ict_amd(DAY, _bars(ICT_LONG[:4]), AMD_CTX) is None  # before the 9:50 bar there is no shift yet


def test_amd_only_buys_in_discount_and_needs_an_accumulation_range():
    premium = Context(prev_high=101.0, prev_low=99.0, pre_high=104.5, pre_low=100.0)  # opened at 101: the upper half
    assert ict_amd(DAY, _bars(ICT_LONG), premium, sides="long") is None
    assert ict_amd(DAY, _bars(ICT_LONG), Context(prev_high=106.0, prev_low=98.0)) is None  # no pre-market range


def test_amd_manipulation_must_come_in_the_first_hour():
    late = _bars(ICT_LONG, start="10:25")  # the same pattern, the sweep at 10:35, completing at 10:55
    assert ict_amd(DAY, late, AMD_CTX) is None


def test_amd_mirrors_into_the_textbook_short():
    ctx = Context(prev_high=102.0, prev_low=94.0, pre_high=100.0, pre_low=95.5)  # AMD_CTX reflected around 100
    p = ict_amd(DAY, _bars(_mirror_rows(ICT_LONG)), ctx)
    assert (p.side, p.entry, p.stop, p.target) == ("short", 99.3, 100.32, 95.5)
    assert "premium half" in p.note and "swept the pre-market high" in p.note and "the pre-market low 95.50" in p.note


def test_amd_plans_read_the_pre_market_from_the_bars():
    prev = _bars([(100.5, 105.0, 100.0, 101.0)] * 3, day=date(2025, 3, 3))  # yesterday: 100-105, the middle 102.50
    pre = _bars([(102.0, 104.5, 100.0, 102.0)] * 12, start="04:00")  # an hour of pre-market at 100.00-104.50
    out = plans(pd.concat([prev, pre, _bars(ICT_LONG)]), "ict_amd", DAY, DAY)
    assert [(p["side"], p["target"]) for p in out] == [("long", 104.5)]
    thin = _bars([(102.0, 104.5, 100.0, 102.0)] * 5, start="04:00")  # 25 minutes: too little to call a range
    assert plans(pd.concat([prev, thin, _bars(ICT_LONG)]), "ict_amd", DAY, DAY) == []
    assert plans(pd.concat([prev, pre, _bars(ICT_LONG)]), "ict_sweep_fvg", DAY, DAY)[0]["entry"] == 101.1  # the others ignore it


def test_market_entry_takes_the_same_setup_in_at_the_next_bars_open():
    limit = ict(DAY, _bars(ICT_LONG), CTX)
    market = ict(DAY, _bars(ICT_LONG), CTX, entry="market")
    assert (market.order, market.entry, market.stop, market.target) == ("market", None, limit.stop, limit.target)
    assert market.placed_at == limit.placed_at and market.valid_until.startswith("2025-03-04T10:05")  # one bar to fill
    assert "buy at market at 10:00 (the gap's middle 101.10 would pay 2.7R)" in market.note
    amd = ict_amd(DAY, _bars(ICT_LONG), AMD_CTX, entry="market")
    assert (amd.order, amd.stop, amd.target) == ("market", 99.68, 104.5)
    prev = _bars([(100.5, 105.0, 100.0, 101.0)] * 3, day=date(2025, 3, 3))
    assert plans(pd.concat([prev, _bars(ICT_LONG)]), "ict_sweep_fvg", DAY, DAY, entry="market")[0]["order"] == "market"
    assert orb(DAY, _bars(ICT_LONG), CTX, entry="limit").order == "market"  # ORB has only one way in


# --- Higher-timeframe trend filter -------------------------------------------------------------------------------

def _session(day, price):
    """A full regular session of 78 flat 5-minute bars closing at `price`."""
    return _bars([(price, price + 0.05, price - 0.05, price)] * 78, day=day)


def test_higher_timeframe_bars_are_built_from_the_session_and_end_on_time():
    day = _session(DAY, 100.0)
    ends, closes = htf_closes(day, "1h")
    assert [t.strftime("%H:%M") for t in ends] == ["10:30", "11:30", "12:30", "13:30", "14:30", "15:30", "16:00"]
    ends, _ = htf_closes(day, "4h")
    assert [t.strftime("%H:%M") for t in ends] == ["13:30", "16:00"]
    ends, closes = htf_closes(pd.concat([_session(date(2025, 3, 3), 99.0), day]), "1d")
    assert [t.strftime("%m-%d %H:%M") for t in ends] == ["03-03 16:00", "03-04 16:00"] and closes == [99.0, 100.0]


def test_the_trend_only_sees_finished_bars():
    days = pd.bdate_range("2025-02-03", periods=20)
    ends, closes = htf_closes(pd.concat([_session(d.date(), 100.0 + i) for i, d in enumerate(days)]), "1d")
    trend = trend_at(ends, closes)
    last = days[-1].date()
    assert trend(datetime(last.year, last.month, last.day, 15, 0, tzinfo=MARKET_TZ)) == 0  # only 19 days had finished...
    assert trend(datetime(last.year, last.month, last.day, 16, 0, tzinfo=MARKET_TZ)) == 1  # ...the 20th closed, the highest
    falling = trend_at(ends, closes[::-1])
    assert falling(datetime(last.year, last.month, last.day, 16, 0, tzinfo=MARKET_TZ)) == -1


def test_the_trend_filter_keeps_setups_with_it_and_drops_those_against_it():
    with_it = Context(prev_high=105.0, prev_low=100.0, trend=lambda t: 1, trend_name="1-hour")
    p = ict(DAY, _bars(ICT_LONG), with_it)
    assert p.side == "long" and p.note.endswith("With the 1-hour trend (up)")
    assert ict(DAY, _bars(ICT_LONG), Context(prev_high=105.0, prev_low=100.0, trend=lambda t: -1)) is None
    assert ict(DAY, _bars(ICT_LONG), Context(prev_high=105.0, prev_low=100.0, trend=lambda t: 0)) is None  # unclear: no trade
    short = ict(DAY, _bars(_mirror_rows(ICT_LONG)), Context(prev_high=100.0, prev_low=95.0, trend=lambda t: -1, trend_name="daily"))
    assert short.side == "short" and short.note.endswith("With the daily trend (down)")


def test_plans_with_a_daily_trend_filter():
    history = pd.bdate_range("2025-01-31", "2025-02-28")  # ~20 sessions before March 3
    yesterday = _bars([(100.5, 105.0, 100.0, 101.0)] * 3, day=date(2025, 3, 3))
    rising = pd.concat([_session(d.date(), 90.0 + i * 0.5) for i, d in enumerate(history)] + [yesterday, _bars(ICT_LONG)])
    assert [p["side"] for p in plans(rising, "ict_sweep_fvg", DAY, DAY, htf="1d")] == ["long"]
    falling = pd.concat([_session(d.date(), 115.0 - i * 0.5) for i, d in enumerate(history)] + [yesterday, _bars(ICT_LONG)])
    assert plans(falling, "ict_sweep_fvg", DAY, DAY, htf="1d") == []
    assert len(plans(falling, "ict_sweep_fvg", DAY, DAY)) == 1  # without the filter the setup stands
