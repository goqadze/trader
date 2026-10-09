"""Intraday bars: 30-minute ones for replaying a past moment of a session (a backtest deciding at 10:00, like a bot
that checks after the open), 5-minute ones for the intraday strategies, 15-minute ones for the dip buyer's checks.
Regular hours (9:30-16:00 New York) by default; `extended` adds the pre-market from 4:00 (the ICT Power of 3
strategy's accumulation range).

Source: Alpaca's market data (years of history, free with the Alpaca keys the news already uses) when its keys
are set, otherwise Yahoo Finance, which only keeps the last 60 days. Index futures (NQ, MES, ...) are rebuilt from
their ETFs (futures.py)."""

import os
import threading
import time
from datetime import date, datetime, time as dtime, timedelta

import httpx
import pandas as pd
import yfinance as yf

from .futures import FUTURES, rebuild
from .tools import MARKET_TZ, yahoo_symbol

BAR = timedelta(minutes=30)
TIMEFRAMES = {"30Min": "30m", "15Min": "15m", "5Min": "5m"}  # Alpaca's name -> Yahoo's interval
SESSION_START, SESSION_END = dtime(9, 30), dtime(16, 0)
PREMARKET_START = dtime(4, 0)  # Alpaca's pre-market data starts at 4:00 New York
ALPACA_BARS = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
# Alpaca's free plan serves consolidated (SIP) prices once they are 15 minutes old
ALPACA_DELAY = timedelta(minutes=16)
CURRENT_MONTH_TTL_S = 1800  # a finished month never changes; the current one is re-fetched now and then
_CACHE_MONTHS = 600  # a 5-minute month is ~1,600 bars: 600 of them stay well under 100 MB

# A failed Alpaca request is tried again after these pauses (seconds): a network blip (a DNS lookup failing for a
# moment, a dropped connection) or Alpaca busy (5xx) shouldn't fail a whole backtest
RETRY_WAITS_S = (1, 3, 9)

# Alpaca's free market data allows 200 requests a minute per account, shared by everything using the keys (this
# service on the Mac and on the server, the bots' quotes). A 5-minute month takes 2 requests (Alpaca sends about
# 2,300 bars a page), so a 5-minute backtest of 20 symbols over a year asks for ~500. Each answer says how many are
# left this minute and when the minute resets: with RATE_RESERVE or fewer left, wait for the reset rather than run
# into "429 Too Many Requests"; a 429 waits for the reset too (RATE_LIMIT_RETRIES times in a row at most).
RATE_RESERVE = 10  # left for the bots' quotes
RATE_LIMIT_RETRIES = 8
RATE_WAIT_MAX_S = 65  # Alpaca's window is a minute: never wait longer than that for one reset
RATE_WAIT_UNKNOWN_S = 15  # a 429 without the reset time
_rate = {"remaining": None, "reset": 0.0}  # Alpaca's last X-RateLimit-Remaining / -Reset (unix seconds)
_rate_lock = threading.Lock()

COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
_cache: dict[tuple[str, str, int, int], tuple[float, pd.DataFrame]] = {}  # (symbol, timeframe, year, month)
_lock = threading.Lock()  # guards _cache and _month_locks; never held during a download
_month_locks: dict[tuple[str, str, int, int], threading.Lock] = {}


def _alpaca_headers() -> dict | None:
    from .news import _has_key

    if not (_has_key("ALPACA_API_KEY") and _has_key("ALPACA_SECRET_KEY")):
        return None
    return {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def _until_reset(r: httpx.Response) -> float | None:
    """Remember the request budget an answer reports; the seconds until it resets (None without those headers)."""
    try:
        remaining, reset = int(r.headers["X-RateLimit-Remaining"]), float(r.headers["X-RateLimit-Reset"])
    except (KeyError, ValueError):
        return None
    with _rate_lock:
        _rate.update(remaining=remaining, reset=reset)
    return reset - time.time()


def _pace() -> None:
    """Before a request: when the last answer left RATE_RESERVE requests or fewer this minute, wait for the reset."""
    with _rate_lock:
        remaining, reset = _rate["remaining"], _rate["reset"]
    if remaining is not None and remaining <= RATE_RESERVE and (wait := reset - time.time()) > 0:
        time.sleep(min(wait + 0.5, RATE_WAIT_MAX_S))


def _get(url: str, params: dict, headers: dict) -> httpx.Response:
    """httpx.get, paced to Alpaca's rate limit and tried again: after RETRY_WAITS_S on a network error or a busy
    server, after the minute's reset on a 429. Anything else fails at once."""
    waits, limited = iter(RETRY_WAITS_S), 0
    while True:
        _pace()
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=30)
        except httpx.TransportError:
            if (wait := next(waits, None)) is None:
                raise
        else:
            reset_in = _until_reset(r)
            if r.status_code == 429 and limited < RATE_LIMIT_RETRIES:
                limited += 1
                wait = RATE_WAIT_UNKNOWN_S if reset_in is None else min(max(reset_in, 0) + 1, RATE_WAIT_MAX_S)
            elif r.status_code < 500 or (wait := next(waits, None)) is None:
                r.raise_for_status()
                return r
        time.sleep(wait)


def _from_alpaca(symbol: str, start: datetime, end: datetime, headers: dict, timeframe: str = "30Min") -> pd.DataFrame:
    """Split- and dividend-adjusted, like the daily bars from Yahoo. Pages until the whole range is in."""
    params = {"timeframe": timeframe, "start": start.isoformat(), "end": end.isoformat(), "adjustment": "all",
              "feed": "sip", "limit": 10000}
    rows: list[dict] = []
    while True:
        data = _get(ALPACA_BARS.format(symbol=symbol), params, headers).json()
        rows += data.get("bars") or []
        if not data.get("next_page_token"):
            break
        params["page_token"] = data["next_page_token"]
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["t"], utc=True).dt.tz_convert(MARKET_TZ)
    return df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})[COLUMNS]


