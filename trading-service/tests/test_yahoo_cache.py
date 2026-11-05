"""Yahoo downloads shared by every bot (yahoo_cache): each symbol once, a slice for a shorter history, one at a time."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from app import dip, rotation, yahoo_cache
from app.brokers import BrokerError

DAYS = pd.date_range("2025-06-02", "2025-06-13", freq="B")  # two weeks of daily closes


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Yahoo:
    """A stand-in download: daily closes of the symbols asked for from `start` on (X: no prices), and what was asked."""

    def __init__(self, error=None, pause=0.0):
        self.asked: list[tuple[list[str], date]] = []
        self.error, self.pause = error, pause

    def __call__(self, symbols, start):
        self.asked.append((list(symbols), start))
        time.sleep(self.pause)
        if self.error:
            raise self.error
        idx = DAYS[DAYS >= pd.Timestamp(start)]
        return pd.DataFrame({s: [float("nan") if s == "X" else 100.0 + i + 10 * ord(s[0]) for i in range(len(idx))]
                             for s in symbols}, index=idx)


@pytest.fixture(autouse=True)
def no_gap(monkeypatch):
    monkeypatch.setattr(yahoo_cache, "GAP_SECONDS", 0.0)


def test_bots_share_one_download_per_symbol():
    yahoo, kept = Yahoo(), yahoo_cache.Shared(120, clock=Clock())
    first = kept.closes(["A", "B"], date(2025, 6, 2), yahoo)
    second = kept.closes(["B", "C"], date(2025, 6, 2), yahoo)
    assert yahoo.asked == [(["A", "B"], date(2025, 6, 2)), (["C"], date(2025, 6, 2))]  # B came from memory
    assert list(second.columns) == ["B", "C"]
    pd.testing.assert_frame_equal(second, Yahoo()(["B", "C"], date(2025, 6, 2)), check_freq=False)
    pd.testing.assert_series_equal(first["B"], second["B"], check_freq=False)


def test_a_shorter_history_is_a_slice_of_a_longer_one_and_a_longer_one_downloads_again():
    yahoo, kept = Yahoo(), yahoo_cache.Shared(120, clock=Clock())
    kept.closes(["A"], date(2025, 6, 9), yahoo, fetch_start=date(2025, 6, 4))  # asks from earlier than it needs
    shorter = kept.closes(["A"], date(2025, 6, 5), yahoo)  # within what came: no download
    assert yahoo.asked == [(["A"], date(2025, 6, 4))]
    assert shorter.index[0] == pd.Timestamp("2025-06-05") and shorter.index[-1] == pd.Timestamp("2025-06-13")
    first = kept.closes(["A"], date(2025, 6, 9), yahoo)
    assert first.index[0] == pd.Timestamp("2025-06-09")  # the slice from where it was asked, not from the download's start
    longer = kept.closes(["A"], date(2025, 6, 2), yahoo)
    assert yahoo.asked[-1] == (["A"], date(2025, 6, 2)) and longer.index[0] == pd.Timestamp("2025-06-02")


def test_kept_prices_expire():
    clock, yahoo = Clock(), Yahoo()
    kept = yahoo_cache.Shared(120, clock=clock)
    kept.closes(["A"], date(2025, 6, 2), yahoo)
    clock.t += 119
    kept.closes(["A"], date(2025, 6, 2), yahoo)
    assert len(yahoo.asked) == 1
    clock.t += 1
    kept.closes(["A"], date(2025, 6, 2), yahoo)
    assert len(yahoo.asked) == 2


def test_a_symbol_without_prices_comes_back_empty_and_is_asked_again():
    yahoo, kept = Yahoo(), yahoo_cache.Shared(120, clock=Clock())
    got = kept.closes(["A", "X"], date(2025, 6, 2), yahoo)
    assert list(got.columns) == ["A", "X"] and got["X"].isna().all() and got["A"].notna().all()
    kept.closes(["A", "X"], date(2025, 6, 2), yahoo)
    assert yahoo.asked[-1] == (["X"], date(2025, 6, 2))
    assert kept.closes(["X"], date(2025, 6, 2), yahoo).empty


def test_a_failed_download_raises_and_keeps_nothing():
    kept = yahoo_cache.Shared(120, clock=Clock())
    with pytest.raises(BrokerError):
        kept.closes(["A"], date(2025, 6, 2), Yahoo(error=BrokerError("Too Many Requests")))
    yahoo = Yahoo()
    kept.closes(["A"], date(2025, 6, 2), yahoo)
    assert yahoo.asked == [(["A"], date(2025, 6, 2))]


def test_downloads_go_out_one_at_a_time_and_bots_asking_together_share_one(monkeypatch):
    monkeypatch.setattr(yahoo_cache, "GAP_SECONDS", 0.3)
    yahoo, kept = Yahoo(pause=0.2), yahoo_cache.Shared(120, clock=Clock())
    with ThreadPoolExecutor(max_workers=3) as pool:
        frames = list(pool.map(lambda _: kept.closes(["A", "B"], date(2025, 6, 2), yahoo), range(3)))
    assert len(yahoo.asked) == 1 and all(f.equals(frames[0]) for f in frames)
    t = time.monotonic()
    kept.closes(["C"], date(2025, 6, 2), yahoo)  # the next download waits out the gap after the last one
    waited = time.monotonic() - t
    assert 0.3 <= waited < 0.3 + 0.2 + 0.25


def test_what_is_kept_never_waits_behind_another_download():
    """A long download of other symbols holds the lock: a bot whose prices are in memory doesn't wait for it."""
    yahoo, kept = Yahoo(), yahoo_cache.Shared(120, clock=Clock())
    kept.closes(["A"], date(2025, 6, 2), yahoo)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with yahoo_cache._lock:  # a download is going on (released before the pool waits for its thread)
            got = pool.submit(kept.closes, ["A"], date(2025, 6, 2), yahoo).result(timeout=2)
    assert list(got.columns) == ["A"] and len(yahoo.asked) == 1


