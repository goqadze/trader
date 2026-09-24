"""Tests for the original simple strategy (sma_rsi) as the agent's decide node runs it.

This is the SMA/RSI + news-sentiment confidence logic, unchanged since it was the only rule set.
It is pure (no network, no LLM), so we can pin every branch exactly.
"""

from app.agent import decide as _decide_rules  # no "strategy" in the state -> the default, sma_rsi


def _state(sma20, sma50, rsi, sentiment=None):
    s = {"indicators": {"sma20": sma20, "sma50": sma50, "rsi14": rsi, "last_close": 100.0}, "steps": []}
    if sentiment is not None:
        s["sentiment"] = sentiment
    return s


def test_uptrend_not_overbought_buys_at_base_confidence():
    out = _decide_rules(_state(52, 50, 55))  # sma20 > sma50, rsi < 70
    assert out["action"] == "BUY"
    assert out["confidence"] == 0.6  # no news -> no nudge


def test_bullish_news_raises_buy_confidence():
    out = _decide_rules(_state(52, 50, 55, "bullish"))
    assert out["action"] == "BUY"
    assert round(out["confidence"], 2) == 0.75  # agreement adds +0.15


def test_bearish_news_conflicts_and_lowers_buy_confidence():
    out = _decide_rules(_state(52, 50, 55, "bearish"))
    assert out["action"] == "BUY"
    assert round(out["confidence"], 2) == 0.45  # conflict subtracts 0.15


def test_downtrend_not_oversold_sells():
    out = _decide_rules(_state(48, 50, 55))  # sma20 < sma50, rsi > 30
    assert out["action"] == "SELL"
    assert out["confidence"] == 0.6


def test_bearish_news_raises_sell_confidence():
    out = _decide_rules(_state(48, 50, 55, "bearish"))
    assert out["action"] == "SELL"
    assert round(out["confidence"], 2) == 0.75  # SELL agrees with bearish


def test_uptrend_but_overbought_holds():
    out = _decide_rules(_state(52, 50, 75))  # uptrend but rsi >= 70 -> neither BUY nor SELL
    assert out["action"] == "HOLD"
    assert out["confidence"] == 0.4


def test_neutral_news_leaves_confidence_unchanged():
    out = _decide_rules(_state(52, 50, 55, "neutral"))
    assert out["confidence"] == 0.6


def test_a_step_is_recorded():
    out = _decide_rules(_state(52, 50, 55))
    assert len(out["steps"]) == 1
    assert "BUY" in out["steps"][0]


def test_sma_rsi_is_the_default_strategy():
    assert _decide_rules({**_state(52, 50, 55), "strategy": "sma_rsi"}) == _decide_rules(_state(52, 50, 55))