def _from_yahoo(symbol: str, start: datetime, end: datetime, timeframe: str = "30Min") -> pd.DataFrame:
    df = yf.download(yahoo_symbol(symbol), start=start.date(), end=end.date() + timedelta(days=1), interval=TIMEFRAMES[timeframe],
                     progress=False, auto_adjust=True, prepost=True)
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.tz_convert(MARKET_TZ)[COLUMNS]


def _session(df: pd.DataFrame, extended: bool = False) -> pd.DataFrame:
    """Only regular hours (9:30-16:00), or with `extended` the pre-market from 4:00 too."""
    if df.empty:
        return df
    t = df.index.time
    return df[(t >= (PREMARKET_START if extended else SESSION_START)) & (t < SESSION_END)]


def _download(symbol: str, first: date, last: date, timeframe: str = "30Min") -> pd.DataFrame:
    """Pre-market and regular-hours bars (30-, 15- or 5-minute) for [first, last], indexed by each bar's START in New
    York time. Callers pick the session they want with _session."""
    start = datetime.combine(first, dtime(0), tzinfo=MARKET_TZ)
    end = min(datetime.combine(last + timedelta(days=1), dtime(0), tzinfo=MARKET_TZ), datetime.now(MARKET_TZ) - ALPACA_DELAY)
    if end <= start:
        return pd.DataFrame(columns=COLUMNS)
    headers = _alpaca_headers()
    df = _from_alpaca(symbol, start, end, headers, timeframe) if headers else _from_yahoo(symbol, start, end, timeframe)
    return _session(df, extended=True).dropna(subset=["Close"])  # both also send after-hours: never used


def _month(symbol: str, day: date, timeframe: str = "30Min") -> pd.DataFrame:
    """The bars of day's calendar month, through a cache: a backtest replays every morning of a year or more,
    one download per month instead of per day. Callers wanting the same month wait for one download; a cached
    month never waits behind another month's download (a scan runs several symbols at once)."""
    key = (symbol, timeframe, day.year, day.month)
    first = day.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    def cached() -> pd.DataFrame | None:
        hit = _cache.get(key)
        finished = last < datetime.now(MARKET_TZ).date()
        return hit[1] if hit and (finished or time.monotonic() - hit[0] < CURRENT_MONTH_TTL_S) else None

    with _lock:
        if (df := cached()) is not None:
            return df
        month_lock = _month_locks.setdefault(key, threading.Lock())
    with month_lock:
        with _lock:
            if (df := cached()) is not None:  # downloaded by the caller we waited for
                return df
        df = _download(symbol, first, last, timeframe)
        with _lock:
            if len(_cache) >= _CACHE_MONTHS:
                _cache.pop(next(iter(_cache)))
            _cache[key] = (time.monotonic(), df)
        return df


def bars_between(symbol: str, first: date, last: date, timeframe: str = "30Min", extended: bool = False) -> pd.DataFrame:
    """Regular-hours bars (30-, 15- or 5-minute) from first to last (inclusive), New York time; with `extended` the
    pre-market from 4:00 as well. A future (NQ, MES, ...) comes rebuilt from its ETF's bars."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {list(TIMEFRAMES)}")
    if symbol in FUTURES:
        return rebuild(FUTURES[symbol], bars_between(FUTURES[symbol].etf, first, last, timeframe, extended))
    months, d = [], first.replace(day=1)
    while d <= last:
        months.append(_month(symbol, d, timeframe))
        d = (d + timedelta(days=32)).replace(day=1)
    df = pd.concat([m for m in months if not m.empty]) if any(not m.empty for m in months) else pd.DataFrame(columns=COLUMNS)
    return _session(df[(df.index.date >= first) & (df.index.date <= last)], extended) if not df.empty else df


def rewind_to(df: pd.DataFrame, symbol: str, decided_at: datetime) -> tuple[pd.DataFrame, str]:
    """Replace decided_at's daily bar with that day as it stood at decided_at: the open, the high and low so far,
    the latest price as its close, the volume so far. Only 30-minute bars that had FINISHED by then count, so
    nothing later leaks in. This is what a live bot sees at that moment."""
    at = decided_at.astimezone(MARKET_TZ)
    if df.empty or df.index[-1].date() != at.date():
        raise ValueError(f"No daily bar for {symbol} on {at.date()} to replay")
    month = _session(_month(symbol, at.date()))  # regular hours: the pre-market isn't part of the day's bar
    day = month[(month.index.date == at.date()) & (month.index + BAR <= at)] if not month.empty else month
    if day.empty:
        source = "Alpaca" if _alpaca_headers() else "Yahoo (the last 60 days only; set the Alpaca keys for more)"
        raise ValueError(f"No 30-minute prices for {symbol} before {at:%Y-%m-%d %H:%M} New York from {source}")
    df = df.copy()
    df["Volume"] = df["Volume"].astype(float)
    df.loc[df.index[-1], COLUMNS] = [
        float(day["Open"].iloc[0]), float(day["High"].max()), float(day["Low"].min()),
        float(day["Close"].iloc[-1]), float(day["Volume"].sum()),
    ]
    return df, (f"Replayed {at:%H:%M} New York: {at.date()}'s bar as it stood then (from 30-minute bars), "
                f"latest price ${float(day['Close'].iloc[-1]):.2f}")