def test_a_frozen_read_asks_once_for_all_and_never_expires():
    """A pass's prices: each symbol once, and every bot of the pass answered from them, however long it lasts: the kept
    copies below run out after 120 s, these don't."""
    clock, yahoo = Clock(), Yahoo()
    kept = yahoo_cache.Shared(120, clock=clock)
    read = yahoo_cache.Frozen(lambda some, since: kept.closes(some, since, yahoo), ["B", "A", "B"], date(2025, 6, 2))
    assert yahoo.asked == [(["A", "B"], date(2025, 6, 2))]
    clock.t += 3600
    got = read.closes(["B", "A"], date(2025, 6, 9))
    assert len(yahoo.asked) == 1
    assert list(got.columns) == ["B", "A"] and got.index[0] == pd.Timestamp("2025-06-09")
    pd.testing.assert_frame_equal(got, Yahoo()(["B", "A"], date(2025, 6, 2)).loc["2025-06-09":], check_freq=False)


def test_a_frozen_read_goes_out_in_chunks_with_the_lock_free_between_them(monkeypatch):
    monkeypatch.setattr(yahoo_cache, "CHUNK", 2)
    monkeypatch.setattr(yahoo_cache, "GAP_SECONDS", 0.2)
    yahoo, kept, beats, free = Yahoo(), yahoo_cache.Shared(120, clock=Clock()), [], []

    def ask(some, since):
        got = kept.closes(some, since, yahoo)
        beats.append(yahoo_cache._lock.locked())
        return got

    def sleep(seconds, real=time.sleep):
        if seconds:  # the stand-in download's own pause is none
            free.append(yahoo_cache._lock.locked())
        real(seconds)

    monkeypatch.setattr(yahoo_cache.time, "sleep", sleep)
    read = yahoo_cache.Frozen(ask, ["A", "B", "C", "D", "E"], date(2025, 6, 2), beat=lambda: beats.append("beat"))
    assert [a[0] for a in yahoo.asked] == [["A", "B"], ["C", "D"], ["E"]]
    assert beats == [False, "beat", False, "beat", False, "beat"]  # one sign of life per chunk
    assert free == [False, False]  # the gaps are waited out with the lock free: a live bot waiting for it goes next
    assert read.closes(["E", "A"], date(2025, 6, 2)).notna().all().all()


def test_a_frozen_read_doesnt_wait_when_nothing_was_downloaded(monkeypatch):
    monkeypatch.setattr(yahoo_cache, "GAP_SECONDS", 5.0)
    monkeypatch.setattr(yahoo_cache, "CHUNK", 1)
    yahoo_cache._last_download = float("-inf")
    t = time.monotonic()
    yahoo_cache.Frozen(lambda some, since: Yahoo()(some, since), ["A", "B", "C"], date(2025, 6, 2))
    assert time.monotonic() - t < 1


