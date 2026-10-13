"""Alpaca's SIP prices when Yahoo has none (alpaca_prices): the frame Yahoo gives, so a check sees the same window."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from app import alpaca_prices, dip, rotation
from app.brokers.base import BrokerError, Quote
from app.market import NY


def ny(h: int, m: int = 0, day: int = 2) -> datetime:
    """A New York time on June <day> 2025 (a Monday), as UTC."""
    return datetime(2025, 6, day, h, m, tzinfo=NY).astimezone(timezone.utc)


def midnight(month: int, day: int) -> datetime:
    """When Alpaca's daily bar of that day starts: midnight New York."""
    return datetime(2025, month, day, tzinfo=NY).astimezone(timezone.utc)


NOW = ny(11, 0) + timedelta(seconds=20)  # the 11:00 check


class FakeAlpaca:
    """Alpaca's bars (up to `end`, as the API gives them) and IEX prices; remembers what was asked."""

    def __init__(self, bars: dict, prices: dict):
        self._bars, self._prices, self.calls, self.adjustments = bars, prices, [], []

    def bars(self, symbols, timeframe, start, end, feed="sip", adjustment="all"):
        self.calls.append(("bars", list(symbols), timeframe, start, end))
        self.adjustments.append(adjustment)
        since = start if isinstance(start, datetime) else datetime.min.replace(tzinfo=timezone.utc)
        return {s: [(t, c) for t, c in self._bars.get(s, []) if since <= t <= end] for s in symbols if s in self._bars}

    def quotes(self, symbols):
        self.calls.append(("quotes", list(symbols)))
        return {s: Quote(price=p, at=NOW) for s, p in self._prices.items() if s in symbols}


def fifteen(closes: list[float], first=(9, 30), day=2) -> list[tuple[datetime, float]]:
    t = ny(*first, day=day)
    return [(t + timedelta(minutes=15 * i), c) for i, c in enumerate(closes)]


def test_15_minute_bars_give_a_check_the_same_window_and_price_as_yahoo():
    # Alpaca: a pre-market bar, the regular bars 9:30-10:30 (the later ones aren't 16 minutes old yet), and A at $103 now
    src = FakeAlpaca({"A": [(ny(8, 0), 99.0)] + fifteen([110, 112, 111, 110, 109, 104, 103.5])}, {"A": 103.0})
    got = alpaca_prices.closes(["A"], "15m", date(2025, 6, 2), now=NOW, src=src)
    assert src.calls == [("bars", ["A"], "15Min", date(2025, 6, 2), NOW - timedelta(minutes=16)), ("quotes", ["A"])]
    assert [t.strftime("%H:%M") for t in got.index] == ["09:45", "10:00", "10:15", "10:30", "10:45", "11:15"]
    assert list(got["A"]) == [110, 112, 111, 110, 109, 103.0]
    # What Yahoo gives at 11:00:20: the same bars, the 10:45-11:00 one, and the still-forming one at the price now
    yahoo = pd.Series([110, 112, 111, 110, 109, 104, 103.0],
                      index=pd.date_range("2025-06-02 09:45", periods=6, freq="15min", tz=NY).append(
                          pd.DatetimeIndex([pd.Timestamp("2025-06-02 11:15", tz=NY)])))
    bot = SimpleNamespace(interval="15m", lookback=1, lookback_unit="hours", drop_from="high")
    moment = dip.check_moment(bot, NOW)
    assert dip.stand(got["A"], bot, moment, date(2025, 6, 2)) == dip.stand(yahoo, bot, moment, date(2025, 6, 2)) == (112, 103.0)


def test_a_symbol_without_a_price_now_is_empty_during_the_session_and_bars_alone_after_it():
    src = FakeAlpaca({"A": fifteen([110, 111]), "B": fifteen([50, 51])}, {"A": 103.0})
    got = alpaca_prices.closes(["A", "B", "C"], "15m", date(2025, 6, 2), now=NOW, src=src)
    assert list(got.columns) == ["A", "B", "C"] and got["B"].isna().all() and got["C"].isna().all()
    evening = ny(17, 0)
    alpaca_prices._kept.clear()  # other prices for the same day: nothing kept from the ones above
    src = FakeAlpaca({"A": fifteen([110] * 26)}, {"A": 103.0})
    got = alpaca_prices.closes(["A"], "15m", date(2025, 6, 2), now=evening, src=src)
    assert [c[0] for c in src.calls] == ["bars"]  # no price now after the close: the bars are all there
    assert got.index[-1] == pd.Timestamp("2025-06-02 16:00", tz=NY) and len(got) == 26


