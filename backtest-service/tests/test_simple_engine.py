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

from app.engines.simple import SimplePortfolioEngine, _exit_levels
from app.models import RunConfig


# --- helpers -------------------------------------------------------------

def _prices(values, start=date(2025, 1, 6)):
    idx = [start + timedelta(days=i) for i in range(len(values))]
    return pd.Series([float(v) for v in values], index=idx)


def _run(prices, decide, bars=None, slots=None, **overrides):
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

    result = asyncio.run(SimplePortfolioEngine().run(cfg, prices, decide, emit, bars=bars, slots=slots))
    return result, events


def _bars(rows, start=date(2025, 1, 6)):
    """(open, high, low, close) per day -> (close Series, OHLC bars), as the runner passes them."""
    idx = [start + timedelta(days=i) for i in range(len(rows))]
    bars = pd.DataFrame([[float(x) for x in r] for r in rows], index=idx, columns=["Open", "High", "Low", "Close"])
    return bars["Close"], bars


def _const(signal):
    """A decide() that returns the same signal every day."""
    async def decide(symbol, as_of, slot="close"):
        return dict(signal)
    return decide


def _scripted(signals):
    """A decide() that returns signals in order, repeating the last one after they run out."""
    state = {"i": 0}

    async def decide(symbol, as_of, slot="close"):
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

    async def counting_decide(symbol, as_of, slot="close"):
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
    async def failing(symbol, as_of, slot="close"):
        return {"action": "HOLD", "confidence": 0.0, "reasoning": "decision-service 500", "error": True}

    res, _ = _run(_prices([100, 101, 102]), failing)
    assert res["decision_errors"] == 3
    assert res["signals"]["HOLD"] == 3


def test_decisions_without_news_are_counted():
    signals = [{"action": "HOLD", "confidence": 0.0, "sentiment": "unavailable"},
               {"action": "HOLD", "confidence": 0.0, "sentiment": "bullish"},
               {"action": "HOLD", "confidence": 0.0, "error": True}]  # a failure is counted as an error, not here
    res, _ = _run(_prices([100, 101, 102]), _scripted(signals))
    assert res["no_news_decisions"] == 1



# --- stops and targets during the day (OHLC bars) ---------------------------

BUY_96_108 = {"action": "BUY", "confidence": 0.9, "position": {"stop_loss": 96.0, "target": 108.0}}


def _only_sell(res):
    sells = [t for t in res["trades"] if t["side"] == "SELL"]
    assert len(sells) == 1
    return sells[0]


def test_stop_fills_at_its_level_when_touched_during_the_day():
    # Day 2 dips to 95 (through the 96 stop) but closes back at 99: a live stop order sells at ~96, not the close
    closes, bars = _bars([(100, 100, 100, 100), (99, 100, 95, 99), (99, 101, 98, 100)])
    sell = _only_sell(_run(closes, _const(BUY_96_108), bars=bars, rebalance_days=100)[0])
    assert (sell["reason"], sell["price"], sell["date"]) == ("stop-loss", 96.0, "2025-01-07")


def test_a_gap_below_the_stop_fills_at_the_open():
    closes, bars = _bars([(100, 100, 100, 100), (93, 94, 90, 91)])  # opens under the stop
    sell = _only_sell(_run(closes, _const(BUY_96_108), bars=bars, rebalance_days=100)[0])
    assert (sell["reason"], sell["price"]) == ("stop-loss", 93.0)


def test_target_fills_at_its_level_when_touched_during_the_day():
    closes, bars = _bars([(100, 100, 100, 100), (101, 109, 100, 102)])  # spikes through 108, closes at 102
    sell = _only_sell(_run(closes, _const(BUY_96_108), bars=bars, rebalance_days=100)[0])
    assert (sell["reason"], sell["price"]) == ("target", 108.0)


def test_a_gap_above_the_target_fills_at_the_open():
    closes, bars = _bars([(100, 100, 100, 100), (112, 115, 110, 111)])
    sell = _only_sell(_run(closes, _const(BUY_96_108), bars=bars, rebalance_days=100)[0])
    assert (sell["reason"], sell["price"]) == ("target", 112.0)


def test_a_day_touching_both_levels_counts_as_the_stop():
    closes, bars = _bars([(100, 100, 100, 100), (100, 110, 94, 100)])
    sell = _only_sell(_run(closes, _const(BUY_96_108), bars=bars, rebalance_days=100)[0])
    assert (sell["reason"], sell["price"]) == ("stop-loss", 96.0)


def test_the_entry_day_range_does_not_trigger_the_stop():
    # Bought at day 1's close; day 1's own low happened before the entry
    closes, bars = _bars([(100, 101, 90, 100), (100, 101, 99, 100)])
    res, _ = _run(closes, _const(BUY_96_108), bars=bars, rebalance_days=100)
    assert res["num_trades"] == 0 and res["open_position"] > 0