def test_a_frozen_read_answers_empty_for_a_symbol_without_prices_and_doesnt_ask_again():
    yahoo = Yahoo()
    read = yahoo_cache.Frozen(yahoo, ["A", "X"], date(2025, 6, 2))
    assert read.closes(["X"], date(2025, 6, 2)).empty
    got = read.closes(["A", "X"], date(2025, 6, 2))
    assert list(got.columns) == ["A", "X"] and got["X"].isna().all() and got["A"].notna().all()
    assert len(yahoo.asked) == 1


def test_a_frozen_read_says_a_failure_once_and_only_to_the_bots_that_need_those_symbols(monkeypatch):
    monkeypatch.setattr(yahoo_cache, "CHUNK", 1)
    asked = []

    def flaky(some, since):
        asked.append(some)
        if some == ["B"]:
            raise BrokerError("Too Many Requests")
        return Yahoo()(some, since)

    read = yahoo_cache.Frozen(flaky, ["A", "B"], date(2025, 6, 2))
    assert read.closes(["A"], date(2025, 6, 2)).notna().all().all()
    for _ in range(3):
        with pytest.raises(BrokerError, match="Too Many Requests"):
            read.closes(["A", "B"], date(2025, 6, 2))
    assert asked == [["A"], ["B"]]


def test_a_frozen_read_sends_what_it_didnt_plan_for_to_the_normal_download():
    """A bot that needs a longer history than the pass read (a rotation's ranking), or a symbol new since its start."""
    yahoo = Yahoo()
    read = yahoo_cache.Frozen(yahoo, ["A"], date(2025, 6, 9))
    assert read.closes(["A"], date(2025, 6, 2)).index[0] == pd.Timestamp("2025-06-02")
    assert yahoo.asked[-1] == (["A"], date(2025, 6, 2))
    assert list(read.closes(["A", "N"], date(2025, 6, 9)).columns) == ["A", "N"]
    assert yahoo.asked[-1] == (["A", "N"], date(2025, 6, 9))


def test_dip_bots_share_their_bar_downloads(monkeypatch):
    """Two bots checking at the same moment: the second downloads only the symbol the first didn't have, from SHARE_DAYS
    earlier than the first one needed, but never past Yahoo's 60-day limit on 15-minute bars."""
    asked = []
    idx = pd.date_range("2025-06-02 09:30", periods=8, freq="15min", tz="America/New_York")

    def download(tickers, start, interval, **kw):
        asked.append((list(tickers), start, interval))
        cols = pd.MultiIndex.from_product([["Close", "Open"], tickers])
        return pd.DataFrame([[100.0 + i] * len(cols) for i in range(len(idx))], index=idx, columns=cols)

    monkeypatch.setattr(dip.yf, "download", download)
    monkeypatch.setattr(dip, "utcnow", lambda: datetime(2025, 6, 2, 15, 0, tzinfo=timezone.utc))
    a = dip.yahoo_bars(["A", "BRK.B"], "15m", date(2025, 5, 20))
    b = dip.yahoo_bars(["BRK.B", "C"], "15m", date(2025, 5, 16))  # a longer window, within SHARE_DAYS
    assert asked == [(["A", "BRK-B"], "2025-05-13", "15m"), (["C"], "2025-05-09", "15m")]
    assert list(b.columns) == ["BRK.B", "C"] and b["BRK.B"].equals(a["BRK.B"])
    assert b.index[0] == pd.Timestamp("2025-06-02 09:45", tz="America/New_York")  # each bar at its close
    dip.yahoo_bars(["D"], "15m", date(2025, 4, 5))  # 58 days back: not 7 more
    assert asked[-1][1] == "2025-04-04"
    dip.yahoo_bars(["A"], "1h", date(2025, 5, 20))  # another interval: its own download
    assert asked[-1] == (["A"], "2025-05-13", "60m")


def test_bots_share_their_daily_downloads(monkeypatch):
    asked = []

    def download(tickers, start, **kw):
        asked.append((list(tickers), start))
        cols = pd.MultiIndex.from_product([["Close"], tickers])
        return pd.DataFrame([[50.0] * len(tickers), [51.0] * len(tickers)], columns=cols,
                            index=pd.DatetimeIndex(["2025-06-02", "2025-06-03"]))

    monkeypatch.setattr(rotation.yf, "download", download)
    assert rotation.universe_prices(["A", "B"], date(2025, 6, 3)) == {"A": 51.0, "B": 51.0}
    assert rotation.universe_prices(["B", "C"], date(2025, 6, 3)) == {"B": 51.0, "C": 51.0}
    assert asked == [(["A", "B"], "2025-05-24"), (["C"], "2025-05-24")]
