"""Tests for SimplePortfolioEngine — the backtest simulator.

Covers the money math (fees, slippage, cost-basis PnL), the risk exits
(stop-loss / target enforced daily), the confidence filter, the buy-and-hold
benchmark, and the emitted event stream. All async runs go through asyncio.run
so no pytest plugin is required.
"""

import asyncio
import math
from datetime import date, timedelta

import pandas as pd

from app.engines.simple import SimplePortfolioEngine
from app.models import RunConfig


# --- helpers -------------------------------------------------------------

def _prices(values, start=date(2025, 1, 6)):
    idx = [start + timedelta(days=i) for i in range(len(values))]
    return pd.Series([float(v) for v in values], index=idx)


def _run(prices, decide, **overrides):
    cfg_args = dict(
        symbol="TEST", start=prices.index[0], end=prices.index[-1],
        initial_cash=10_000, min_confidence=0.6, rebalance_days=1,
        position_pct=1.0, fee_pct=0.0, slippage_pct=0.0,
    )
    cfg_args.update(overrides)
    cfg = RunConfig(**cfg_args)
    events = []

    async def emit(ev):
        events.append(ev)

    result = asyncio.run(SimplePortfolioEngine().run(cfg, prices, decide, emit))
    return result, events


def _const(signal):
    """A decide() that returns the same signal every day."""
    async def decide(symbol, as_of):
        return dict(signal)
    return decide


def _scripted(signals):
    """A decide() that returns signals in order, repeating the last one after they run out."""
    state = {"i": 0}

    async def decide(symbol, as_of):
        sig = signals[min(state["i"], len(signals) - 1)]
        state["i"] += 1
        return dict(sig)
    return decide


BUY = {"action": "BUY", "confidence": 0.9, "reasoning": "", "position": {"stop_loss": 0, "target": math.inf}}
SELL = {"action": "SELL", "confidence": 0.9, "reasoning": ""}


# --- trading + accounting ------------------------------------------------

def test_buy_then_hold_tracks_price():
    res, _ = _run(_prices([100, 101, 102]), _const(BUY), rebalance_days=100)
    # 100 shares bought at 100 (no slippage), held; final equity = 100 * 102
    assert res["final_equity"] == 10_200.0
    assert res["total_return_pct"] == 2.0


def test_min_confidence_filters_every_trade():
    """The exact scenario a user hit: min_confidence above the signal's confidence -> no trades."""
    weak_buy = {"action": "BUY", "confidence": 0.6, "reasoning": ""}
    res, _ = _run(_prices([100, 101, 102, 103]), _const(weak_buy), min_confidence=0.65)
    assert res["num_trades"] == 0
    assert res["final_equity"] == 10_000.0
    assert res["total_return_pct"] == 0.0
    assert all(pt["equity"] == 10_000.0 for pt in res["equity_curve"])  # flat all the way


def test_slippage_makes_buys_fill_higher():
    _, ev = _run(_prices([100, 100, 100]), _const(BUY), slippage_pct=0.01, rebalance_days=100)
    buys = [e for e in ev if e.get("type") == "trade" and e["side"] == "BUY"]
    assert buys[0]["price"] == 101.0  # 100 * (1 + 0.01)


def test_fees_reduce_final_equity():
    prices = _prices([100, 110])
    no_fee, _ = _run(prices, _scripted([BUY, SELL]), fee_pct=0.0)
    with_fee, _ = _run(prices, _scripted([BUY, SELL]), fee_pct=0.01)
    assert with_fee["final_equity"] < no_fee["final_equity"]
    sells = [t for t in with_fee["trades"] if t["side"] == "SELL"]
    assert sells[0]["fee"] > 0


def test_round_trip_pnl_is_net_of_costs():
    # Buy at 100, sell at 110, no costs: 100 shares * $10 = $1000 profit
    res, _ = _run(_prices([100, 110]), _scripted([BUY, SELL]))
    sells = [t for t in res["trades"] if t["side"] == "SELL"]
    assert sells[0]["pnl"] == 1000.0
    assert res["final_equity"] == 11_000.0


# --- risk exits ----------------------------------------------------------

