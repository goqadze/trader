"""Tests for the technical-analysis helpers in app/ta.py (synthetic prices, no network)."""

import math

import numpy as np
import pandas as pd
import pytest

from app import ta


def bars(closes, volume=1_000_000, spread=0.5):
    """Daily bars from a list of closes: each day opens at the previous close, high/low `spread` beyond the body."""
    closes = [float(c) for c in closes]
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "Open": opens,
        "High": [max(o, c) + spread for o, c in zip(opens, closes)],
        "Low": [min(o, c) - spread for o, c in zip(opens, closes)],
        "Close": closes,
        "Volume": [float(volume)] * len(closes),
    }, index=pd.bdate_range("2025-01-01", periods=len(closes)))


def test_sma_and_ema():
    s = pd.Series(range(1, 21), dtype=float)
    assert ta.sma(s, 5).iloc[-1] == 18.0  # mean of 16..20
    assert math.isnan(ta.ema(s, 5).iloc[3])  # not enough values yet
    assert 18.0 < ta.ema(s, 5).iloc[-1] < 20.0  # an EMA hugs recent values more than the SMA does


def test_rsi_extremes_and_flat():
    assert ta.rsi(pd.Series(range(1, 40), dtype=float)).iloc[-1] == 100.0  # only gains
    assert ta.rsi(pd.Series(range(40, 1, -1), dtype=float)).iloc[-1] == 0.0  # only losses
    assert ta.rsi(pd.Series([10.0] * 30)).iloc[-1] == 50.0  # nothing moved: neutral


def test_rsi_matches_wilder_on_a_known_series():
    """Alternating +2/-1 moves: Wilder's averages settle around gain 1.0, loss 0.5 -> RS 2 -> RSI 66.7.
    Exactly: it wobbles between 65.0 (after a down day) and 68.3 (after an up day)."""
    closes, p = [], 100.0
    for i in range(400):
        p += 2 if i % 2 == 0 else -1
        closes.append(p)
    r = ta.rsi(pd.Series(closes))
    assert r.iloc[-1] == pytest.approx(65.0, abs=0.1)  # the last move was down
    assert r.iloc[-2] == pytest.approx(68.3, abs=0.1)


def test_macd_is_positive_and_above_signal_while_accelerating_up():
    line, sig, hist = ta.macd(pd.Series([100 * 1.01 ** i for i in range(80)]))
    assert line.iloc[-1] > 0 and hist.iloc[-1] > 0


def test_bollinger_bands_collapse_on_a_constant_price():
    mid, up, lo = ta.bollinger(pd.Series([50.0] * 25))
    assert mid.iloc[-1] == up.iloc[-1] == lo.iloc[-1] == 50.0


def test_atr_of_steady_one_dollar_days():
    df = bars([100.0] * 30, spread=0.5)  # every day spans exactly 99.5 - 100.5
    assert ta.atr(df).iloc[-1] == pytest.approx(1.0)


def test_adx_sees_a_strong_uptrend_and_a_flat_market():
    adx, pdi, mdi = ta.adx(bars([100 + i for i in range(80)]))
    assert adx.iloc[-1] > 40 and pdi.iloc[-1] > mdi.iloc[-1]
    choppy = 100 + np.random.default_rng(0).normal(0, 1, 120)  # random noise around 100, no drift
    flat_adx, _, _ = ta.adx(bars(choppy))
    assert flat_adx.iloc[-1] < 20


def test_swing_points_need_confirmation_on_both_sides():
    lows = pd.Series([5, 4, 3, 2, 3, 4, 5, 4, 3, 1, 2], dtype=float)
    assert ta.swing_lows(lows) == [3]  # the 1 at position 9 has only one bar after it: not confirmed yet
    highs = pd.Series([1, 2, 5, 2, 1, 2, 3], dtype=float)
    assert ta.swing_highs(highs) == [2]


def test_visits_merge_touches_that_are_close_together():
    mask = pd.Series([1, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1], dtype=bool)
    assert ta.visits(mask) == 3  # positions 0-3 are one visit, then 8, then 14


def test_candlestick_patterns():
    assert ta.is_hammer(o=10.0, h=10.2, l=8.0, c=10.1)  # long lower wick, small body at the top
    assert not ta.is_hammer(o=10.0, h=12.0, l=9.9, c=10.1)
    assert ta.is_shooting_star(o=10.0, h=12.0, l=9.9, c=9.9)
    assert ta.is_bullish_engulfing(po=10.0, pc=9.0, o=8.9, c=10.2)
    assert not ta.is_bullish_engulfing(po=9.0, pc=10.0, o=8.9, c=10.2)  # yesterday was up: nothing to engulf
    assert ta.is_bearish_engulfing(po=9.0, pc=10.0, o=10.1, c=8.8)


def test_rel_volume_compares_today_with_the_20_days_before():
    df = bars([100.0] * 25)
    df.iloc[-1, df.columns.get_loc("Volume")] = 3_000_000
    assert ta.rel_volume(df) == 3.0
