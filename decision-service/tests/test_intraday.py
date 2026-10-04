"""Tests for the 30-minute bars behind backtests that decide after the open: replaying 10:00 without look-ahead,
the Alpaca source (paging, regular hours only), the Yahoo fallback, and the per-month cache. No network."""

from datetime import date, datetime

import pandas as pd
import pytest

from app import intraday
from app.tools import MARKET_TZ


def _daily(day: date, close=110.0):
    idx = pd.DatetimeIndex([pd.Timestamp(day) - pd.Timedelta(days=1), pd.Timestamp(day)])
    return pd.DataFrame({"Open": [99.0, 101.0], "High": [101.0, 115.0], "Low": [98.0, 95.0],
                         "Close": [100.0, close], "Volume": [1000, 5000]}, index=idx)  # int volume, like Yahoo


def _half_hours(day: date):
    """Regular-hours 30-minute bars indexed by their start, New York time: 9:30, 10:00, 10:30."""
    start = pd.Timestamp(f"{day} 09:30", tz="America/New_York")
    idx = pd.DatetimeIndex([start + pd.Timedelta(minutes=30 * k) for k in range(3)])
    return pd.DataFrame({"Open": [101.0, 103.0, 104.0], "High": [104.0, 115.0, 106.0], "Low": [100.0, 95.0, 103.0],
                         "Close": [103.0, 104.0, 105.0], "Volume": [800.0, 600.0, 500.0]}, index=idx)


@pytest.fixture
def half_hours(monkeypatch):
    intraday._cache.clear()
    monkeypatch.setattr(intraday, "_download", lambda symbol, first, last: _half_hours(date(2026, 8, 3)))
    yield
    intraday._cache.clear()


def test_a_10am_replay_sees_only_the_first_half_hour(half_hours):
    ten = datetime(2026, 8, 3, 10, 0, tzinfo=MARKET_TZ)
    df, note = intraday.rewind_to(_daily(date(2026, 8, 3)), "AAPL", ten)
    last = df.iloc[-1]
    # 9:30-10:00 only: the 10:00 bar's spike to 115 and dip to 95 hadn't happened yet
    assert (last.Open, last.High, last.Low, last.Close, last.Volume) == (101.0, 104.0, 100.0, 103.0, 800.0)
    assert df.iloc[0].Close == 100.0  # earlier days untouched
    assert "10:00" in note


def test_a_later_moment_includes_every_finished_bar(half_hours):
    df, _ = intraday.rewind_to(_daily(date(2026, 8, 3)), "AAPL", datetime(2026, 8, 3, 10, 45, tzinfo=MARKET_TZ))
    last = df.iloc[-1]
    assert (last.High, last.Low, last.Close, last.Volume) == (115.0, 95.0, 104.0, 1400.0)  # 10:30 bar not finished


def test_no_bars_for_the_day_is_a_clear_error(half_hours):
    with pytest.raises(ValueError, match="No 30-minute prices"):
        intraday.rewind_to(_daily(date(2026, 8, 4)), "AAPL", datetime(2026, 8, 4, 10, 0, tzinfo=MARKET_TZ))


def test_a_month_is_downloaded_once_and_shared_by_its_days(monkeypatch):
    intraday._cache.clear()
    calls = []
    monkeypatch.setattr(intraday, "_download", lambda symbol, first, last: calls.append((first, last)) or _half_hours(first))
    intraday._month("AAPL", date(2025, 3, 3))
    intraday._month("AAPL", date(2025, 3, 28))
    intraday._month("AAPL", date(2025, 4, 1))
    assert calls == [(date(2025, 3, 1), date(2025, 3, 31)), (date(2025, 4, 1), date(2025, 4, 30))]
    intraday._cache.clear()


def test_a_cached_month_never_waits_behind_another_download(monkeypatch):
    """A scan runs several symbols at once: one symbol's download mustn't stall every other decision."""
    import threading

    intraday._cache.clear()
    release, calls = threading.Event(), []

    def download(symbol, first, last):
        calls.append(symbol)
        if symbol == "SLOW":
            assert release.wait(5)
        return _half_hours(first)

    monkeypatch.setattr(intraday, "_download", download)
    intraday._month("AAPL", date(2025, 3, 3))  # cached
    slow = [threading.Thread(target=intraday._month, args=("SLOW", date(2025, 3, 3))) for _ in range(2)]
    for t in slow:
        t.start()
    done = threading.Event()
    threading.Thread(target=lambda: (intraday._month("AAPL", date(2025, 3, 10)), done.set())).start()
    assert done.wait(1)  # served from the cache while SLOW is still downloading
    release.set()
    for t in slow:
        t.join(5)
    assert calls == ["AAPL", "SLOW"]  # the two SLOW callers shared one download
    intraday._cache.clear()


def test_bars_between_spans_months_and_trims_to_the_range(monkeypatch):
    intraday._cache.clear()
    monkeypatch.setattr(intraday, "_download", lambda symbol, first, last: pd.concat([_half_hours(first), _half_hours(last)]))
    df = intraday.bars_between("AAPL", date(2025, 3, 31), date(2025, 4, 1))
    assert sorted(set(df.index.date)) == [date(2025, 3, 31), date(2025, 4, 1)]
    intraday._cache.clear()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_alpaca_bars_are_paged_and_limited_to_regular_hours(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "PK")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    bar = lambda t, c: {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 100}  # noqa: E731
    pages = [  # March 3, 2025 is winter time: 9:30 New York = 14:30 UTC
        {"bars": [bar("2025-03-03T14:00:00Z", 1.0), bar("2025-03-03T14:30:00Z", 2.0)], "next_page_token": "p2"},
        {"bars": [bar("2025-03-03T15:00:00Z", 3.0), bar("2025-03-03T21:00:00Z", 4.0)], "next_page_token": None},
    ]
    seen = []

    def fake_get(url, params, headers, timeout):
        seen.append(dict(params))
        return _Resp(pages[len(seen) - 1])

    monkeypatch.setattr(intraday.httpx, "get", fake_get)
    df = intraday._download("AAPL", date(2025, 3, 3), date(2025, 3, 3))
    assert list(df["Close"]) == [2.0, 3.0]  # 9:00 pre-market and 16:00 after-hours dropped
    assert str(df.index[0]) == "2025-03-03 09:30:00-05:00"
    assert seen[1]["page_token"] == "p2" and seen[0]["adjustment"] == "all"


def test_without_alpaca_keys_yahoo_is_the_fallback(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "# paste your key")
    used = []
    monkeypatch.setattr(intraday, "_from_yahoo", lambda symbol, start, end: used.append(symbol) or _half_hours(date(2025, 3, 3)))
    monkeypatch.setattr(intraday, "_from_alpaca", lambda *a: pytest.fail("no Alpaca keys: must not call it"))
    intraday._download("AAPL", date(2025, 3, 3), date(2025, 3, 3))
    assert used == ["AAPL"]


def test_the_endpoint_serves_bars_for_backtests(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "bars_between", lambda symbol, start, end: _half_hours(date(2025, 3, 3)))
    r = TestClient(main.app).get("/bars/intraday", params={"symbol": "aapl", "start": "2025-03-03", "end": "2025-03-03"})
    assert r.status_code == 200
    first = r.json()[0]
    assert first["t"].startswith("2025-03-03T09:30:00-05:00") and first["close"] == 103.0
