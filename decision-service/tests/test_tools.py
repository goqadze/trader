"""Tests for price loading in app/tools.py: the history cache and the live-bar volume projection (no network)."""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from test_ta import bars

from app import tools


@pytest.fixture
def downloads(monkeypatch):
    """Fake Yahoo: 600 business days of bars ending today; records every download."""
    calls = []
    today = datetime.now(tools.MARKET_TZ).date()
    df = bars([100 + i * 0.1 for i in range(600)])
    df.index = pd.bdate_range(end=today, periods=600)

    def fake(symbol, start, end):
        calls.append((symbol, start, end))
        return df[(df.index.date >= start) & (df.index.date <= end)]

    monkeypatch.setattr(tools, "_download", fake)
    tools._cache.clear()
    return calls, df


def test_a_backtest_downloads_the_history_once_and_never_sees_the_future(downloads):
    calls, df = downloads
    days = [d.date() for d in df.index[-200:-100]]  # 100 past trading days, oldest first (like a backtest)
    for d in days:
        out = tools.get_prices("AAPL", d)
        assert out.index[-1].date() == d  # nothing after as_of
        assert out.index[0].date() >= d - timedelta(days=tools.LOOKBACK_DAYS)
    assert len(calls) == 1


def test_today_is_always_fetched_fresh(downloads):
    calls, _ = downloads
    today = datetime.now(tools.MARKET_TZ).date()
    tools.get_prices("AAPL", today)
    tools.get_prices("AAPL", today)
    assert len(calls) == 2  # today's bar is still changing: never cached


def test_symbols_are_cached_separately(downloads):
    calls, df = downloads
    day = df.index[-50].date()
    for sym in ("AAPL", "MSFT", "AAPL"):
        tools.get_prices(sym, day)
    assert [c[0] for c in calls] == ["AAPL", "MSFT"]


def _today_bar(day: date):
    df = bars([100.0] * 25, volume=1_000_000)
    df.index = pd.bdate_range(end=day, periods=25)
    return df


@pytest.mark.parametrize("ny_time, factor", [((10, 0), 1 / 0.12), ((15, 30), 1 / 0.78)])
def test_a_forming_bar_gets_a_full_day_volume_estimate(ny_time, factor):
    day = date(2025, 6, 2)
    at = datetime(2025, 6, 2, *ny_time, tzinfo=tools.MARKET_TZ).astimezone(timezone.utc)
    out, note = tools.project_partial_volume(_today_bar(day), at)
    assert out["Volume"].iloc[-1] == pytest.approx(1_000_000 * factor)
    assert out["Volume"].iloc[-2] == 1_000_000  # finished days untouched
    assert "still forming" in note


def test_finished_bars_are_left_alone():
    day = date(2025, 6, 2)
    df = _today_bar(day)
    after_close = datetime(2025, 6, 2, 16, 30, tzinfo=tools.MARKET_TZ)
    assert tools.project_partial_volume(df, after_close)[1] is None  # the day is complete
    assert tools.project_partial_volume(df, None)[1] is None  # backtests: every bar is a finished day
    next_morning = datetime(2025, 6, 3, 10, 0, tzinfo=tools.MARKET_TZ)
    assert tools.project_partial_volume(df, next_morning)[1] is None  # no bar for June 3 yet
