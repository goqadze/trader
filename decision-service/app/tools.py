import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

MARKET_TZ = ZoneInfo("America/New_York")
# ~290 trading days: enough for an SMA200 plus a few weeks of its slope (the trend-following strategy)
LOOKBACK_DAYS = 420

# Finished days never change, so a backtest (hundreds of decisions on one symbol) downloads the history once
# instead of once per decision -- faster, and it stays under Yahoo's rate limits when a bot checks daily.
# symbol -> (first day covered, last FINISHED day covered, bars)
_cache: dict[str, tuple[date, date, pd.DataFrame]] = {}
_cache_lock = threading.Lock()
_CACHE_SYMBOLS = 32


def _download(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Daily OHLCV bars in [start, end] from Yahoo Finance (free, no API key); auto_adjust corrects for splits/dividends."""
    df = yf.download(symbol, start=start, end=end + timedelta(days=1), progress=False, auto_adjust=True)  # end is exclusive
    # Newer yfinance returns two-level column names; flatten them to plain "Close", "Open", ...
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def get_prices(symbol: str, as_of: date, lookback_days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Daily prices up to and including as_of. Never returns later data (no look-ahead).
    A past as_of is served from the cache when possible; today (live bots) is always fetched fresh,
    because today's bar is still changing."""
    start = as_of - timedelta(days=lookback_days)
    today = datetime.now(MARKET_TZ).date()
    if as_of < today:
        with _cache_lock:
            hit = _cache.get(symbol)
        if hit and hit[0] <= start and as_of <= hit[1]:
            df = hit[2]
        else:
            df = _download(symbol, start, today)
            with _cache_lock:
                if len(_cache) >= _CACHE_SYMBOLS:
                    _cache.pop(next(iter(_cache)))  # drop the oldest symbol
                _cache[symbol] = (start, today - timedelta(days=1), df)  # today's bar may be unfinished
    else:
        df = _download(symbol, start, as_of)
    # Safety filter: drop anything dated after as_of so backtests can't see the future
    return df[(df.index.date >= start) & (df.index.date <= as_of)]


# A backtest deciding at a past moment of a session (10:00, like a bot on "after the open") must see that day as
# it stood then, not its finished daily bar. Yahoo keeps 30-minute bars for the last 60 days only.
INTRADAY_BAR = timedelta(minutes=30)
INTRADAY_TTL_S = 1800
_intraday: dict[str, tuple[float, pd.DataFrame]] = {}
_intraday_lock = threading.Lock()


def _download_intraday(symbol: str) -> pd.DataFrame:
    """The last 60 days of 30-minute bars, indexed by each bar's START in New York time."""
    df = yf.download(symbol, period="60d", interval="30m", progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.tz_convert(MARKET_TZ)


def intraday_bars(symbol: str) -> pd.DataFrame:
    """_download_intraday through a short-lived cache: a backtest replays dozens of past mornings on one symbol.
    One download at a time, so simultaneous backtest runs wait for it and share it."""
    with _intraday_lock:
        hit = _intraday.get(symbol)
        if hit and time.monotonic() - hit[0] < INTRADAY_TTL_S:
            return hit[1]
        df = _download_intraday(symbol)
        if len(_intraday) >= _CACHE_SYMBOLS:
            _intraday.pop(next(iter(_intraday)))
        _intraday[symbol] = (time.monotonic(), df)
        return df


def rewind_to(df: pd.DataFrame, symbol: str, decided_at: datetime) -> tuple[pd.DataFrame, str]:
    """Replace decided_at's daily bar with that day as it stood at decided_at: the open, the high and low so far,
    the latest price as its close, the volume so far. Only 30-minute bars that had FINISHED by then count, so
    nothing later leaks in. This is what a live bot sees at that moment."""
    at = decided_at.astimezone(MARKET_TZ)
    if df.empty or df.index[-1].date() != at.date():
        raise ValueError(f"No daily bar for {symbol} on {at.date()} to replay")
    bars = intraday_bars(symbol)
    day = bars[(bars.index.date == at.date()) & (bars.index + INTRADAY_BAR <= at)]
    if day.empty:
        raise ValueError(f"No 30-minute prices for {symbol} before {at:%Y-%m-%d %H:%M} New York "
                         "(Yahoo keeps them for the last 60 days)")
    df = df.copy()
    df.loc[df.index[-1], ["Open", "High", "Low", "Close", "Volume"]] = [
        float(day["Open"].iloc[0]), float(day["High"].max()), float(day["Low"].min()),
        float(day["Close"].iloc[-1]), float(day["Volume"].sum()),
    ]
    return df, (f"Replayed {at:%H:%M} New York: {at.date()}'s bar as it stood then (from 30-minute bars), "
                f"latest price ${float(day['Close'].iloc[-1]):.2f}")


# Share of a regular session's volume done N minutes after the 9:30 open. US stocks trade in a U-shape:
# busy open, quiet midday, busy close (the closing auction alone is ~8%). Rough, but far better than
# comparing half an hour of volume with full-day averages. Assumes a full 6.5h day (half days overshoot).
_VOLUME_PROFILE = ([0, 30, 60, 120, 180, 240, 300, 330, 360, 390],
                   [0.0, 0.12, 0.20, 0.33, 0.43, 0.52, 0.61, 0.67, 0.78, 1.0])


def project_partial_volume(df: pd.DataFrame, decided_at: datetime | None) -> tuple[pd.DataFrame, str | None]:
    """A live decision during the session sees today's bar still forming. Scale its volume up to a full-day
    estimate so "volume 1.5x average" means the same at 10:00 as in a backtest (which sees finished days).
    Returns the (possibly adjusted) bars and a note for the steps trail."""
    if decided_at is None or df.empty:
        return df, None
    now = decided_at.astimezone(MARKET_TZ)
    if df.index[-1].date() != now.date():
        return df, None
    minutes = now.hour * 60 + now.minute - (9 * 60 + 30)  # since the open
    fraction = float(np.interp(minutes, *_VOLUME_PROFILE))
    if not 0 < fraction < 1:
        return df, None
    df = df.copy()
    df["Volume"] = df["Volume"].astype(float)  # Yahoo sends whole numbers; pandas refuses a fraction in an int column
    df.iloc[-1, df.columns.get_loc("Volume")] = df["Volume"].iloc[-1] / fraction
    return df, (f"Today's bar is still forming ({now:%H:%M} New York): its volume is scaled x{1 / fraction:.1f} "
                "to a full-day estimate before comparing with past days")


def indicators(df: pd.DataFrame) -> dict:
    """Compute simple technical indicators from daily closing prices (the sma_rsi strategy)."""
    close = df["Close"]
    # RSI (14): compares average gains to average losses over 14 days; <30 = oversold, >70 = overbought
    delta = close.diff()  # day-over-day price change
    gain = delta.clip(lower=0).rolling(14).mean()  # average of up days
    loss = (-delta.clip(upper=0)).rolling(14).mean()  # average of down days (as positive numbers)
    rsi = 100 - 100 / (1 + gain / loss)
    return {
        "last_close": float(close.iloc[-1]),
        # SMA = simple moving average: the mean closing price over the last N days (smooths out noise)
        "sma20": float(close.rolling(20).mean().iloc[-1]),  # short-term trend
        "sma50": float(close.rolling(50).mean().iloc[-1]),  # longer-term trend
        "rsi14": float(rsi.iloc[-1]),
    }
