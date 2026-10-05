"""Index futures rebuilt from the ETFs that track the same index: NQ / MNQ from QQQ, ES / MES from SPY, YM / MYM
from DIA.

This app's market data (Alpaca) has no futures. A future and its ETF follow the same index, so during New York hours
they move almost exactly together: NQ is about 41x QQQ, ES about 10x SPY, YM about 100x DIA. A future's bars are
rebuilt from its ETF's: each day's bars times the PREVIOUS day's closing ratio of the future to the ETF (Yahoo's daily
closes, so nothing from the day itself leaks in), rounded to the future's tick. What this can't show: the overnight
session (Globex trades almost 24 hours; here there is only the ETF's pre-market from 4:00) and the future's own small
premium over the index. Keep the table in sync with backtest-service/app/futures.py (which adds the contract costs)."""

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import yfinance as yf


@dataclass(frozen=True)
class Future:
    root: str
    name: str
    etf: str  # whose bars it is rebuilt from
    yahoo: str  # Yahoo's continuous front-month contract, for the price level
    multiplier: float  # dollars per point
    tick: float  # the smallest price step


FUTURES = {f.root: f for f in (
    Future("NQ", "E-mini Nasdaq-100", "QQQ", "NQ=F", 20, 0.25),
    Future("MNQ", "Micro E-mini Nasdaq-100", "QQQ", "NQ=F", 2, 0.25),
    Future("ES", "E-mini S&P 500", "SPY", "ES=F", 50, 0.25),
    Future("MES", "Micro E-mini S&P 500", "SPY", "ES=F", 5, 0.25),
    Future("YM", "E-mini Dow", "DIA", "YM=F", 5, 1.0),
    Future("MYM", "Micro E-mini Dow", "DIA", "YM=F", 0.5, 1.0),
)}


def closing_ratios(f: Future, first: date, last: date) -> pd.Series:
    """The future's daily close divided by the ETF's, per trading day (plain dates), from Yahoo."""
    df = yf.download([f.yahoo, f.etf], start=first - timedelta(days=10), end=last + timedelta(days=1), progress=False,
                     auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"no daily prices for {f.yahoo} / {f.etf} from Yahoo")
    close = df["Close"]
    ratio = (close[f.yahoo] / close[f.etf]).dropna()
    if ratio.empty:
        raise ValueError(f"no daily prices for {f.yahoo} / {f.etf} from Yahoo")
    ratio.index = [d.date() for d in ratio.index]
    return ratio


def rebuild(f: Future, etf_bars: pd.DataFrame, ratios: pd.Series | None = None) -> pd.DataFrame:
    """The future's bars from its ETF's: each day x the previous trading day's closing ratio (the first day, with no
    earlier ratio, uses its own), on the future's tick. `ratios` is for tests; normally it comes from Yahoo."""
    if etf_bars.empty:
        return etf_bars
    days = sorted(set(etf_bars.index.date))
    if ratios is None:
        ratios = closing_ratios(f, days[0], days[-1])
    known = sorted(ratios.items())
    scale = {}
    for d in days:
        before = [r for day, r in known if day < d]
        scale[d] = before[-1] if before else known[0][1]
    factor = pd.Series([scale[d] for d in etf_bars.index.date], index=etf_bars.index)
    out = etf_bars.copy()
    for col in ("Open", "High", "Low", "Close"):
        out[col] = ((etf_bars[col] * factor) / f.tick).round() * f.tick
    return out
