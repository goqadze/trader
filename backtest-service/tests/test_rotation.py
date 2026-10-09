"""Momentum rotation: what it holds, when it rotates, the benchmark, and that it can't look ahead."""

import asyncio
from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.engines import RotationEngine
from app.engines.rotation import momentum
from app.models import RotationConfig


def _closes(**yearly):
    """Business-day closes from 2022-01-03 to 2025-06-30 (the window starts in 2024: 12-1 momentum needs 13 months
    before it), each symbol growing at its yearly rate (or following a callable of the day index)."""
    idx = pd.bdate_range("2022-01-03", "2025-06-30")
    t = np.arange(len(idx)) / 252
    cols = {s: (r(np.arange(len(idx))) if callable(r) else 100 * (1 + r) ** t) for s, r in yearly.items()}
    return pd.DataFrame(cols, index=[d.date() for d in idx])


def _run(closes, **kw):
    cfg = RotationConfig(**({"symbols": list(closes.columns), "start": date(2024, 1, 2), "end": date(2025, 6, 30),
                             "slippage_pct": 0.0} | kw))
    events = []

    async def emit(ev):
        events.append(ev)

    return asyncio.run(RotationEngine().run(cfg, closes, emit)), events


def test_it_holds_the_strongest_and_never_a_falling_one():
    r, _ = _run(_closes(A=0.6, B=0.1, C=-0.2), top_n=1)
    assert {tuple(x["held"]) for x in r["rebalances"]} == {("A",)}
    r, _ = _run(_closes(A=0.6, B=0.1, C=-0.2), top_n=3)
    assert {tuple(x["held"]) for x in r["rebalances"]} == {("A", "B")}  # C fell: its slot stays in cash
    assert r["rebalances"][0]["ranking"][0] == {"symbol": "A", "momentum_pct": pytest.approx(62.4, abs=0.5)}  # 12 calendar months ≈ 262 business days at 60% a year


def test_the_result_serializes_to_json():
    import json

    r, _ = _run(_closes(A=0.6, B=0.1, C=-0.2), top_n=2)
    json.dumps(r)  # numpy values (a numpy bool for beat_buy_hold) made the API answer 500


def test_everything_falling_means_cash():
    r, _ = _run(_closes(A=-0.1, B=-0.2), top_n=1)
    assert r["total_return_pct"] == 0.0 and r["num_trades"] == 0
    assert r["rebalances"][0]["held"] == []
    r2, _ = _run(_closes(A=-0.1, B=-0.2), top_n=1, abs_filter=False)
    assert r2["rebalances"][0]["held"] == ["A"]  # without the filter it holds the least bad


def test_it_rotates_out_of_a_leader_that_turns_and_counts_the_round_trip():
    turn = len(pd.bdate_range("2022-01-03", "2024-04-01"))  # A peaks in April 2024, then falls; B keeps rising
    a = lambda i: np.where(i < turn, 100 * 1.003 ** i, 100 * 1.003 ** turn * 0.997 ** (i - turn))  # noqa: E731
    r, events = _run(_closes(A=a, B=0.3), top_n=1)
    helds = [x["held"] for x in r["rebalances"]]
    assert helds[0] == ["A"] and helds[-1] == ["B"]
    exits = [t for t in r["trades"] if "pnl" in t]
    assert exits and exits[0]["symbol"] == "A" and exits[0]["reason"] == "rotated out"
    assert r["num_trades"] == len(exits)
    logged = [d["reasoning"] for e in events if e["type"] == "step" for d in e["decisions"]]
    assert any("sold A" in text and "bought B" in text for text in logged)


def test_the_benchmark_holds_the_whole_universe_in_equal_parts():
    r, _ = _run(_closes(A=0.6, B=0.0), top_n=1)
    closes = _closes(A=0.6, B=0.0)  # $5,000 each on the first day, held to the end
    first, last = closes.loc[date(2024, 1, 2)], closes.iloc[-1]
    expected = (0.5 * last["A"] / first["A"] + 0.5 * last["B"] / first["B"] - 1) * 100
    assert r["buy_hold_return_pct"] == pytest.approx(expected, abs=0.01)
    assert r["benchmark_symbols"] == ["A", "B"]


def test_slippage_costs_something():
    free, _ = _run(_closes(A=0.6, B=0.5, C=0.4), top_n=2)
    costly, _ = _run(_closes(A=0.6, B=0.5, C=0.4), top_n=2, slippage_pct=0.001)
    assert costly["total_return_pct"] < free["total_return_pct"]


def test_momentum_only_sees_the_past():
    closes = _closes(A=0.6, B=0.1).ffill()
    closes.index = pd.DatetimeIndex(closes.index)
    day = date(2024, 6, 28)
    before = momentum(closes, day, 12, 1)
    changed = closes.copy()
    changed.loc[pd.Timestamp("2024-06-01"):, "B"] *= 10  # everything after the skipped month's start
    assert momentum(changed, day, 12, 1).equals(before)  # the latest month is skipped: no effect


def test_settings_are_checked():
    with pytest.raises(ValidationError, match="top_n"):
        RotationConfig(symbols=["A", "B"], start=date(2024, 1, 1), end=date(2025, 1, 1), top_n=3)
    with pytest.raises(ValidationError, match="ticker"):
        RotationConfig(symbols=["A", "not one"], start=date(2024, 1, 1), end=date(2025, 1, 1))


def test_whole_shares_leave_a_slot_under_one_share_in_cash_and_fractions_buy_it():
    closes = _closes(A=0.6, B=0.5, C=0.4)  # about $256 and $225 a share when the window starts
    whole, events = _run(closes, top_n=2, initial_cash=300, fractional=False)  # $150 slots
    assert whole["num_trades"] == 0 and whole["total_return_pct"] == 0.0
    assert all(r["held"] == [] for r in whole["rebalances"])
    logged = [d["reasoning"] for e in events if e["type"] == "step" for d in e["decisions"]]
    assert "less than one share of A, B: that money stays in cash" in logged[0]
    split, _ = _run(closes, top_n=2, initial_cash=300, fractional=True)
    assert split["rebalances"][0]["held"] == ["A", "B"] and split["total_return_pct"] > 30
    assert split["total_return_pct"] == pytest.approx(_run(closes, top_n=2, fractional=True)[0]["total_return_pct"], abs=0.1)


def test_whole_shares_buy_whole_numbers_and_trim_only_worthwhile_changes():
    r, _ = _run(_closes(A=0.6, B=0.5, C=0.4), top_n=2, fractional=False)
    buys = [t for t in r["trades"] if t["side"] == "BUY"]
    assert buys and all(t["shares"] == int(t["shares"]) for t in buys)
    # Month after month A and B stay the picks: a trim or top-up happens only when it's worth 2% of a slot ($100)
    small = [t for t in r["trades"] if t["shares"] * t["price"] < 0.02 * 5_000 and "pnl" not in t]
    assert small == []


def test_fractional_is_the_default():
    assert RotationConfig(symbols=["A", "B"], start=date(2024, 1, 1), end=date(2025, 1, 1), top_n=1).fractional is True
