"""The dip buyer: when it buys, when it sells, the blacklist, the news veto, the slots, intraday check times, and that
it can't look ahead."""

import asyncio
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.engines import DipEngine
from app.engines.dip import downsample, references, uptrend
from app.models import DipConfig
from app.runner import _bar_closes

START = date(2024, 1, 2)


def _daily(**paths):
    """Business-day closes from 2024-01-02, one column per symbol (each a list of prices; shorter lists are padded with
    their last price)."""
    n = max(len(p) for p in paths.values())
    idx = pd.bdate_range("2024-01-02", periods=n)
    return pd.DataFrame({s: list(p) + [p[-1]] * (n - len(p)) for s, p in paths.items()}, index=idx)


def _run(closes, news_fn=None, daily=None, **kw):
    first, last = closes.index[0], closes.index[-1]
    # rebound off unless a test turns it on: these tests are about the rest of the rules (buy as soon as it fell)
    cfg = DipConfig(**({"symbols": list(closes.columns), "start": first.date(), "end": last.date(), "interval": "1d",
                        "lookback": 5, "drop_pct": 0.05, "stop_pct": 0.05, "max_positions": 2, "slippage_pct": 0.0,
                        "rebound": False} | kw))
    events = []

    async def emit(ev):
        events.append(ev)

    return asyncio.run(DipEngine().run(cfg, closes, emit, daily=daily, news_fn=news_fn)), events


# A climb to 110, a fall of 6.4% to 103, and a recovery back to 110
RECOVERS = [100, 102, 104, 106, 108, 110, 107, 103, 105, 108, 110, 111]


def test_it_buys_a_dip_and_sells_back_at_the_price_the_fall_started_from():
    r, _ = _run(_daily(A=RECOVERS))
    buy, sell = r["trades"]
    assert (buy["side"], buy["date"], buy["price"]) == ("BUY", "2024-01-11", 103.0)  # 103 is 6.4% under the 5-day high 110
    assert buy["reference"] == buy["target"] == 110.0 and buy["stop"] == pytest.approx(97.85)
    assert (sell["side"], sell["reason"], sell["price"]) == ("SELL", "target", 110.0)
    assert sell["pnl"] > 0 and r["num_trades"] == 1 and r["target_exits"] == 1
    assert r["dip_stats"]["bought"] == 1


def test_a_smaller_fall_is_not_bought():
    r, _ = _run(_daily(A=RECOVERS), drop_pct=0.07)  # it only fell 6.4%
    assert r["trades"] == [] and r["total_return_pct"] == 0.0


def test_the_percent_target_sells_rise_pct_above_the_buy():
    r, _ = _run(_daily(A=RECOVERS), target_mode="percent", rise_pct=0.02)
    buy, sell = r["trades"]
    assert buy["target"] == pytest.approx(105.06)  # 103 * 1.02, to the cent
    assert (sell["reason"], sell["date"], sell["price"]) == ("target", "2024-01-15", 108.0)  # 105 wasn't enough
    r, _ = _run(_daily(A=RECOVERS), target_mode="percent", rise_pct=0.019)
    assert r["trades"][1]["price"] == 105.0  # target 104.96


def test_a_stop_loss_sells_and_blacklists_the_symbol_for_good():
    # Bought at 106 (5.4% under 112), falls to 97 (more than 5% under the buy), dips again later: never bought again
    path = [100, 104, 106, 108, 110, 112, 106, 97, 100, 104, 108, 112, 114, 116, 108, 104, 110, 116]
    r, _ = _run(_daily(A=path))
    sells = [t for t in r["trades"] if t["side"] == "SELL"]
    assert [t["reason"] for t in sells] == ["stop-loss"] and sells[0]["price"] == 97.0
    assert r["stop_exits"] == 1 and r["blacklisted_at_end"] == ["A"]
    assert r["dip_stats"]["blacklisted_skips"] >= 1  # the later dip was seen and skipped
    assert r["by_symbol"][0]["status"] == "blacklisted"
    assert any(e["kind"] == "blacklisted" and e["symbol"] == "A" for e in r["events"])


