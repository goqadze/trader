"""Yahoo downloads shared by every bot: each symbol once, not once per bot, and one download at a time.

Every dip bot downloads its watchlist's bars at each check (a 15-minute bot every 15 minutes, all of them at the same
moment), and every bot its starting watchlist's daily closes every few minutes (the buy & hold line): one Yahoo request
per symbol. Dozens of bots on overlapping watchlists would ask for the same symbols again and again, and Yahoo blocks an
address that asks too often, which would stop the real-money bots' checks too. So:
  - a symbol downloaded less than `ttl` seconds ago is served from memory, to any bot that asks;
  - a bot that needs a shorter history gets a slice of a longer one (the download may reach further back than asked,
    `fetch_start`, so bots with a slightly longer window share it too);
  - downloads go out one at a time, GAP_SECONDS apart, whichever bot or request asks.

The answer is the frame the download itself gives: closes from `start` on, one column per symbol asked for, in that
order (a symbol without prices: an empty column), indexed by the union of their bars. A symbol that came back without
prices isn't kept: the next ask tries Yahoo again.
"""

import threading
import time
from collections.abc import Callable
from datetime import date

import pandas as pd

GAP_SECONDS = 1.0  # between two Yahoo downloads, from any bot or request

_lock = threading.Lock()  # held while downloading: one download at a time, all caches together
_last_download = float("-inf")  # time.monotonic() when the last one ended

Fetch = Callable[[list[str], date], pd.DataFrame]  # (symbols, start) -> closes, one column per symbol


def _since(frame: pd.DataFrame, start: date) -> pd.DataFrame:
    t = pd.Timestamp(start)
    if getattr(frame.index, "tz", None) is not None:
        t = t.tz_localize(frame.index.tz)
    return frame[frame.index >= t]


class Shared:
    """One kind of download (daily closes, or one bar interval): what came in recently, per symbol."""

    def __init__(self, ttl: float, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._kept: dict[str, tuple[float, date, pd.Series]] = {}  # symbol -> (downloaded at, from, closes)

    def clear(self) -> None:
        self._kept.clear()

    def _fresh(self, symbol: str, start: date) -> bool:
        kept = self._kept.get(symbol)
        return kept is not None and self.clock() - kept[0] < self.ttl and kept[1] <= start

    def closes(self, symbols: list[str], start: date, fetch: Fetch, fetch_start: date | None = None) -> pd.DataFrame:
        """Closes of `symbols` from `start` on: the kept ones, and one download (from fetch_start, if earlier) for the
        rest. Raises what `fetch` raises; nothing is kept then."""
        global _last_download
        with _lock:
            have = {s: self._kept[s][2] for s in symbols if self._fresh(s, start)}
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
        if not have:
            return pd.DataFrame(columns=symbols, dtype=float)
        return _since(pd.concat(have, axis=1).sort_index(), start).reindex(columns=symbols)
