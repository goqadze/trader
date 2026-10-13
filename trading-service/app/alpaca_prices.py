"""Prices from Alpaca when Yahoo doesn't answer (or has none for a symbol): the same closes, from the SIP feed.

SIP is the consolidated feed: every trade on every US exchange, which is what Yahoo shows too. (IEX, Alpaca's free
real-time feed, is one exchange: a thin ETF can go a day with almost no IEX bars.) Alpaca's free plan gives SIP bars
once they are 15 minutes old, so the bars come up to SIP_DELAY ago, and during the session the price now is added
where Yahoo has its still-forming bar: the bots' own IEX price (the latest trade, or the bid/ask midpoint when that
trade is old; AlpacaBroker.quote). A dip check's window only holds bars that closed one bar before the check
(dip.stand), so with 15-minute or longer bars it is the window Yahoo gives; a 5-minute bot's misses its last two bars.
Measured 2026-10-13, 8 symbols x 3 weeks of 15-minute bars: the same bars, median gap 0.000%, largest 0.27%, the same
7-day highs.

Alpaca pages bars by about 1,000, so 160 symbols' three weeks of 15-minute bars are some 140 requests (of 200 a
minute). Complete SIP bars don't change, so each symbol's are kept for the day: the day's first ask reads them all (so
a split or dividend adjusts the history), later ones only the bars since the last ask, about one request.

`backed` puts it behind a Yahoo download (dip.yahoo_bars, rotation.yahoo_closes): Yahoo first, Alpaca for the symbols
it gave nothing for. Market data only, with the paper keys (the live ones if those are all there are).
"""

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta

import pandas as pd

from .brokers import BrokerError
from .brokers.alpaca import AlpacaBroker
from .config import settings
from .db import utcnow
from .market import NY, ny_date, session_bounds

logger = logging.getLogger("trading-service")

SIP_DELAY = timedelta(minutes=16)  # the free plan's SIP bars are 15 minutes old at least; one more for the clocks
# Alpaca's bar for each interval, and the interval's length in minutes. Hourly bars are two 30-minute ones counted from
# the open (9:30-10:30 ...), like Yahoo's: Alpaca's own hourly bars start on the hour
TIMEFRAMES = {"5m": ("5Min", 5), "15m": ("15Min", 15), "30m": ("30Min", 30), "1h": ("30Min", 60)}
# Like Yahoo's: its daily closes are adjusted for dividends too (auto_adjust), its intraday bars for splits only
ADJUSTMENT = {"1Day": "all"}

# (symbol, Alpaca timeframe) -> (the New York day they were read, read from, read up to, {bar start: close})
_kept: dict[tuple[str, str], tuple[date, date, datetime, dict[datetime, float]]] = {}


def source() -> AlpacaBroker:
    for key, secret in ((settings.alpaca_paper_key_id, settings.alpaca_paper_secret_key),
                        (settings.alpaca_live_key_id, settings.alpaca_live_secret_key)):
        if key and secret:
            return AlpacaBroker(key, secret)  # market data only: the same with either account's keys
    raise BrokerError("no Alpaca keys for prices")


