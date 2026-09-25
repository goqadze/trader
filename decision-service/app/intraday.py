"""30-minute bars, for replaying a past moment of a session (a backtest deciding at 10:00, like a bot that
checks after the open). Only regular hours (9:30-16:00 New York) count.

Source: Alpaca's market data (years of history, free with the Alpaca keys the news already uses) when its keys
are set, otherwise Yahoo Finance, which only keeps the last 60 days."""

import os
import threading
import time
from datetime import date, datetime, time as dtime, timedelta

import httpx
import pandas as pd
import yfinance as yf

from .tools import MARKET_TZ

BAR = timedelta(minutes=30)
SESSION_START, SESSION_END = dtime(9, 30), dtime(16, 0)
ALPACA_BARS = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
# Alpaca's free plan serves consolidated (SIP) prices once they are 15 minutes old
ALPACA_DELAY = timedelta(minutes=16)
CURRENT_MONTH_TTL_S = 1800  # a finished month never changes; the current one is re-fetched now and then
_CACHE_MONTHS = 240

COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
_cache: dict[tuple[str, int, int], tuple[float, pd.DataFrame]] = {}
_lock = threading.Lock()


def _alpaca_headers() -> dict | None:
    from .news import _has_key

    if not (_has_key("ALPACA_API_KEY") and _has_key("ALPACA_SECRET_KEY")):
        return None
    return {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def _from_alpaca(symbol: str, start: datetime, end: datetime, headers: dict) -> pd.DataFrame:
    """Split- and dividend-adjusted, like the daily bars from Yahoo. Pages until the whole range is in."""
    params = {"timeframe": "30Min", "start": start.isoformat(), "end": end.isoformat(), "adjustment": "all",
              "feed": "sip", "limit": 10000}
    rows: list[dict] = []
    while True:
        r = httpx.get(ALPACA_BARS.format(symbol=symbol), params=params, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        rows += data.get("bars") or []
        if not data.get("next_page_token"):
            break
        params["page_token"] = data["next_page_token"]
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["t"], utc=True).dt.tz_convert(MARKET_TZ)
    return df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})[COLUMNS]


def _from_yahoo(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    df = yf.download(symbol, start=start.date(), end=end.date() + timedelta(days=1), interval="30m", progress=False, auto_adjust=True)
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.tz_convert(MARKET_TZ)[COLUMNS]


def _download(symbol: str, first: date, last: date) -> pd.DataFrame:
    """Regular-hours 30-minute bars for [first, last], indexed by each bar's START in New York time."""
    start = datetime.combine(first, dtime(0), tzinfo=MARKET_TZ)
    end = min(datetime.combine(last + timedelta(days=1), dtime(0), tzinfo=MARKET_TZ), datetime.now(MARKET_TZ) - ALPACA_DELAY)
    if end <= start:
        return pd.DataFrame(columns=COLUMNS)
    headers = _alpaca_headers()
    df = _from_alpaca(symbol, start, end, headers) if headers else _from_yahoo(symbol, start, end)
    t = df.index.time
    return df[(t >= SESSION_START) & (t < SESSION_END)].dropna(subset=["Close"])  # Alpaca also sends pre/after-market


def _month(symbol: str, day: date) -> pd.DataFrame:
    """The bars of day's calendar month, through a cache: a backtest replays every morning of a year or more,
    one download per month instead of per day. One download at a time, so parallel runs share them."""
    key = (symbol, day.year, day.month)
    first = day.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    with _lock:
        hit = _cache.get(key)
        finished = last < datetime.now(MARKET_TZ).date()
        if hit and (finished or time.monotonic() - hit[0] < CURRENT_MONTH_TTL_S):
            return hit[1]
        df = _download(symbol, first, last)
        if len(_cache) >= _CACHE_MONTHS:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic(), df)
        return df


def bars_between(symbol: str, first: date, last: date) -> pd.DataFrame:
    """Regular-hours 30-minute bars from first to last (inclusive), New York time."""
    months, d = [], first.replace(day=1)
    while d <= last:
        months.append(_month(symbol, d))
        d = (d + timedelta(days=32)).replace(day=1)
    df = pd.concat([m for m in months if not m.empty]) if any(not m.empty for m in months) else pd.DataFrame(columns=COLUMNS)
    return df[(df.index.date >= first) & (df.index.date <= last)] if not df.empty else df


def rewind_to(df: pd.DataFrame, symbol: str, decided_at: datetime) -> tuple[pd.DataFrame, str]:
    """Replace decided_at's daily bar with that day as it stood at decided_at: the open, the high and low so far,
    the latest price as its close, the volume so far. Only 30-minute bars that had FINISHED by then count, so
    nothing later leaks in. This is what a live bot sees at that moment."""
    at = decided_at.astimezone(MARKET_TZ)
    if df.empty or df.index[-1].date() != at.date():
        raise ValueError(f"No daily bar for {symbol} on {at.date()} to replay")
    month = _month(symbol, at.date())
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
