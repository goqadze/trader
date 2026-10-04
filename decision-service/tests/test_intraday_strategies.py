"""Intraday strategies on 5-minute bars: the exact setups they take, the ones they skip, and no look-ahead."""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import intraday_strategies as ist
from app.intraday_strategies import Context, ict, orb, plans
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
    monkeypatch.setattr(main, "bars_between", lambda symbol, start, end, timeframe="30Min": asked.append((symbol, start, timeframe)) or pd.concat([prev, _bars(ICT_LONG)]))
    client = TestClient(main.app)
    r = client.get("/intraday/plans", params={"symbol": "qqq", "start": "2025-03-04", "end": "2025-03-04", "strategy": "ict_sweep_fvg"})
    assert r.status_code == 200 and r.json()[0]["side"] == "long"
    assert asked == [("QQQ", date(2025, 2, 22), "5Min")]  # a week and a half earlier, for the previous session
    assert client.get("/intraday/plans", params={"symbol": "QQQ", "start": "2025-03-04", "end": "2025-03-04", "strategy": "nope"}).status_code == 422


@pytest.mark.parametrize("strategy", list(ist.INTRADAY_STRATEGIES))
def test_every_plan_is_placed_before_it_may_fill_and_closes_by_1555(strategy):
    prev = _bars([(100.5, 105.0, 100.0, 101.0)] * 3, day=date(2025, 3, 3))
    for p in plans(pd.concat([prev, _bars(ICT_LONG)]), strategy, date(2025, 3, 4), date(2025, 3, 4)):
        assert p["placed_at"] < p["valid_until"] <= p["exit_by"]
