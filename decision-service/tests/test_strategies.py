"""Tests for the decision strategies in app/strategies.py and how the agent applies the news tilt.

decide() is tested branch by branch on hand-made facts; analyze() on synthetic bars shaped into each setup.
No network, no LLM.
"""

import numpy as np
import pandas as pd
import pytest
from pytest import approx
from test_ta import bars

from app.agent import decide as agent_decide
from app.strategies import STRATEGIES, NewsView, _divergence, format_facts

NO_NEWS = NewsView()


def run(sid, facts, news=NO_NEWS):
    return STRATEGIES[sid].decide(facts, news)


def ohlcv(rows, start="2025-01-01"):
    """Bars from explicit (open, high, low, close, volume) rows."""
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=pd.bdate_range(start, periods=len(rows)))


def random_walk(n=300, seed=0):
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))
    df = bars(closes)
    df["Volume"] = rng.uniform(0.5e6, 2e6, n)
    return df


# --- every strategy -------------------------------------------------------------------------------------

@pytest.mark.parametrize("sid", list(STRATEGIES))
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_strategy_gives_a_valid_verdict_on_random_prices(sid, seed):
    """Whatever the market does, each strategy answers BUY/SELL/HOLD with a sane confidence and a reason."""
    strat = STRATEGIES[sid]
    df = random_walk(seed=seed)
    for end in range(strat.min_bars, len(df) + 1, 7):  # many different days, incl. the very first possible one
        facts = strat.analyze(df.iloc[:end])
        assert np.isfinite(facts["last_close"])
        for news in (NO_NEWS, NewsView("bullish", ("[x, 2h ago] Apple beats earnings",)), NewsView("bearish")):
            v = strat.decide(facts, news)
            assert v.action in ("BUY", "SELL", "HOLD")
            assert 0 <= v.confidence <= 0.85 and v.rule
        assert format_facts(facts)


def test_catalog_ids_match_their_keys_and_the_simple_strategy_comes_first():
    assert list(STRATEGIES)[0] == "sma_rsi"
    assert all(k == s.id for k, s in STRATEGIES.items())
    assert len(STRATEGIES) == 11


# --- the news tilt (agent.decide) ------------------------------------------------------------------------

BREAKOUT_BUY = {"last_close": 105.0, "high_20d": 103.0, "low_10d": 98.0, "base_width_atr": 5.0,
                "close_in_range": 0.9, "rel_volume": 2.0, "sma50": 100.0}


def test_agreeing_news_raises_and_conflicting_news_lowers_any_strategys_confidence():
    base = {"strategy": "breakout", "indicators": BREAKOUT_BUY, "steps": []}
    plain = agent_decide({**base, "sentiment": "neutral"})
    assert plain["action"] == "BUY" and plain["confidence"] == 0.65
    assert agent_decide({**base, "sentiment": "bullish"})["confidence"] == 0.8
    assert agent_decide({**base, "sentiment": "bearish"})["confidence"] == 0.5  # below the default 0.6: no trade


def test_news_catalyst_is_not_tilted_twice():
    facts = {"last_close": 103.0, "prev_close": 100.0, "day_return": 0.03, "rel_volume": 2.0}
    out = agent_decide({"strategy": "news_catalyst", "indicators": facts, "sentiment": "bullish",
                        "catalysts": ["[x, 3h ago] Apple beats earnings"], "steps": []})
    assert out["action"] == "BUY" and out["confidence"] == 0.75  # 0.55 + catalyst 0.10 + volume 0.10, no +0.15


# --- 1. trend following ------------------------------------------------------------------------------------

def test_trend_following_on_real_shapes():
    up = STRATEGIES["trend_following"]
    assert run("trend_following", up.analyze(bars([100 * 1.004 ** i for i in range(260)]))).action == "BUY"
    assert run("trend_following", up.analyze(bars([100 * 0.996 ** i for i in range(260)]))).action == "SELL"


