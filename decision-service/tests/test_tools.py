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


def test_whole_number_volume_from_yahoo_can_be_scaled():
    # Yahoo's volume column is int64; writing the scaled (fractional) estimate into it used to crash every
    # decision made during the session (bots checking after the open)
    df = _today_bar(date(2025, 6, 2))
    df["Volume"] = df["Volume"].astype("int64")
    out, _ = tools.project_partial_volume(df, datetime(2025, 6, 2, 10, 0, tzinfo=tools.MARKET_TZ))
    assert out["Volume"].iloc[-1] == pytest.approx(1_000_000 / 0.12)


def test_finished_bars_are_left_alone():
    day = date(2025, 6, 2)
    df = _today_bar(day)
    after_close = datetime(2025, 6, 2, 16, 30, tzinfo=tools.MARKET_TZ)
    assert tools.project_partial_volume(df, after_close)[1] is None  # the day is complete
    assert tools.project_partial_volume(df, None)[1] is None  # backtests: every bar is a finished day
    next_morning = datetime(2025, 6, 3, 10, 0, tzinfo=tools.MARKET_TZ)
    assert tools.project_partial_volume(df, next_morning)[1] is None  # no bar for June 3 yet


# --- replaying a past moment of a session (backtests deciding at 10:00) ------------------------------------

def _daily(day: date, close=110.0):
    idx = pd.DatetimeIndex([pd.Timestamp(day) - pd.Timedelta(days=1), pd.Timestamp(day)])
    return pd.DataFrame({"Open": [99.0, 101.0], "High": [101.0, 115.0], "Low": [98.0, 95.0],
                         "Close": [100.0, close], "Volume": [1000.0, 5000.0]}, index=idx)


def _half_hours(day: date):
    """30-minute bars indexed by their start, New York time: 9:30, 10:00, 10:30."""
    start = pd.Timestamp(f"{day} 09:30", tz="America/New_York")
    idx = pd.DatetimeIndex([start, start + pd.Timedelta(minutes=30), start + pd.Timedelta(minutes=60)])
    return pd.DataFrame({"Open": [101.0, 103.0, 104.0], "High": [104.0, 115.0, 106.0], "Low": [100.0, 95.0, 103.0],
                         "Close": [103.0, 104.0, 105.0], "Volume": [800.0, 600.0, 500.0]}, index=idx)


@pytest.fixture
def half_hours(monkeypatch):
    tools._intraday.clear()
    monkeypatch.setattr(tools, "_download_intraday", lambda symbol: _half_hours(date(2026, 8, 3)))
    yield
    tools._intraday.clear()


def test_a_10am_replay_sees_only_the_first_half_hour(half_hours):
    ten = datetime(2026, 8, 3, 10, 0, tzinfo=tools.MARKET_TZ)
    df, note = tools.rewind_to(_daily(date(2026, 8, 3)), "AAPL", ten)
    last = df.iloc[-1]
    # 9:30-10:00 only: the 10:00 bar's spike to 115 and dip to 95 hadn't happened yet
    assert (last.Open, last.High, last.Low, last.Close, last.Volume) == (101.0, 104.0, 100.0, 103.0, 800.0)
    assert df.iloc[0].Close == 100.0  # earlier days untouched
    assert "10:00" in note


def test_a_later_moment_includes_every_finished_bar(half_hours):
    df, _ = tools.rewind_to(_daily(date(2026, 8, 3)), "AAPL", datetime(2026, 8, 3, 10, 45, tzinfo=tools.MARKET_TZ))
    last = df.iloc[-1]
    assert (last.High, last.Low, last.Close, last.Volume) == (115.0, 95.0, 104.0, 1400.0)  # 10:30 bar not finished


def test_no_intraday_data_is_a_clear_error(half_hours):
    with pytest.raises(ValueError, match="60 days"):
        tools.rewind_to(_daily(date(2026, 8, 4)), "AAPL", datetime(2026, 8, 4, 10, 0, tzinfo=tools.MARKET_TZ))


def test_intraday_bars_are_downloaded_once(monkeypatch):
    tools._intraday.clear()
    calls = []
    monkeypatch.setattr(tools, "_download_intraday", lambda symbol: calls.append(symbol) or _half_hours(date(2026, 8, 3)))
    tools.intraday_bars("AAPL")
    tools.intraday_bars("AAPL")
    assert calls == ["AAPL"]
    tools._intraday.clear()


def test_a_past_10am_decision_uses_the_replayed_day(monkeypatch):
    from app import agent

    replayed = []
    history = bars([100 + i * 0.1 for i in range(300)])  # plenty for any strategy's warm-up

    def fake_rewind(df, symbol, at):
        replayed.append(at)
        return df, "Replayed 10:00"

    monkeypatch.setattr(agent, "get_prices", lambda symbol, as_of: history)
    monkeypatch.setattr(agent, "rewind_to", fake_rewind)
    ten = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    out = agent.fetch_data({"symbol": "AAPL", "as_of": date(2026, 8, 3), "decided_at": ten, "strategy": "sma_rsi"})
    assert replayed == [ten] and "Replayed 10:00" in out["steps"]
    # Without a decision moment (the default 15:30 backtest decision) the finished bar is used as before
    agent.fetch_data({"symbol": "AAPL", "as_of": date(2026, 8, 3), "strategy": "sma_rsi"})
    assert len(replayed) == 1