def closes(symbols: list[str], interval: str, start: date, now: datetime | None = None,
           src: AlpacaBroker | None = None) -> pd.DataFrame:
    """What dip.yahoo_bars gives (rotation.yahoo_closes for "1d"): closes from `start`, one column per symbol;
    intraday, the regular session's bars indexed by when each closed (New York time), the last one the price now;
    daily, by day, today's the price now during the session. During the session a symbol without a price now comes
    back as an empty column: no trading on prices 16 minutes old."""
    now = now or utcnow()
    src = src or source()
    today = ny_date(now)
    bounds = session_bounds(today)
    in_session = bounds is not None and bounds[0] <= now < bounds[1]
    daily = interval == "1d"
    timeframe, minutes = ("1Day", 0) if daily else TIMEFRAMES[interval]
    raw = _sip_bars(src, symbols, timeframe, start, now - SIP_DELAY, today)
    prices = src.quotes(symbols) if in_session else {}
    sessions: dict[date, tuple[datetime, datetime] | None] = {}

    def session_open(t: datetime) -> datetime | None:  # the open of the regular session t falls in (None: outside)
        day = t.astimezone(NY).date()
        if day not in sessions:
            sessions[day] = session_bounds(day)
        b = sessions[day]
        return b[0] if b and b[0] <= t < b[1] else None

    cols: dict[str, pd.Series] = {}
    for s in symbols:
        rows = sorted(raw.get(s, []))
        kept: dict = {}
        if rows and not (in_session and s not in prices):
            if daily:
                kept = {pd.Timestamp(t.astimezone(NY).date()): c for t, c in rows}
                if in_session:  # today's bar so far: the price now instead
                    kept = {d: c for d, c in kept.items() if d < pd.Timestamp(today)} | {pd.Timestamp(today): prices[s].price}
            else:
                step = timedelta(minutes=minutes)
                for t, c in rows:
                    if (o := session_open(t)) is not None:
                        kept[o + (t - o) // step * step + step] = c  # a later 30-minute bar of the same hour: its close
                if in_session:
                    kept[bounds[0] + (now - bounds[0]) // step * step + step] = prices[s].price  # Yahoo's forming bar
        times = sorted(kept)
        if daily:
            idx = pd.DatetimeIndex(times)
        else:
            idx = pd.DatetimeIndex(times, tz="UTC").tz_convert(NY) if times else pd.DatetimeIndex([], tz=NY)
        cols[s] = pd.Series([kept[t] for t in times], index=idx, dtype=float)
    return pd.concat(cols, axis=1).sort_index().reindex(columns=symbols)


def _sip_bars(src: AlpacaBroker, symbols: list[str], timeframe: str, start: date, end: datetime,
              today: date) -> dict[str, list[tuple[datetime, float]]]:
    """src.bars, from what was kept today: only the bars since the last ask for the symbols read today from `start` or
    earlier, all of them for the others."""
    kept = {s: k for s in symbols if (k := _kept.get((s, timeframe))) and k[0] == today and k[1] <= start}
    new = [s for s in symbols if s not in kept]
    if new:
        got = src.bars(new, timeframe, start, end, adjustment=ADJUSTMENT.get(timeframe, "split"))
        for s in new:
            _kept[(s, timeframe)] = (today, start, end, dict(got.get(s, [])))
    if kept:
        got = src.bars(list(kept), timeframe, min(k[2] for k in kept.values()), end,
                       adjustment=ADJUSTMENT.get(timeframe, "split"))
        for s, (day, since, _, bars) in kept.items():
            _kept[(s, timeframe)] = (day, since, end, bars | dict(got.get(s, [])))
    return {s: sorted(_kept[(s, timeframe)][3].items()) for s in symbols}


def backed(fetch: Callable[[list[str], date], pd.DataFrame], symbols: list[str], interval: str,
           start: date) -> pd.DataFrame:
    """Yahoo's closes (`fetch`), and Alpaca's for the symbols Yahoo gave none for: all of them when it failed. Raises
    only when both fail; a symbol neither has stays an empty column."""
    try:
        got, failed = fetch(symbols, start), None
    except Exception as e:  # the download's BrokerError, or anything a broken download raises
        got, failed = None, e
    missing = list(symbols) if got is None else [s for s in symbols if s not in got or got[s].dropna().empty]
    if not missing:
        return got
    try:
        more = closes(missing, interval, start)
    except Exception as e:
        if failed is not None:
            raise BrokerError(f"{failed}; Alpaca's prices failed too: {e}") from e
        logger.warning("no prices for %s from Yahoo, nor from Alpaca: %s", ", ".join(missing), e)
        return got
    logger.warning("prices for %s from Alpaca SIP (Yahoo: %s)", ", ".join(missing), failed or "none for them")
    if got is None:
        return more.reindex(columns=symbols)
    return pd.concat([got.drop(columns=missing, errors="ignore"), more], axis=1).sort_index().reindex(columns=symbols)