def test_trend_following_branches():
    f = {"last_close": 110.0, "sma50": 105.0, "sma200": 100.0, "sma200_20d_ago": 98.0, "adx14": 27.0, "plus_di": 30.0, "minus_di": 15.0}
    assert run("trend_following", f).confidence == approx(0.75)  # ADX >= 25 and a rising SMA200
    assert run("trend_following", {**f, "adx14": 15.0}).action == "HOLD"  # golden cross but no trend strength
    assert run("trend_following", {**f, "last_close": 104.0}).action == "HOLD"  # pulled back below SMA50
    death = run("trend_following", {**f, "sma50": 95.0, "last_close": 90.0, "plus_di": 10.0, "minus_di": 30.0})
    assert death.action == "SELL" and death.confidence == approx(0.7)  # death cross AND below SMA200


# --- 2. momentum --------------------------------------------------------------------------------------------

MOMENTUM = {"last_close": 110.0, "sma50": 100.0, "return_3m": 0.12, "macd": 1.5, "macd_signal": 1.0,
            "macd_hist": 0.5, "macd_hist_prev": 0.3, "rsi14": 60.0, "rel_volume": 1.6}


def test_momentum_branches():
    assert run("momentum", MOMENTUM).confidence == approx(0.8)  # above zero and on heavy volume
    assert run("momentum", {**MOMENTUM, "rsi14": 75.0}).action == "HOLD"  # overbought: don't chase
    assert run("momentum", {**MOMENTUM, "macd_hist": 0.2}).action == "HOLD"  # not accelerating
    assert run("momentum", {**MOMENTUM, "return_3m": -0.05}).action == "HOLD"
    faded = run("momentum", {**MOMENTUM, "macd": 0.5, "rsi14": 45.0})
    assert faded.action == "SELL"


# --- 3. breakout --------------------------------------------------------------------------------------------

def _range_then(last_close, last_volume):
    """45 days going sideways between ~98 and ~102 on 1M shares a day, then one more day."""
    rows = [(100.0, 102.0, 98.0, 100.0 + (1 if i % 2 else -1), 1e6) for i in range(45)]
    rows.append((101.0, last_close + 0.2, 100.8, last_close, last_volume))
    return ohlcv(rows)


def test_breakout_needs_volume_on_a_close_above_the_range():
    strat = STRATEGIES["breakout"]
    assert run("breakout", strat.analyze(_range_then(104.0, 3e6))).action == "BUY"
    weak = run("breakout", strat.analyze(_range_then(104.0, 1.1e6)))
    assert weak.action == "HOLD" and "false breakout" in weak.rule
    assert run("breakout", strat.analyze(_range_then(101.0, 3e6))).action == "HOLD"  # still inside the range


def test_breakout_exits_below_the_10_day_low():
    assert run("breakout", {**BREAKOUT_BUY, "last_close": 97.0}).action == "SELL"


def test_breakout_rejected_when_it_closes_near_the_lows():
    assert run("breakout", {**BREAKOUT_BUY, "close_in_range": 0.3}).action == "HOLD"


# --- 4. mean reversion ------------------------------------------------------------------------------------

MEANREV = {"open": 94.0, "high": 96.0, "low": 93.5, "last_close": 95.5, "prev_close": 94.0, "hammer": False,
           "bullish_engulfing": True, "shooting_star": False, "bearish_engulfing": False,
           "bb_mid": 100.0, "bb_upper": 106.0, "bb_lower": 94.5, "pierced_lower_band_3d": True, "min_rsi_3d": 27.0,
           "rsi14": 33.0, "sma200": 90.0, "adx14": 22.0, "plus_di": 18.0, "minus_di": 24.0}


def test_mean_reversion_buys_the_confirmed_oversold_bounce():
    v = run("mean_reversion", MEANREV)
    assert v.action == "BUY" and v.confidence == approx(0.75)  # + above SMA200 + bullish engulfing


def test_mean_reversion_refuses_a_falling_knife_and_waits_for_confirmation():
    assert run("mean_reversion", {**MEANREV, "adx14": 35.0}).action == "HOLD"  # strong downtrend
    assert run("mean_reversion", {**MEANREV, "last_close": 93.8}).action == "HOLD"  # still below the lower band
    assert run("mean_reversion", {**MEANREV, "min_rsi_3d": 38.0}).action == "HOLD"  # never oversold


def test_mean_reversion_sells_back_at_the_mean():
    assert run("mean_reversion", {**MEANREV, "last_close": 100.5}).action == "SELL"
    assert run("mean_reversion", {**MEANREV, "last_close": 107.0}).confidence == approx(0.7)  # stretched to the upper band


