"""Yahoo downloads shared by every bot: each symbol once, not once per bot, and one download at a time.

Every dip bot downloads its watchlist's bars at each check (a 15-minute bot every 15 minutes, all of them at the same
moment), and every bot its starting watchlist's daily closes every few minutes (the buy & hold line): one Yahoo request
per symbol. Dozens of bots on overlapping watchlists would ask for the same symbols again and again, and Yahoo blocks an
address that asks too often, which would stop the real-money bots' checks too. So:
  - a symbol downloaded less than `ttl` seconds ago is served from memory, to any bot that asks;
  - a bot that needs a shorter history gets a slice of a longer one (the download may reach further back than asked,
    `fetch_start`, so bots with a slightly longer window share it too);
  - downloads go out one at a time, GAP_SECONDS apart, whichever bot or request asks.

A scheduler pass goes one step further (Frozen): it asks once, at its start, for everything its bots will need, and
answers them from that however long the pass lasts. Left to ask one after another, each bot downloads whatever has
expired since the bot before it: with 250 symbols that is dozens of small downloads, each waiting for the last, for
minutes on end.

The answer is the frame the download itself gives: closes from `start` on, one column per symbol asked for, in that
order (a symbol without prices: an empty column), indexed by the union of their bars. A symbol that came back without
prices isn't kept: the next ask tries Yahoo again.
"""

import threading
import time
from collections.abc import Callable, Iterable
from datetime import date

import pandas as pd

GAP_SECONDS = 1.0  # between two Yahoo downloads, from any bot or request
CHUNK = 50  # symbols in one download of a pass: a live bot waiting for its own prices waits for one of these at most

_lock = threading.Lock()  # held while downloading: one download at a time, all caches together
_last_download = float("-inf")  # time.monotonic() when the last one ended

Fetch = Callable[[list[str], date], pd.DataFrame]  # (symbols, start) -> closes, one column per symbol


def _since(frame: pd.DataFrame, start: date) -> pd.DataFrame:
    t = pd.Timestamp(start)
    if getattr(frame.index, "tz", None) is not None:
        t = t.tz_localize(frame.index.tz)
    return frame[frame.index >= t]


def _frame(have: dict[str, pd.Series], symbols: list[str], start: date) -> pd.DataFrame:
    if not have:
        return pd.DataFrame(columns=symbols, dtype=float)
    return _since(pd.concat(have, axis=1).sort_index(), start).reindex(columns=symbols)


class Shared:
    """One kind of download (daily closes, or one bar interval): what came in recently, per symbol."""

    def __init__(self, ttl: float, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._kept: dict[str, tuple[float, date, pd.Series]] = {}  # symbol -> (downloaded at, from, closes)

    def clear(self) -> None:
        self._kept.clear()

    def _fresh(self, symbols: list[str], start: date) -> dict[str, pd.Series]:
        found = {s: self._kept.get(s) for s in symbols}
        return {s: k[2] for s, k in found.items() if k is not None and self.clock() - k[0] < self.ttl and k[1] <= start}

    def closes(self, symbols: list[str], start: date, fetch: Fetch, fetch_start: date | None = None) -> pd.DataFrame:
        """Closes of `symbols` from `start` on: the kept ones, and one download (from fetch_start, if earlier) for the
        rest. Raises what `fetch` raises; nothing is kept then. What is kept never waits behind another's download."""
        global _last_download
        have = self._fresh(symbols, start)
        if len(have) == len(set(symbols)):
            return _frame(have, symbols, start)
        with _lock:
            have = self._fresh(symbols, start)
            need = [s for s in symbols if s not in have]
            if need:
                begin = min(start, fetch_start or start)
                wait = _last_download + GAP_SECONDS - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    got = fetch(need, begin)
                finally:
                    _last_download = time.monotonic()
                at = self.clock()
                for s in need:
                    col = got[s].dropna() if s in got else pd.Series(dtype=float)
                    if col.empty:
                        self._kept.pop(s, None)  # no prices: ask again next time
                    else:
                        self._kept[s] = (at, begin, col)
                        have[s] = col
        return _frame(have, symbols, start)


class Frozen:
    """One scheduler pass's prices of one kind: `symbols` from `start` on, read when it's made (`ask`: a Shared's
    download, which leaves out what is already kept), in chunks of CHUNK with the lock free between them. From then on
    the bots of the pass get their answer from here, so the prices don't run out during a long pass: they are as of the
    pass's start, the moment its checks are about. `beat` is called after each chunk: the pass is alive."""

    def __init__(self, ask: Callable[[list[str], date], pd.DataFrame], symbols: Iterable[str], start: date,
                 beat: Callable[[], None] = lambda: None):
        self.ask, self.start = ask, start
        self._columns: dict[str, pd.Series] = {}
        self._failed: dict[str, Exception] = {}
        wanted = sorted(set(symbols))
        for i in range(0, len(wanted), CHUNK):
            self._read(wanted[i:i + CHUNK])
            beat()

    def _read(self, chunk: list[str]) -> None:
        wait = _last_download + GAP_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)  # outside the lock: a live bot waiting for it goes next
        try:
            got = self.ask(chunk, self.start)
        except Exception as e:  # BrokerError, or anything a broken download raises: said to whoever asks for these
            self._failed.update(dict.fromkeys(chunk, e))
            return
        for s in chunk:
            self._columns[s] = got[s].dropna() if s in got else pd.Series(dtype=float)

    def closes(self, symbols: list[str], start: date) -> pd.DataFrame:
        """What Shared.closes answers. Asked for what the pass didn't read (a longer history, a symbol new since its
        start): the bot's own download. A symbol that failed or came back empty isn't asked again in this pass: each bot
        that watches it would wait for Yahoo to fail once more."""
        if start < self.start or any(s not in self._columns and s not in self._failed for s in symbols):
            return self.ask(symbols, start)
        for s in symbols:
            if s in self._failed:
                raise self._failed[s]
        return _frame({s: self._columns[s] for s in symbols if not self._columns[s].empty}, symbols, start)
