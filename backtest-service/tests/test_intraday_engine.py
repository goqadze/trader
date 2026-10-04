"""The intraday engine: how an order plan fills, exits and is sized on 5-minute bars."""

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.engines import IntradayEngine
from app.models import RunConfig

NY = ZoneInfo("America/New_York")
D1, D2 = date(2025, 3, 4), date(2025, 3, 5)


def _day(day, rows, start="09:30"):
    t0 = datetime.combine(day, datetime.strptime(start, "%H:%M").time(), tzinfo=NY)
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=[t0 + timedelta(minutes=5 * i) for i in range(len(rows))])


def _flat(day, price=100.0, n=78):
    return _day(day, [(price, price + 0.05, price - 0.05, price)] * n)


def _at(day, hhmm):
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time(), tzinfo=NY).isoformat()


def _plan(day=D1, side="long", order="limit", entry=100.0, stop=99.0, target=102.0, target_r=None,
          placed="10:00", valid="11:00"):
    return {"date": day.isoformat(), "side": side, "order": order, "entry": entry, "stop": stop, "target": target,
            "target_r": target_r, "placed_at": _at(day, placed), "valid_until": _at(day, valid),
            "exit_by": _at(day, "15:55"), "note": "test setup"}


def _run(bars, plans, **cfg):
    events = []

    async def emit(ev):
        events.append(ev)

    c = RunConfig(**{"symbol": "QQQ", "start": D1, "end": D2, "strategy": "ict_sweep_fvg", "slippage_pct": 0.0, **cfg})
    return asyncio.run(IntradayEngine().run(c, bars, plans, emit)), events


def _with(day, at_bar: int, rows):
    """A flat day at 100 with `rows` replacing bars from index `at_bar` (bar 6 starts at 10:00)."""
    bars = _flat(day)
    for j, row in enumerate(rows):
        bars.iloc[at_bar + j] = row
    return bars


def _two_days(d1_bars):
    return pd.concat([d1_bars, _flat(D2)])


def test_a_limit_fills_where_touched_and_exits_at_the_target():
    bars = _with(D1, 6, [(100.5, 100.6, 99.9, 100.2), (100.2, 102.5, 100.1, 102.3)])  # 10:00 dips to the limit; 10:05 hits 102
    r, events = _run(_two_days(bars), [_plan()])
    entry, exit_ = r["trades"]
    assert (entry["side"], entry["price"], entry["date"]) == ("BUY", 100.0, "2025-03-04 10:00")
    assert entry["shares"] == 100  # 1% of $10,000 = $100 at risk, $1 a share between the entry and the stop
    assert (exit_["reason"], exit_["price"], exit_["pnl"]) == ("target", 102.0, 200.0)
    assert r["num_trades"] == 1 and r["total_return_pct"] == 2.0 and r["verdict"]["grade"] == "no"  # one trade proves nothing
    assert [e["type"] for e in events].count("trade") == 2
    logged = [d for e in events if e["type"] == "step" for d in e["decisions"]]
    assert len(logged) == 1 and logged[0]["action"] == "BUY" and "Filled 100.00 at 10:00" in logged[0]["reasoning"]


def test_a_gap_through_the_limit_fills_at_the_better_open():
    bars = _with(D1, 6, [(99.6, 99.8, 99.5, 99.7)])
    r, _ = _run(_two_days(bars), [_plan()])
    assert r["trades"][0]["price"] == 99.6


def test_the_fill_bar_counts_only_the_stop():
    bars = _with(D1, 6, [(100.5, 102.5, 98.9, 100.0)])  # fills, touches both the target and the stop in one bar
    r, _ = _run(_two_days(bars), [_plan()])
    assert r["trades"][1]["reason"] == "stop-loss" and r["trades"][1]["pnl"] == -100.0


def test_a_bar_touching_both_later_is_a_stop():
    bars = _with(D1, 6, [(100.5, 100.6, 99.9, 100.2), (100.2, 102.5, 98.5, 100.0)])
    r, _ = _run(_two_days(bars), [_plan()])
    assert r["trades"][1]["reason"] == "stop-loss" and r["trades"][1]["price"] == 99.0


def test_unfilled_and_runaway_orders_are_cancelled():
    never = _flat(D1, price=101.0)  # never comes back to 100
    r, events = _run(_two_days(never), [_plan()])
    assert r["num_trades"] == 0 and r["unfilled"] == 1
    assert "not filled by 11:00" in next(e["reasoning"] for e in events if e["type"] == "step")
    runaway = _with(D1, 6, [(100.5, 102.2, 100.4, 102.0), (102.0, 102.1, 99.5, 99.8)])  # target first, then the entry
    r, events = _run(_two_days(runaway), [_plan()])
    assert r["num_trades"] == 0 and "reached the target before the entry" in next(e["reasoning"] for e in events if e["type"] == "step")


def test_a_market_short_with_a_risk_multiple_target_is_closed_at_1555():
    bars = _flat(D1)  # flat all day: neither the stop (101) nor 10R lower is reached
    plan = _plan(side="short", order="market", entry=None, stop=101.0, target=None, target_r=10.0, placed="09:35", valid="09:40")
    r, _ = _run(_two_days(bars), [plan], slippage_pct=0.001)
    entry, exit_ = r["trades"]
    assert (entry["side"], entry["date"]) == ("SELL", "2025-03-04 09:35")
    assert entry["price"] == pytest.approx(99.9)  # a short sale fills a touch lower
    assert (exit_["side"], exit_["reason"], exit_["date"]) == ("BUY", "close", "2025-03-04 15:55")
    assert exit_["price"] == pytest.approx(100.1)  # buying back fills a touch higher
    assert exit_["pnl"] < 0 and exit_["hold_minutes"] == 380


def test_a_short_wins_when_the_price_falls_to_its_target():
    bars = _with(D1, 6, [(99.5, 100.1, 99.4, 99.8), (99.8, 99.9, 97.5, 97.8)])
    r, _ = _run(_two_days(bars), [_plan(side="short", entry=100.0, stop=101.0, target=98.0)])
    assert r["trades"][1]["reason"] == "target" and r["trades"][1]["pnl"] == 200.0


def test_a_tight_stop_is_capped_by_the_cash():
    bars = _with(D1, 6, [(100.5, 100.6, 99.9, 100.2)])
    r, _ = _run(_two_days(bars), [_plan(stop=99.99, target=100.5)])
    assert r["trades"][0]["shares"] == 100  # $100 / $0.01 would be 10,000 shares; $10,000 buys 100


def test_the_breaker_stops_new_trades():
    # Fills at 100, then the next bar opens at 70, through the 80 stop: 25 shares lose $30 each, 7.5% of the account
    crash = _with(D1, 6, [(100.5, 100.6, 99.9, 100.2), (70.0, 70.5, 69.5, 70.0)])
    bars = pd.concat([crash, _with(D2, 6, [(100.5, 100.6, 99.9, 100.2), (100.2, 102.5, 100.1, 102.3)])])
    plans = [_plan(stop=80.0, target=150.0), _plan(day=D2)]
    r, events = _run(bars, plans, risk_pct=0.05, max_drawdown_pct=0.05)
    assert r["trades"][1]["price"] == 70.0 and r["trades"][1]["pnl"] == -750.0
    assert r["num_trades"] == 1 and r["breaker_tripped_on"] == "2025-03-04"
    assert "Paused" in [e for e in events if e["type"] == "step"][1]["reasoning"]