def test_reenable_days_lets_a_blacklisted_symbol_trade_again():
    path = [100, 104, 106, 108, 110, 112, 106, 97, 100, 104, 108, 112, 114, 116, 108, 104, 110, 116]
    r, _ = _run(_daily(A=path), reenable_days=3)
    assert len([t for t in r["trades"] if t["side"] == "BUY"]) == 2
    assert any(e["kind"] == "reenabled" for e in r["events"])


def test_drop_from_start_measures_against_the_price_lookback_days_ago():
    # Up to 110 then down to 103: 6.4% under the high, but under 5% below the price 5 days earlier (106, then 108)
    path = [100, 104, 106, 108, 110, 109, 106, 103, 103]
    high, _ = _run(_daily(A=path))
    start, _ = _run(_daily(A=path), drop_from="start")
    assert high["dip_stats"]["bought"] == 1 and start["dip_stats"]["bought"] == 0


def test_the_deepest_falls_get_the_free_slots():
    up = [100, 102, 104, 106, 108, 110]
    r, _ = _run(_daily(A=up + [103, 110], B=up + [100, 110], C=up + [98, 110]), max_positions=2)
    bought = sorted(t["symbol"] for t in r["trades"] if t["side"] == "BUY")
    assert bought == ["B", "C"]  # C fell 10.9%, B 9.1%, A 6.4%: no slot left for A
    assert r["dip_stats"]["no_slot"] == 1


def test_each_slot_is_an_equal_part_of_the_equity_in_whole_shares():
    up = [100, 102, 104, 106, 108, 110]
    r, _ = _run(_daily(A=up + [103, 110]), max_positions=4)
    assert r["trades"][0]["shares"] == 24  # 10,000 / 4 = 2,500 -> 24 shares at 103


def test_fractional_shares_let_a_slot_smaller_than_one_share_buy():
    up = [100, 102, 104, 106, 108, 110]
    closes = _daily(A=up + [103, 110])
    r, _ = _run(closes, initial_cash=300, max_positions=4)  # a $75 slot can't buy one share at 103
    assert r["trades"] == [] and r["dip_stats"]["too_small"] == 1
    r, _ = _run(closes, initial_cash=300, max_positions=4, fractional=True)
    buy, sell = r["trades"]
    assert buy["shares"] == 0.728155  # $75 / $103, cut to a millionth of a share
    assert sell["shares"] == buy["shares"] and sell["pnl"] == pytest.approx(0.728155 * 7, abs=0.01)
    assert r["final_equity"] == pytest.approx(300 + 0.728155 * 7, abs=0.01)
    r, _ = _run(closes, initial_cash=10_000, max_positions=4, fractional=True)
    assert r["trades"][0]["shares"] == 24.271844  # with fractions the whole $2,500 slot is spent


def test_a_fractional_buy_needs_at_least_a_dollar():
    from app.engines.dip import affordable
    assert affordable(0.99, 103, fractional=True) == 0 and affordable(1.03, 103, fractional=True) == 0.01
    assert affordable(250, 103) == 2 and affordable(250, 103, fractional=True) == 2.427184
    assert affordable(0, 103, fractional=True) == affordable(50, 0, fractional=True) == 0


def _session(day: int, prices: list[float]) -> pd.DataFrame:
    """One 15-minute session on 2024-01-<day>: closes at 9:45, 10:00 ... 16:00 (26 checks; padded with the last price)."""
    times = pd.date_range(f"2024-01-{day:02d} 09:45", f"2024-01-{day:02d} 16:00", freq="15min", tz="America/New_York")
    return pd.DataFrame({"A": prices + [prices[-1]] * (len(times) - len(prices))}, index=times)