# --- 5. range trading ------------------------------------------------------------------------------------

RANGE = {"open": 101.0, "high": 102.5, "low": 100.2, "last_close": 102.0, "prev_close": 101.0, "hammer": False,
         "bullish_engulfing": False, "shooting_star": False, "bearish_engulfing": False,
         "support": 100.0, "resistance": 110.0, "range_height_pct": 0.10, "support_visits": 3, "resistance_visits": 2,
         "range_position": 0.2, "adx14": 15.0, "rsi14": 38.0}


def test_range_trading_branches():
    assert run("range_trading", RANGE).action == "BUY"
    assert run("range_trading", {**RANGE, "adx14": 28.0}).action == "HOLD"  # trending, not a range
    assert run("range_trading", {**RANGE, "support_visits": 1}).action == "HOLD"  # support not proven
    assert run("range_trading", {**RANGE, "range_position": 0.5}).action == "HOLD"  # the middle
    assert run("range_trading", {**RANGE, "range_position": 0.9, "last_close": 109.0}).action == "SELL"  # at resistance
    broke = run("range_trading", {**RANGE, "last_close": 99.0})
    assert broke.action == "SELL" and broke.confidence == approx(0.7)  # support failed


def test_range_analyze_finds_the_edges_and_counts_visits():
    waves = [105 + 4 * np.sin(i / 3) for i in range(60)]
    f = STRATEGIES["range_trading"].analyze(bars(waves, spread=0.3))
    assert 100 < f["support"] < 101.5 and 108.5 < f["resistance"] < 110
    assert f["support_visits"] >= 2 and f["resistance_visits"] >= 2


# --- 6. pullback to the 20 EMA ---------------------------------------------------------------------------

PULLBACK = {"open": 101.0, "high": 102.5, "low": 99.8, "last_close": 102.0, "prev_close": 101.0, "hammer": False,
            "bullish_engulfing": False, "shooting_star": False, "bearish_engulfing": False,
            "ema20": 100.0, "ema50": 96.0, "ema50_10d_ago": 94.0, "touched_ema20": True, "rsi14": 52.0}


def test_pullback_branches():
    assert run("ma_pullback", PULLBACK).confidence == approx(0.65)  # healthy RSI
    assert run("ma_pullback", {**PULLBACK, "touched_ema20": False}).action == "HOLD"
    assert run("ma_pullback", {**PULLBACK, "ema50_10d_ago": 97.0}).action == "HOLD"  # EMA50 not rising
    assert run("ma_pullback", {**PULLBACK, "last_close": 99.5}).action == "HOLD"  # closed below the EMA
    assert run("ma_pullback", {**PULLBACK, "last_close": 95.0}).action == "SELL"  # below EMA50


# --- 7. reversal ------------------------------------------------------------------------------------------

def test_divergence_detection():
    n = 50
    price = np.full(n, 100.0)
    rsi = np.full(n, 50.0)
    price[30], rsi[30] = 90.0, 25.0  # first low: oversold
    price[46], rsi[46] = 88.0, 38.0  # lower low in price, higher low in RSI, 3 bars ago
    ok, detail = _divergence(price, rsi, [30, 46], n, bullish=True)
    assert ok and "90.00 -> 88.00" in detail
    assert not _divergence(price, rsi, [30, 40], n, bullish=True)[0]  # newer low 9 bars old: stale


REVERSAL = {"open": 90.0, "high": 92.5, "low": 89.5, "last_close": 92.0, "prev_close": 90.5, "hammer": False,
            "bullish_engulfing": True, "shooting_star": False, "bearish_engulfing": False, "prev_high": 91.0,
            "rsi14": 41.0, "bullish_divergence": True, "bullish_detail": "x", "bearish_divergence": False, "bearish_detail": ""}


def test_reversal_branches():
    assert run("reversal", REVERSAL).confidence == approx(0.75)  # engulfing + closed above yesterday's high
    assert run("reversal", {**REVERSAL, "last_close": 89.8}).action == "HOLD"  # no up candle yet
    assert run("reversal", {**REVERSAL, "bullish_divergence": False}).action == "HOLD"
    top = {**REVERSAL, "bullish_divergence": False, "bearish_divergence": True, "last_close": 89.0}
    assert run("reversal", top).action == "SELL"