def test_hourly_bars_are_counted_from_the_open_like_yahoos():
    src = FakeAlpaca({"A": [(ny(9, 30) + timedelta(minutes=30 * i), float(i + 1)) for i in range(5)]}, {"A": 6.0})
    got = alpaca_prices.closes(["A"], "1h", date(2025, 6, 2), now=ny(12, 0) + timedelta(seconds=20), src=src)
    assert src.calls[0][2] == "30Min"
    assert {t.strftime("%H:%M"): c for t, c in got["A"].items()} == {"10:30": 2.0, "11:30": 4.0, "12:30": 6.0}


def test_daily_closes_by_day_and_today_the_price_now():
    days = [(midnight(5, 29), 100.0), (midnight(5, 30), 101.0), (midnight(6, 2), 99.0)]  # today's bar so far last
    got = alpaca_prices.closes(["A"], "1d", date(2025, 5, 20), now=NOW, src=FakeAlpaca({"A": days}, {"A": 98.5}))
    assert {t.strftime("%m-%d"): c for t, c in got["A"].items()} == {"05-29": 100.0, "05-30": 101.0, "06-02": 98.5}
    intraday = FakeAlpaca({"A": fifteen([110])}, {"A": 103.0})
    alpaca_prices.closes(["A"], "15m", date(2025, 6, 2), now=NOW, src=intraday)
    daily = FakeAlpaca({"A": days}, {"A": 98.5})
    alpaca_prices._kept.clear()
    alpaca_prices.closes(["A"], "1d", date(2025, 5, 20), now=NOW, src=daily)
    assert (intraday.adjustments, daily.adjustments) == (["split"], ["all"])  # as Yahoo adjusts them
    after = alpaca_prices.closes(["A"], "1d", date(2025, 5, 20), now=ny(17, 0), src=FakeAlpaca({"A": days}, {}))
    assert after["A"].iloc[-1] == 99.0


def test_bars_read_today_are_asked_again_only_from_where_the_last_ask_ended():
    src = FakeAlpaca({"A": fifteen([110, 112, 111, 110, 109, 104, 103.5]), "B": fifteen([50] * 7)}, {"A": 103.0, "B": 50.0})
    alpaca_prices.closes(["A"], "15m", date(2025, 6, 2), now=NOW, src=src)
    later = NOW + timedelta(minutes=15)
    got = alpaca_prices.closes(["A", "B"], "15m", date(2025, 6, 2), now=later, src=src)
    asks = [c for c in src.calls if c[0] == "bars"]
    assert asks[1:] == [("bars", ["B"], "15Min", date(2025, 6, 2), later - timedelta(minutes=16)),  # new: all of it
                        ("bars", ["A"], "15Min", NOW - timedelta(minutes=16), later - timedelta(minutes=16))]  # since then
    fresh = alpaca_prices.closes(["A", "B"], "15m", date(2025, 6, 2), now=later,
                                 src=FakeAlpaca({"A": fifteen([110, 112, 111, 110, 109, 104, 103.5]), "B": fifteen([50] * 7)},
                                                {"A": 103.0, "B": 50.0}))
    assert got.equals(fresh) and list(got["A"].dropna())[-2:] == [104.0, 103.0]  # 10:45-11:00 came in, then the price now
    alpaca_prices.closes(["A"], "15m", date(2025, 5, 20), now=later, src=src)  # an earlier start: read again from it
    alpaca_prices.closes(["A"], "15m", date(2025, 6, 2), now=ny(11, 0, day=3), src=src)  # the next day: all of it again
    assert [c[3] for c in src.calls if c[0] == "bars"][-2:] == [date(2025, 5, 20), date(2025, 6, 2)]


