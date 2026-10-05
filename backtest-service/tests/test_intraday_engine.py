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


# --- Index futures: whole contracts, points x multiplier, ticks and fees per contract ---------------------------

def test_a_future_trades_whole_contracts_capped_by_the_margin():
    # MNQ at 20,000: a 10-point stop risks $20 a contract, so 1% of $10,000 would be 5 contracts; the margin (10% of
    # $40,000 a contract) allows only 2
    bars = _flat(D1, price=20_000.0)
    for j, row in enumerate([(20_005.0, 20_006.0, 19_999.0, 20_002.0), (20_002.0, 20_025.0, 20_001.0, 20_022.0)]):
        bars.iloc[6 + j] = row
    plan = _plan(entry=20_000.0, stop=19_990.0, target=20_020.0)
    r, _ = _run(pd.concat([bars, _flat(D2, price=20_000.0)]), [plan], symbol="MNQ")
    entry, exit_ = r["trades"]
    assert entry["shares"] == 2 and exit_["reason"] == "target"
    assert exit_["pnl"] == pytest.approx(20 * 2 * 2 - 4 * 0.62)  # 20 points x $2 x 2 contracts, $0.62 a contract a side
    assert exit_["fee"] == pytest.approx(1.24) and r["contract"]["multiplier"] == 2 and r["contract"]["etf"] == "QQQ"


def test_a_futures_market_order_slips_a_tick_not_a_percentage():
    plan = _plan(side="short", order="market", entry=None, stop=20_010.0, target=None, target_r=10.0, placed="09:35", valid="09:40")
    r, _ = _run(_two_days(_flat(D1, price=20_000.0)), [plan], symbol="MES")
    entry, exit_ = r["trades"]
    assert entry["price"] == 19_999.75 and exit_["price"] == 20_000.25  # one 0.25 tick worse each way
    assert entry["shares"] == 1  # 10.25 points x $5 = $51.25 at risk; the margin on $100,000 a contract allows 1


def test_futures_only_run_with_the_intraday_strategies():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="intraday strategies only"):
        RunConfig(symbol="MNQ", start=D1, end=D2, strategy="sma_rsi")


def test_a_market_entry_with_a_fixed_target_takes_profit_there():
    # The ICT market entry: in at the next bar's open after the setup, the setup's own stop and target
    bars = _with(D1, 6, [(100.3, 100.4, 100.2, 100.3), (100.3, 102.5, 100.2, 102.3)])
    plan = _plan(order="market", entry=None, stop=99.0, target=102.0, valid="10:05")
    r, _ = _run(_two_days(bars), [plan])
    entry, exit_ = r["trades"]
    assert entry["price"] == 100.3 and exit_["reason"] == "target" and exit_["price"] == 102.0
    assert entry["shares"] == 76  # 1% of $10,000 over the 1.30 between the fill and the stop


def test_the_entry_setting_reaches_decision_service(monkeypatch):
    from app import runner

    asked = {}

    class Resp:
        status_code = 200

        def json(self):
            return []

    monkeypatch.setattr(runner.httpx, "get", lambda url, timeout, params: asked.update(params) or Resp())
    runner._intraday_plans(RunConfig(symbol="MNQ", start=D1, end=D2, strategy="ict_amd", entry="market", htf="4h"))
    assert asked["entry"] == "market" and asked["strategy"] == "ict_amd" and asked["htf"] == "4h"
    assert RunConfig(symbol="QQQ", start=D1, end=D2, strategy="ict_sweep_fvg").entry == "limit"  # the default


def test_skipped_setups_say_why_and_how_big_they_were():
    flat = _two_days(_flat(D1, price=20_000.0))
    # MNQ: a 100-point stop risks $200 a contract, more than the $100 (1%) allowed
    wide = _plan(order="market", entry=None, stop=19_900.0, target=20_400.0, placed="09:35", valid="09:40")
    r, events = _run(flat, [wide], symbol="MNQ")
    assert r["num_trades"] == 0 and r["skips"] == {"too_small": 1}
    small = r["too_small"]
    assert (small["count"], small["unit"], small["by_margin"], small["allowed_risk"]) == (1, "contract", 0, 100.0)
    assert small["risk_per_unit"] == pytest.approx(200.5)  # 100.25 points (a tick of slippage) x $2
    logged = [d["reasoning"] for e in events if e["type"] == "step" for d in e["decisions"]][0]
    assert logged.startswith("Not traded: too small for the account (one contract risks $200 to the stop, more than the $100 (1.0%)")
    # NQ: $20 a point, so one contract at 20,000 ties up $40,000 of margin, more than the account
    r, _ = _run(flat, [_plan(order="market", entry=None, stop=19_995.0, target=20_100.0, placed="09:35", valid="09:40")], symbol="NQ")
    assert r["too_small"]["by_margin"] == 1 and r["too_small"]["margin_per_unit"] == pytest.approx(40_000.5, abs=1)
    r, _ = _run(_two_days(_flat(D1, price=101.0)), [_plan()])
    assert r["skips"] == {"unfilled": 1} and "too_small" not in r
