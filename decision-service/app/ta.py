"""Technical-analysis building blocks for the strategies in strategies.py.

Inputs are daily bars (a DataFrame with Open/High/Low/Close/Volume, oldest first) or one of their columns.
A value at bar t only uses bars up to t, so nothing here can peek into the future.
Conventions follow what charting platforms show (TradingView, most brokers): Wilder's smoothing for
RSI / ATR / ADX, population standard deviation for Bollinger Bands, MACD 12/26/9.
"""

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    """Simple moving average: the plain mean of the last n values."""
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    """Exponential moving average: recent values weigh more, so it turns faster than an SMA."""
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (an EMA with alpha 1/n), used by RSI, ATR and ADX."""
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Relative Strength Index, 0-100: average gain vs average loss. <30 oversold, >70 overbought."""
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), n)
    loss = _wilder(-delta.clip(upper=0), n)
    out = 100 - 100 / (1 + gain / loss)  # loss 0 -> gain/loss = inf -> RSI 100
    return out.mask((gain == 0) & (loss == 0), 50.0)  # a perfectly flat stretch is neutral, not undefined


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line (fast EMA - slow EMA), its signal line (EMA of the line) and the histogram (line - signal).
    Line above signal = momentum building; histogram growing = accelerating."""
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Middle band (SMA n) and the bands k standard deviations above and below it."""
    mid = sma(close, n)
    sd = close.rolling(n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def true_range(df: pd.DataFrame) -> pd.Series:
    """The day's full move including any gap from yesterday's close."""
    prev = df["Close"].shift()
    return pd.concat([df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average True Range: how much the stock typically moves in a day, in dollars."""
    return _wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Average Directional Index (trend STRENGTH, 0-100, direction-blind) plus +DI / -DI (which side is winning).
    Rule of thumb: ADX < 20 no trend (ranging), > 25 a real trend."""
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = _wilder(true_range(df), n)
    plus_di = 100 * _wilder(plus_dm, n) / tr
    minus_di = 100 * _wilder(minus_dm, n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return _wilder(dx, n), plus_di, minus_di


def swing_lows(low: pd.Series, k: int = 2) -> list[int]:
    """Positions of pivot lows: lower than the k bars before and not above the k bars after.
    The last k bars can't qualify yet, because the bars that would confirm them haven't happened."""
    v = low.to_numpy(dtype=float)
    return [i for i in range(k, len(v) - k) if v[i] < v[i - k:i].min() and v[i] <= v[i + 1:i + k + 1].min()]


def swing_highs(high: pd.Series, k: int = 2) -> list[int]:
    """Positions of pivot highs (the mirror image of swing_lows)."""
    v = high.to_numpy(dtype=float)
    return [i for i in range(k, len(v) - k) if v[i] > v[i - k:i].max() and v[i] >= v[i + 1:i + k + 1].max()]


def visits(mask: pd.Series, gap: int = 3) -> int:
    """How many separate times a condition was true: runs closer than `gap` bars apart count as one visit.
    Used to count how often price came back to a support or resistance zone."""
    count, last = 0, None
    for i, hit in enumerate(mask.to_numpy(dtype=bool)):
        if hit:
            if last is None or i - last > gap:
                count += 1
            last = i
    return count


# --- Candlestick patterns (one bar, or the last two). o/h/l/c = open/high/low/close. ---

def is_hammer(o: float, h: float, l: float, c: float) -> bool:
    """Hammer / bullish pin bar: a long lower wick (sellers pushed down, buyers pushed it back), small top."""
    rng = h - l
    if rng <= 0:
        return False
    body, lower, upper = abs(c - o), min(o, c) - l, h - max(o, c)
    return lower >= 2 * body and lower >= 0.5 * rng and upper <= 0.25 * rng


def is_shooting_star(o: float, h: float, l: float, c: float) -> bool:
    """Shooting star / bearish pin bar: a long upper wick (buyers pushed up, sellers slammed it back)."""
    rng = h - l
    if rng <= 0:
        return False
    body, lower, upper = abs(c - o), min(o, c) - l, h - max(o, c)
    return upper >= 2 * body and upper >= 0.5 * rng and lower <= 0.25 * rng


def is_bullish_engulfing(po: float, pc: float, o: float, c: float) -> bool:
    """A down day followed by an up day whose body swallows it."""
    return pc < po and c > o and o <= pc and c >= po


def is_bearish_engulfing(po: float, pc: float, o: float, c: float) -> bool:
    """An up day followed by a down day whose body swallows it."""
    return pc > po and c < o and o >= pc and c <= po


def last(s: pd.Series, back: int = 0) -> float:
    """Value `back` bars before the latest one, as a plain float (NaN if not computable yet)."""
    v = s.iloc[-1 - back]
    return float(v) if pd.notna(v) else float("nan")


def rel_volume(df: pd.DataFrame, n: int = 20) -> float:
    """Today's volume as a multiple of the average of the n days before it (1.5 = 50% busier than usual)."""
    avg = df["Volume"].iloc[-n - 1:-1].mean()
    return float(df["Volume"].iloc[-1] / avg) if avg and np.isfinite(avg) else float("nan")