# --- Yahoo first, Alpaca for the rest --------------------------------------------------------------------------------

def frame(**cols) -> pd.DataFrame:
    return pd.DataFrame(cols, index=pd.date_range("2025-06-02 09:45", periods=2, freq="15min", tz=NY))


@pytest.fixture
def alpaca(monkeypatch):
    asked = []

    def closes(symbols, interval, start):
        asked.append(list(symbols))
        return pd.DataFrame({s: [7.0, 8.0] for s in symbols},
                            index=pd.DatetimeIndex([pd.Timestamp("2025-06-02 10:00", tz=NY), pd.Timestamp("2025-06-02 10:15", tz=NY)]))

    monkeypatch.setattr(alpaca_prices, "closes", closes)
    return asked


def test_yahoos_answer_is_used_as_it_is(alpaca):
    got = alpaca_prices.backed(lambda s, start: frame(A=[1.0, 2.0], B=[3.0, 4.0]), ["A", "B"], "15m", date(2025, 6, 2))
    assert alpaca == [] and got.equals(frame(A=[1.0, 2.0], B=[3.0, 4.0]))


def test_the_symbols_yahoo_has_no_prices_for_come_from_alpaca(alpaca):
    got = alpaca_prices.backed(lambda s, start: frame(A=[1.0, 2.0], B=[float("nan")] * 2), ["A", "B", "C"], "15m",
                               date(2025, 6, 2))
    assert alpaca == [["B", "C"]] and list(got.columns) == ["A", "B", "C"]
    assert list(got["A"].dropna()) == [1.0, 2.0] and list(got["B"].dropna()) == [7.0, 8.0]


def test_when_yahoo_fails_everything_comes_from_alpaca(alpaca):
    def blocked(symbols, start):
        raise BrokerError("prices failed: Too Many Requests")

    got = alpaca_prices.backed(blocked, ["A", "B"], "15m", date(2025, 6, 2))
    assert alpaca == [["A", "B"]] and list(got.columns) == ["A", "B"] and list(got["A"]) == [7.0, 8.0]


def test_only_when_both_fail_is_it_an_error(monkeypatch):
    def down(symbols, interval, start):
        raise BrokerError("Alpaca bars: 500")

    def blocked(symbols, start):
        raise BrokerError("prices failed: Too Many Requests")

    monkeypatch.setattr(alpaca_prices, "closes", down)
    with pytest.raises(BrokerError, match="Too Many Requests; Alpaca's prices failed too: Alpaca bars: 500"):
        alpaca_prices.backed(blocked, ["A"], "15m", date(2025, 6, 2))
    partial = alpaca_prices.backed(lambda s, start: frame(A=[1.0, 2.0], B=[float("nan")] * 2), ["A", "B"], "15m",
                                   date(2025, 6, 2))
    assert list(partial["A"]) == [1.0, 2.0] and partial["B"].isna().all()  # what Yahoo had, B still empty


def test_a_blocked_yahoo_leaves_the_bots_their_prices(monkeypatch):
    """The real wiring: yfinance fails, dip bars and daily closes come from Alpaca (keys from the settings)."""
    def blocked(*a, **kw):
        raise RuntimeError("YFRateLimitError: Too Many Requests. Rate limited. Try after a while.")

    src = FakeAlpaca({"A": fifteen([110, 112, 111])}, {"A": 103.0})
    monkeypatch.setattr(dip.yf, "download", blocked)
    monkeypatch.setattr(alpaca_prices, "source", lambda: src)
    monkeypatch.setattr(alpaca_prices, "utcnow", lambda: NOW)
    monkeypatch.setattr(dip, "utcnow", lambda: NOW)
    bars = dip.yahoo_bars(["A"], "15m", date(2025, 5, 30))
    assert list(bars["A"]) == [110, 112, 111, 103.0] and src.calls[0][3] == date(2025, 5, 23)  # from SHARE_DAYS earlier
    src = FakeAlpaca({"A": [(midnight(5, 30), 101.0)]}, {"A": 103.0})
    monkeypatch.setattr(alpaca_prices, "source", lambda: src)
    assert rotation.universe_prices(["A"], date(2025, 6, 2)) == {"A": 103.0}
