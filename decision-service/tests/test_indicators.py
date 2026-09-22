"""Tests for the technical indicators in app/tools.py (no network — synthetic prices)."""

import pandas as pd

from app.tools import indicators


def _df(values):
    return pd.DataFrame({"Close": [float(v) for v in values]})


def test_indicator_keys_and_last_close():
    out = indicators(_df(range(1, 61)))  # 1..60
    assert set(out) == {"last_close", "sma20", "sma50", "rsi14"}
    assert out["last_close"] == 60.0


def test_moving_averages_match_hand_calc():
    out = indicators(_df(range(1, 61)))
    # SMA20 = mean of the last 20 values (41..60) = 50.5
    assert round(out["sma20"], 1) == 50.5
    # SMA50 = mean of the last 50 values (11..60) = 35.5
    assert round(out["sma50"], 1) == 35.5


def test_uptrend_has_short_ma_above_long_ma():
    out = indicators(_df(range(1, 61)))  # steadily rising
    assert out["sma20"] > out["sma50"]


def test_rsi_saturates_high_on_pure_uptrend():
    out = indicators(_df(range(1, 61)))  # only gains, no losses
    assert out["rsi14"] > 99  # approaches 100


def test_rsi_saturates_low_on_pure_downtrend():
    out = indicators(_df(range(60, 0, -1)))  # only losses
    assert out["rsi14"] < 1  # approaches 0