def test_a_symbol_sold_today_is_not_bought_again_the_same_day():
    # 11:45 falls to 103 (bought), 12:00 back at 110 (sold at the target), 12:15 down at 103 again: not rebought today
    closes = pd.concat([_session(2, [110] * 8 + [103, 110, 103]), _session(3, [103])])
    r, _ = _run(closes, interval="15m", lookback=1, lookback_unit="hours")
    assert [(t["side"], t["time"]) for t in r["trades"]] == [("BUY", "11:45"), ("SELL", "12:00")]


def test_max_hold_days_sells_on_time():
    path = [100, 102, 104, 106, 108, 110, 103, 103, 103, 103, 103]
    r, _ = _run(_daily(A=path), max_hold_days=2)
    sells = [t for t in r["trades"] if t["side"] == "SELL"]
    assert sells[0]["reason"] == "time" and sells[0]["hold_days"] == 2
    assert r["time_exits"] >= 1


NEWS_PATH = [100, 102, 104, 106, 108, 110, 107, 103, 103, 110]


def test_bearish_news_blocks_the_buy_for_that_day():
    seen = []

    async def news(symbol, day, at):
        seen.append((symbol, day, at))
        return {"sentiment": "bearish" if day == date(2024, 1, 11) else "neutral"}

    r, _ = _run(_daily(A=NEWS_PATH), news_fn=news, news=True)
    assert seen[0] == ("A", date(2024, 1, 11), None)  # a daily check: news up to that day's 15:30
    assert r["dip_stats"]["news_vetoes"] == 1
    assert r["trades"][0]["date"] == "2024-01-12"  # the next day's verdict was neutral
    assert any(e["kind"] == "news" for e in r["events"])


def test_intraday_news_is_asked_at_the_check():
    seen = []

    async def news(symbol, day, at):
        seen.append(at)
        return {"sentiment": "neutral"}

    closes = pd.concat([_session(2, [110] * 8 + [103]), _session(3, [110])])
    _run(closes, news_fn=news, news=True, interval="15m", lookback=1, lookback_unit="hours")
    assert seen[0].isoformat() == "2024-01-02T11:45:00-05:00"


def test_news_off_never_asks():
    async def news(symbol, day, at):
        raise AssertionError("news asked while off")

    r, _ = _run(_daily(A=RECOVERS), news_fn=news)
    assert r["dip_stats"]["bought"] == 1


def test_failed_news_buys_on_prices_and_counts_it():
    async def news(symbol, day, at):
        return {"sentiment": "unavailable", "error": True}

    r, _ = _run(_daily(A=RECOVERS), news_fn=news, news=True)
    assert r["dip_stats"]["bought"] == 1 and r["no_news_decisions"] == 1


def test_the_trend_filter_only_buys_dips_in_an_uptrend():
    n = 260
    rising = list(np.linspace(50, 110, n)) + [103, 110]
    falling = list(np.linspace(170, 110, n)) + [103, 110]
    closes = _daily(UP=rising, DOWN=falling)
    window = closes.iloc[n - 6:]  # the run itself starts near the end; the history before it feeds the averages
    r, _ = _run(window, trend_filter=True, daily=closes)
    bought = [t["symbol"] for t in r["trades"] if t["side"] == "BUY"]
    assert bought == ["UP"] and r["dip_stats"]["trend_skips"] >= 1


def test_uptrend_needs_200_days_and_uses_the_previous_close():
    closes = pd.DataFrame({"A": np.linspace(1, 2, 220)}, index=pd.bdate_range("2023-01-02", periods=220))
    t = uptrend(closes)
    assert not t["A"].iloc[199]  # day 200's own close isn't known before it
    assert t["A"].iloc[201]


def test_references_only_use_earlier_checks():
    closes = pd.DataFrame({"A": [1.0, 5.0, 2.0, 3.0, 4.0]})
    cfg = DipConfig(symbols=["A"], start=date(2024, 1, 2), end=date(2024, 2, 1), interval="1d", lookback=2)
    high = references(closes, cfg)["A"].tolist()
    assert np.isnan(high[0]) and np.isnan(high[1]) and high[2:] == [5.0, 5.0, 3.0]
    start = references(closes, cfg.model_copy(update={"drop_from": "start"}))["A"].tolist()
    assert start[2:] == [1.0, 5.0, 2.0]