def test_stop_loss_exit_fires_daily():
    buy = {"action": "BUY", "confidence": 0.9, "position": {"stop_loss": 96.0, "target": 108.0}}
    prices = _prices([100, 101, 102, 103, 99, 96, 95, 94])  # crashes through the 96 stop
    res, _ = _run(prices, _const(buy), rebalance_days=100)
    sells = [t for t in res["trades"] if t["side"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["reason"] == "stop-loss"


def test_target_exit_fires_daily():
    buy = {"action": "BUY", "confidence": 0.9, "position": {"stop_loss": 90.0, "target": 108.0}}
    prices = _prices([100, 102, 105, 109, 110])  # climbs through the 108 target
    res, _ = _run(prices, _const(buy), rebalance_days=100)
    sells = [t for t in res["trades"] if t["side"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["reason"] == "target"


def test_signal_sell_is_labelled():
    prices = _prices([100, 101, 102])
    res, _ = _run(prices, _scripted([BUY, SELL]))
    sells = [t for t in res["trades"] if t["side"] == "SELL"]
    assert sells[0]["reason"] == "signal"


# --- benchmark -----------------------------------------------------------

def test_stop_loss_can_beat_buy_and_hold():
    """Cutting the loss early beats riding the stock down -> positive alpha."""
    buy = {"action": "BUY", "confidence": 0.9, "position": {"stop_loss": 96.0, "target": 108.0}}
    prices = _prices([100, 101, 102, 103, 99, 96, 95, 94])
    res, _ = _run(prices, _const(buy), rebalance_days=100)
    assert res["total_return_pct"] == -4.0   # exited at the 96 stop
    assert res["buy_hold_return_pct"] == -6.0  # would have ridden to 94
    assert res["alpha_vs_buy_hold_pct"] == 2.0
    assert res["beat_buy_hold"] is True


def test_selling_early_lags_buy_and_hold():
    prices = _prices([100, 101, 120, 130])  # big rally after we exit
    res, _ = _run(prices, _scripted([BUY, SELL]))
    assert res["beat_buy_hold"] is False
    assert res["alpha_vs_buy_hold_pct"] < 0


# --- event stream --------------------------------------------------------

def test_emits_start_steps_and_done():
    prices = _prices([100, 101, 102])
    _, ev = _run(prices, _const(BUY), rebalance_days=100)
    assert ev[0]["type"] == "start"
    assert ev[-1]["type"] == "done"
    steps = [e for e in ev if e["type"] == "step"]
    assert len(steps) == len(prices)  # one step per trading day


def test_sentiment_present_on_decision_days_only():
    signal = {"action": "HOLD", "confidence": 0.4, "sentiment": "bullish", "reasoning": "r"}
    prices = _prices([100, 101, 102])
    _, ev = _run(prices, _const(signal), rebalance_days=2)  # decide on day 0 and 2, carry day 1
    steps = [e for e in ev if e["type"] == "step"]
    assert steps[0]["sentiment"] == "bullish"  # decision day
    assert steps[1]["sentiment"] == ""          # carry day
    assert steps[2]["sentiment"] == "bullish"  # decision day


def test_equity_curve_has_a_point_per_day_with_expected_keys():
    prices = _prices([100, 101, 102, 103])
    res, _ = _run(prices, _const(BUY), rebalance_days=100)
    assert len(res["equity_curve"]) == len(prices)
    assert set(res["equity_curve"][0]) == {"date", "equity", "price", "position"}


def test_rebalance_days_limits_decision_calls():
    calls = {"n": 0}

    async def counting_decide(symbol, as_of):
        calls["n"] += 1
        return dict(BUY)

    _run(_prices(list(range(100, 110))), counting_decide, rebalance_days=5)
    # 10 days, decide every 5 -> days 0 and 5 -> 2 calls
    assert calls["n"] == 2


# --- comparison metrics ----------------------------------------------------

def test_trade_stats_for_one_win_and_one_loss():
    # +10% round trip (100 -> 110), then -10% (100 -> 90); each held one day
    res, _ = _run(_prices([100, 110, 100, 90]), _scripted([BUY, SELL, BUY, SELL]))
    sells = [t for t in res["trades"] if t["side"] == "SELL"]
    assert [t["pnl_pct"] for t in sells] == [10.0, -10.0]
    assert [t["hold_days"] for t in sells] == [1, 1]
    assert res["win_rate_pct"] == 50.0
    assert res["profit_factor"] == round(1000 / 1100, 2)  # +$1000 on 100 shares, -$1100 on 110
    assert res["avg_trade_pct"] == 0.0
    assert (res["avg_win_pct"], res["avg_loss_pct"]) == (10.0, -10.0)
    assert (res["best_trade_pct"], res["worst_trade_pct"]) == (10.0, -10.0)
    assert res["avg_hold_days"] == 1.0
    assert res["exposure_pct"] == 50.0  # holding at the end of days 1 and 3 of 4
    assert res["signals"] == {"BUY": 2, "SELL": 2, "HOLD": 0}


def test_open_position_at_the_end_is_reported_separately():
    res, _ = _run(_prices([100, 105]), _const(BUY))
    assert res["num_trades"] == 0  # nothing closed
    assert res["open_position"] == 100
    assert res["unrealized_pnl"] == 500.0
    assert res["total_return_pct"] == 5.0  # but it is in the return
    assert res["profit_factor"] is None and res["best_trade_pct"] is None


def test_no_losing_trades_has_no_profit_factor():
    res, _ = _run(_prices([100, 110]), _scripted([BUY, SELL]))
    assert res["profit_factor"] is None  # nothing to divide by; the UI shows it as infinite


def test_ratios_of_a_flat_run_are_undefined():
    res, _ = _run(_prices([100, 101, 99, 102]), _const({"action": "HOLD", "confidence": 0.0}))
    assert res["volatility_pct"] == 0.0
    assert res["sharpe"] is None and res["sortino"] is None
    assert res["buy_hold_sharpe"] is not None  # the benchmark still moved


def test_a_steady_rise_has_no_downside_so_no_sortino():
    res, _ = _run(_prices([100, 101, 103, 106]), _const(BUY))
    assert res["sharpe"] > 0
    assert res["sortino"] is None


def test_buy_and_hold_drawdown_is_measured_on_the_price():
    res, _ = _run(_prices([100, 120, 90, 110]), _const({"action": "HOLD", "confidence": 0.0}))
    assert res["buy_hold_max_drawdown_pct"] == -25.0  # 120 -> 90
    assert res["max_drawdown_pct"] == 0.0  # stayed in cash
    assert res["return_over_drawdown"] is None


def test_failed_decisions_are_counted():
    async def failing(symbol, as_of):
        return {"action": "HOLD", "confidence": 0.0, "reasoning": "decision-service 500", "error": True}

    res, _ = _run(_prices([100, 101, 102]), failing)
    assert res["decision_errors"] == 3
    assert res["signals"]["HOLD"] == 3