def test_stop_fill_still_pays_slippage():
    closes, bars = _bars([(100, 100, 100, 100), (99, 100, 95, 99)])
    sell = _only_sell(_run(closes, _const(BUY_96_108), bars=bars, rebalance_days=100, slippage_pct=0.01)[0])
    assert sell["price"] == round(96.0 * 0.99, 4)



# --- deciding at the bots' moments: 10:00, 15:30, or both --------------------------------------------------

def _slots(rows, start=date(2025, 1, 6)):
    """Per day: (10:00 price, 9:30-10:00 high/low, 15:30 price, 10:00-15:30 high/low, 15:30-16:00 high/low),
    as runner._fetch_slots builds them. Each high/low is a (high, low) pair."""
    out = {}
    for k, (p10, a, p1530, b, c) in enumerate(rows):
        out[start + timedelta(days=k)] = {"open_price": float(p10), "close_price": float(p1530), "close_at": "15:30",
                                          "a_high": a[0], "a_low": a[1], "b_high": b[0], "b_low": b[1],
                                          "c_high": c[0], "c_low": c[1]}
    return pd.DataFrame.from_dict(out, orient="index")


FLAT = (100, 100)  # a stretch that stayed at 100


def _recording(signal):
    """A decide() that returns `signal` and remembers which slots it was asked for."""
    asked = []

    async def decide(symbol, as_of, slot="close"):
        asked.append((as_of.isoformat(), slot))
        return dict(signal)
    return decide, asked


def test_an_open_decision_fills_at_the_10am_price():
    closes, bars = _bars([(100, 106, 99, 105), (105, 106, 104, 105)])
    slots = _slots([(102, (103, 99), 104, (106, 101), (105, 104)), (105, (105, 104), 105, (106, 104), (106, 104))])
    decide, asked = _recording({"action": "BUY", "confidence": 0.9})
    res, _ = _run(closes, decide, bars=bars, slots=slots, decide_at="open", rebalance_days=100)
    assert asked == [("2025-01-06", "open")]  # only the open slot, never the close
    assert res["trades"][0]["price"] == 102.0  # the 10:00 price, not the 105 close


def test_a_close_decision_fills_at_the_1530_price_like_a_bot():
    # A live bot decides and buys at 15:30, not on the 16:00 close
    closes, bars = _bars([(100, 106, 99, 105)])
    slots = _slots([(101, (102, 99), 103, (104, 100), (106, 102))])
    decide, asked = _recording({"action": "BUY", "confidence": 0.9})
    res, _ = _run(closes, decide, bars=bars, slots=slots, rebalance_days=100)
    assert asked == [("2025-01-06", "close")]
    assert res["trades"][0]["price"] == 103.0


def test_a_1530_entry_is_protected_for_the_last_half_hour():
    # Bought at 15:30 for 100 (stop 96); the last 30 minutes drop to 95
    closes, bars = _bars([(100, 101, 95, 96)])
    slots = _slots([(100, FLAT, 100, FLAT, (100, 95))])
    res, _ = _run(closes, _const(BUY_96_108), bars=bars, slots=slots, rebalance_days=100)
    sell = _only_sell(res)
    assert (sell["reason"], sell["price"], sell["hold_days"]) == ("stop-loss", 96.0, 0)


def test_a_10am_entry_is_protected_for_the_rest_of_that_day():
    # Bought at 10:00 for 100; the afternoon drops to 94, through the 96 stop
    closes, bars = _bars([(101, 101, 94, 95)])
    slots = _slots([(100, (101, 99), 97, (100, 94), (97, 95))])
    res, _ = _run(closes, _const(BUY_96_108), bars=bars, slots=slots, decide_at="open", rebalance_days=100)
    sell = _only_sell(res)
    assert (sell["reason"], sell["price"], sell["hold_days"]) == ("stop-loss", 96.0, 0)


def test_the_morning_before_10am_is_checked_before_deciding():
    # Holding from day 1; day 2 dips to 95 before 10:00: stopped out at 96 before the 10:00 decision
    closes, bars = _bars([(100, 100, 100, 100), (99, 100, 95, 99)])
    slots = _slots([(100, FLAT, 100, FLAT, FLAT), (97, (99, 95), 99, (100, 97), (99, 99))])
    decide, asked = _recording(BUY_96_108)
    res, _ = _run(closes, decide, bars=bars, slots=slots, decide_at="open")
    buys = [t for t in res["trades"] if t["side"] == "BUY"]
    assert _only_sell(res)["price"] == 96.0
    assert len(buys) == 2 and buys[1]["price"] == 97.0  # flat again at 10:00, so the BUY signal re-enters