# --- 8. gap and go -----------------------------------------------------------------------------------------

def _gap_day(open_, low, close, volume):
    rows = [(100.0, 100.8, 99.2, 100.0, 1e6)] * 30 + [(open_, max(open_, close) + 0.3, low, close, volume)]
    return ohlcv(rows)


def test_gap_and_go():
    strat = STRATEGIES["gap_and_go"]
    held = strat.analyze(_gap_day(104.0, 103.5, 106.0, 3e6))
    assert run("gap_and_go", held).confidence == approx(0.65)  # held, strong close, no news
    assert run("gap_and_go", held, NewsView("bullish", ("[x, 1h ago] Apple beats earnings",))).confidence == approx(0.75)
    assert run("gap_and_go", strat.analyze(_gap_day(104.0, 99.9, 103.0, 3e6))).action == "HOLD"  # filled the gap
    assert run("gap_and_go", strat.analyze(_gap_day(104.0, 102.0, 103.0, 3e6))).action == "HOLD"  # fading below the open
    assert run("gap_and_go", strat.analyze(_gap_day(104.0, 103.5, 106.0, 1.2e6))).action == "HOLD"  # light volume
    assert run("gap_and_go", strat.analyze(_gap_day(100.5, 100.2, 101.0, 3e6))).action == "HOLD"  # no real gap
    assert run("gap_and_go", strat.analyze(_gap_day(96.0, 94.0, 95.0, 3e6))).action == "SELL"  # gap down, no recovery


# --- 9. news catalyst --------------------------------------------------------------------------------------

UP_DAY = {"last_close": 102.0, "prev_close": 100.0, "day_return": 0.02, "rel_volume": 1.0}


def test_news_catalyst_needs_the_price_to_agree():
    assert run("news_catalyst", UP_DAY, NewsView("unavailable")).action == "HOLD"
    assert run("news_catalyst", UP_DAY, NewsView("neutral")).action == "HOLD"
    assert run("news_catalyst", {**UP_DAY, "day_return": -0.01}, NewsView("bullish")).action == "HOLD"  # market disagrees
    plain = run("news_catalyst", UP_DAY, NewsView("bullish"))
    assert plain.action == "BUY" and plain.confidence == approx(0.55)  # opinion + small move: below the default 0.6
    assert run("news_catalyst", UP_DAY, NewsView("bullish", ("[x, 1h ago] upgrade",))).confidence == approx(0.65)
    down = run("news_catalyst", {**UP_DAY, "day_return": -0.03, "rel_volume": 2.0}, NewsView("bearish", ("[x, 1h ago] recall",)))
    assert down.action == "SELL" and down.confidence == approx(0.75)


# --- 10. fibonacci -----------------------------------------------------------------------------------------

def _fib_bars(last_open, last_low, last_close):
    """Flat at 100, a 20-day run to 120, a slide back toward ~111, then the final day."""
    closes = [100.0] * 30 + [100 + i for i in range(1, 21)] + [120 - i for i in range(1, 10)]
    df = bars(closes)
    df.loc[df.index[-1] + pd.offsets.BDay()] = [last_open, max(last_open, last_close) + 0.3, last_low, last_close, 1e6]
    return df


def test_fibonacci_buys_a_bounce_in_the_golden_zone():
    strat = STRATEGIES["fibonacci"]
    f = strat.analyze(_fib_bars(110.2, 109.6, 111.2))
    assert f["fib_618"] < f["low"] <= f["fib_50"]
    assert run("fibonacci", f).action == "BUY"
    assert run("fibonacci", strat.analyze(_fib_bars(111.8, 110.9, 112.5))).action == "HOLD"  # not down to the zone yet
    assert run("fibonacci", strat.analyze(_fib_bars(110.2, 109.6, 109.9))).action == "HOLD"  # down candle


def test_fibonacci_sells_when_the_move_fails():
    f = STRATEGIES["fibonacci"].analyze(_fib_bars(105.0, 102.0, 103.0))
    assert f["last_close"] < f["fib_786"] and run("fibonacci", f).action == "SELL"
