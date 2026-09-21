from datetime import date, timedelta

import pandas as pd
import yfinance as yf


def get_prices(symbol: str, as_of: date, lookback_days: int = 120) -> pd.DataFrame:
    """Daily prices up to and including as_of. Never returns later data (no look-ahead)."""
    # Go back ~120 calendar days so we have at least 50 trading days for the SMA50
    start = as_of - timedelta(days=lookback_days)
    end = as_of + timedelta(days=1)  # yfinance end is exclusive
    # Download daily OHLCV bars from Yahoo Finance (free, no API key); auto_adjust corrects for splits/dividends
    df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=True)
    # Newer yfinance returns two-level column names; flatten them to plain "Close", "Open", ...
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    # Safety filter: drop anything dated after as_of so backtests can't see the future
    return df[df.index.date <= as_of]


def indicators(df: pd.DataFrame) -> dict:
    """Compute simple technical indicators from daily closing prices."""
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