def test_intraday_checks_skip_the_bar_that_closes_at_the_bell():
    """A live bot checks at 9:45 ... 15:45; the 16:00 close only marks the day's equity."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"2024-01-0{d} {t}", tz="America/New_York") for d in (2, 3)
                            for t in ("15:00", "15:30", "16:00")])
    closes = pd.DataFrame({"A": [110, 110, 100, 100, 100, 100]}, index=idx)
    r, _ = _run(closes, interval="30m", lookback=1, lookback_unit="hours")  # a window of 2 half-hour checks
    buy = r["trades"][0]
    assert (buy["date"], buy["time"]) == ("2024-01-03", "15:00")  # not 2024-01-02 16:00, when it first fell


def test_the_window_in_hours_counts_bars_of_the_interval():
    cfg = DipConfig(symbols=["A"], start=date(2024, 1, 2), end=date(2024, 2, 1), interval="15m", lookback=2, lookback_unit="hours")
    assert cfg.window_bars() == 8
    assert cfg.model_copy(update={"lookback_unit": "days"}).window_bars() == 2 * 26
    assert DipConfig(symbols=["A"], start=date(2024, 1, 2), end=date(2024, 2, 1), interval="1h", lookback=3).window_bars() == 21


def test_the_benchmark_holds_every_symbol_in_equal_parts():
    r, _ = _run(_daily(A=[100] * 6 + [110], B=[100] * 6 + [90]))
    assert r["equity_curve"][-1]["price"] == pytest.approx(10_000.0)  # +10% and -10% in equal parts
    assert r["benchmark_symbols"] == ["A", "B"]


def test_the_result_serializes_to_json():
    r, _ = _run(_daily(A=RECOVERS, B=[100, 104, 106, 108, 110, 112, 106, 97, 100]))
    json.dumps(r)
    assert r["verdict"]["grade"] == "no"  # one or two trades prove nothing


def test_the_breaker_stops_new_buys():
    up = [100, 102, 104, 106, 108, 110]
    r, _ = _run(_daily(A=up + [103, 90, 85, 84, 92, 95], B=up + [110, 110, 110, 100, 95, 110]), max_drawdown_pct=0.05,
                stop_pct=0.3)
    assert r["breaker_tripped_on"] is not None
    assert [t["symbol"] for t in r["trades"] if t["side"] == "BUY"] == ["A"]


def test_config_rules():
    base = {"symbols": ["aapl", "AAPL", "msft"], "start": date(2024, 1, 2), "end": date(2024, 6, 1)}
    assert DipConfig(**base).symbols == ["AAPL", "MSFT"]
    with pytest.raises(ValidationError):
        DipConfig(**base | {"interval": "1d", "lookback_unit": "hours"})
    with pytest.raises(ValidationError):
        DipConfig(**base | {"symbols": ["BTC-USD"]})
    with pytest.raises(ValidationError):
        DipConfig(**base | {"end": date(2024, 1, 1)})
    with pytest.raises(ValidationError):
        DipConfig(**base | {"interval": "1h", "lookback": 0})


def test_bar_closes_regroup_half_hours_into_hours_from_the_open():
    starts = pd.DatetimeIndex([pd.Timestamp(f"2024-01-02 {t}", tz="America/New_York")
                               for t in ("09:30", "10:00", "10:30", "11:00", "15:00", "15:30")])
    bars = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]}, index=starts)
    hourly = _bar_closes(bars, 60, 30)
    assert [t.strftime("%H:%M") for t in hourly.index] == ["10:30", "11:30", "15:30", "16:00"]
    assert hourly.tolist() == [2.0, 4.0, 5.0, 6.0]
    same = _bar_closes(bars, 30, 30)
    assert same.index[0].strftime("%H:%M") == "10:00" and same.iloc[0] == 1.0


# --- Waiting for the turn (rebound) -----------------------------------------------------------------------


def test_with_rebound_it_waits_for_the_turn_up_from_the_low():
    # 110 -> 103 (fell 6.4%: waits) -> 100 (still falling: the low) -> 101 (+1% off the low: bought) -> 110 (sold)
    r, _ = _run(_daily(A=[100, 102, 104, 106, 108, 110, 103, 100, 101, 105, 110]), rebound=True, rebound_pct=0.01)
    buy, sell = r["trades"]
    assert (buy["date"], buy["price"], buy["reference"]) == ("2024-01-12", 101.0, 110.0)
    assert (buy["low_price"], buy["low_t"], buy["rebound_pct"]) == (100.0, "2024-01-11", 1.0)
    assert (buy["peak_price"], buy["peak_t"]) == (110.0, "2024-01-09")  # where the fall started
    assert (sell["reason"], sell["price"]) == ("target", 110.0)
    assert r["dip_stats"]["rebounds"] == 1


def test_without_rebound_the_same_fall_is_bought_at_once():
    r, _ = _run(_daily(A=[100, 102, 104, 106, 108, 110, 103, 100, 101, 105, 110]), rebound=False)
    assert r["trades"][0]["price"] == 103.0 and "low_t" not in r["trades"][0]


def test_a_bigger_rebound_waits_longer():
    path = [100, 102, 104, 106, 108, 110, 103, 100, 101, 102, 104, 110]
    r, _ = _run(_daily(A=path), rebound=True, rebound_pct=0.03)
    assert r["trades"][0]["price"] == 104.0  # 102 is only +2% off the low of 100


def test_a_dip_back_at_its_reference_before_turning_is_over():
    # From 103 straight back to 111: past the price the fall started from, nothing left to buy
    r, _ = _run(_daily(A=[100, 102, 104, 106, 108, 110, 103, 111, 111]), rebound=True, rebound_pct=0.01)
    assert r["trades"] == [] and r["dip_stats"]["dips"] == 1


def test_a_fall_keeps_waiting_past_the_window_and_targets_where_it_started():
    # 5-day window: by the turn the window's high is long gone, but the target is still the 110 the fall started from
    path = [100, 102, 104, 106, 108, 110, 104, 100, 96, 92, 90, 89, 88, 89.5, 95, 110]
    r, _ = _run(_daily(A=path), rebound=True, rebound_pct=0.015, stop_pct=0.2)
    buy, sell = r["trades"]
    assert buy["price"] == 89.5 and buy["low_price"] == 88.0 and buy["target"] == 110.0
    assert sell["reason"] == "target"


def test_every_round_trip_carries_its_chart_points():
    # Bought at 103, falls further to 101 before recovering: the low on the chart is 101, after the buy
    r, _ = _run(_daily(A=[100, 102, 104, 106, 108, 110, 103, 101, 105, 110]))
    sell = r["trades"][1]
    assert (sell["peak_t"], sell["peak_price"]) == ("2024-01-09", 110.0)
    assert (sell["low_t"], sell["low_price"]) == ("2024-01-11", 101.0)
    assert (sell["buy_t"], sell["t"]) == ("2024-01-10", "2024-01-15")


def test_intraday_chart_points_carry_the_time():
    closes = pd.concat([_session(2, [110] * 8 + [103, 102, 110]), _session(3, [110])])
    r, _ = _run(closes, interval="15m", lookback=1, lookback_unit="hours")
    sell = r["trades"][1]
    assert sell["buy_t"] == "2024-01-02T11:45:00-05:00" and sell["low_t"] == "2024-01-02T12:00:00-05:00"


def test_downsample_keeps_the_highs_and_lows():
    s = pd.Series(np.sin(np.linspace(0, 20, 5000)) + np.linspace(0, 1, 5000))
    s.iloc[1234] = 9.0
    s.iloc[3210] = -9.0
    thin = downsample(s, 200)
    assert len(thin) <= 202 and thin.max() == 9.0 and thin.min() == -9.0
    assert thin.index.is_monotonic_increasing
    assert downsample(s.iloc[:50], 200).equals(s.iloc[:50])