def test_both_decides_twice_on_a_decision_day():
    closes, bars = _bars([(100, 101, 99, 100)] * 3)
    slots = _slots([(100, (101, 99), 100, (101, 99), (101, 99))] * 3)
    decide, asked = _recording({"action": "HOLD", "confidence": 0.0})
    _, events = _run(closes, decide, bars=bars, slots=slots, decide_at="both", rebalance_days=2)
    assert asked == [("2025-01-06", "open"), ("2025-01-06", "close"), ("2025-01-08", "open"), ("2025-01-08", "close")]
    steps = [e for e in events if e["type"] == "step"]
    assert [[d["slot"] for d in s["decisions"]] for s in steps] == [["open", "close"], [], ["open", "close"]]


def test_a_morning_buy_can_be_sold_at_1530():
    closes, bars = _bars([(100, 104, 99, 104)])
    slots = _slots([(100, (101, 99), 103, (103, 99), (104, 103))])
    res, _ = _run(closes, _scripted([BUY, SELL]), bars=bars, slots=slots, decide_at="both")
    sell = _only_sell(res)
    assert (sell["reason"], sell["price"], sell["pnl"]) == ("signal", 103.0, 300.0)  # 100 shares, +$3 at 15:30


def test_a_day_without_30_minute_data_falls_back_to_the_close():
    closes, bars = _bars([(100, 101, 99, 100), (100, 101, 99, 102)])
    slots = _slots([(100, (101, 99), 100, (101, 99), (101, 99))])  # nothing for day 2
    decide, asked = _recording({"action": "BUY", "confidence": 0.9})
    res, _ = _run(closes, decide, bars=bars, slots=slots, decide_at="both", position_pct=0.5)
    # day 2: no 10:00 to replay (a failed decision), but the close decision still runs, on the close
    assert asked == [("2025-01-06", "open"), ("2025-01-06", "close"), ("2025-01-07", "close")]
    assert res["decision_errors"] == 1


def test_close_decisions_are_logged_with_their_slot():
    _, events = _run(_prices([100, 101]), _const({"action": "HOLD", "confidence": 0.0}))
    steps = [e for e in events if e["type"] == "step"]
    assert [d["slot"] for d in steps[0]["decisions"]] == ["close"]


# --- the same exits and safety net as a live bot -----------------------------------------------------------

def test_stop_and_target_come_from_the_actual_fill_like_a_bot():
    # With 1% slippage the fill is 101; a bot sets its levels from that fill (not the 100 signal price), to the cent
    cfg = RunConfig(start=date(2025, 1, 1), end=date(2025, 2, 1), stop_pct=0.04, target_pct=0.08)
    assert _exit_levels(cfg, {"entry": 100, "stop_loss": 96, "target": 108}, 101.0) == (96.96, 109.08)
    # ... and the engine really uses them: 96.96 is hit on day 2, not the signal's 96
    closes, bars = _bars([(100, 100, 100, 100), (99, 99, 96.9, 98)])
    sell = _only_sell(_run(closes, _const({"action": "BUY", "confidence": 0.9, "position": {"entry": 100, "stop_loss": 96, "target": 108}}),
                           bars=bars, rebalance_days=100, slippage_pct=0.01, stop_pct=0.04, target_pct=0.08)[0])
    assert (sell["reason"], sell["price"]) == ("stop-loss", round(96.96 * 0.99, 4))


def test_without_run_levels_the_signals_distances_apply_to_the_fill():
    cfg = RunConfig(start=date(2025, 1, 1), end=date(2025, 2, 1))  # stop_pct / target_pct not set
    assert _exit_levels(cfg, {"entry": 100, "stop_loss": 95, "target": 110}, 102.0) == (96.9, 112.2)
    assert _exit_levels(cfg, {}, 102.0) == (0.0, math.inf)  # no position block: no stop, no target


def test_the_drawdown_breaker_stops_decisions_like_a_paused_bot():
    # Bought at 100 with no stop; the price falls 25%: the 20% breaker trips during day 3, before its decision
    # (a live bot's 5-minute check would pause it then too), and no decision follows
    decide, asked = _recording({"action": "BUY", "confidence": 0.9})
    res, events = _run(_prices([100, 90, 75, 70, 80]), decide)
    assert res["breaker_tripped_on"] == "2025-01-08"
    assert [a[0] for a in asked] == ["2025-01-06", "2025-01-07"]
    notes = [d for e in events if e["type"] == "step" for d in e["decisions"] if "Drawdown breaker" in d["reasoning"]]
    assert len(notes) == 1


def test_the_breaker_can_be_turned_off():
    res, _ = _run(_prices([100, 90, 75, 70]), _const(BUY), max_drawdown_pct=0)
    assert res["breaker_tripped_on"] is None
