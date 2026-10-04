"""The decision strategies: each one turns daily price bars (plus the news RAG's read) into BUY / SELL / HOLD.

The ten new ones were picked from four strategy round-ups (Evest, DataDrivenInvestor, XBTFX, DBS) for what
this app can actually run: US stocks, daily bars, one symbol per bot, a long-only cash account, a decision
every `rebalance_days` trading days, and a stop-loss / take-profit enforced by the bot or the backtest.
Left out on purpose: intraday methods (scalping, HFT, ICT/SMC, session breakouts, liquidity sweeps), anything
that needs two symbols or short selling (pairs trading, arbitrage), trendlines (too subjective to automate
reliably; support/resistance and breakouts cover the idea), and styles or categories that aren't rules on
their own (swing/position/day trading, algo/quant/copy/robo trading, options, crypto, forex).

Each strategy is two pure functions, so every rule can be tested without the network:
    analyze(df)          daily bars -> a dict of facts (indicator values, patterns), shown in the steps trail
    decide(facts, news)  facts + news -> Verdict(action, confidence, rule)

One confidence scale for all strategies, so a bot's min_confidence means the same thing whichever it runs:
    0.60        a textbook setup (equal to the default min_confidence, so it trades)
    +0.05/0.10  per extra confirmation (volume, candle pattern, trend alignment); technical max 0.85
    +/-0.15     news RAG agrees / disagrees (applied in agent.decide; not for news_catalyst, where news IS the signal)
    0.40        HOLD
SELL means "exit the long": the bot and the backtest only act on it while holding shares.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import ta
from .tools import indicators as sma_rsi_indicators

DEFAULT_STRATEGY = "sma_rsi"


@dataclass(frozen=True)
class NewsView:
    """What the news RAG found, in the form the strategies use."""

    sentiment: str = "unavailable"  # bullish | bearish | neutral | unavailable | off (technical only)
    catalysts: tuple[str, ...] = ()  # fresh (<48h) company-specific events among the retrieved headlines


@dataclass(frozen=True)
class Verdict:
    action: str  # BUY | SELL | HOLD
    confidence: float  # before the news tilt
    rule: str  # which conditions fired, in words (goes into the steps trail)


@dataclass(frozen=True)
class Strategy:
    id: str
    name: str
    style: str  # trend | momentum | breakout | mean reversion | event
    summary: str  # one sentence for the UI
    buy: str  # the entry rule in words
    sell: str  # the exit rule in words
    best_for: str
    min_bars: int  # daily bars needed before the first decision
    analyze: Callable[[pd.DataFrame], dict]
    decide: Callable[[dict, NewsView], Verdict]
    news_tilt: bool = True  # False: news is part of the rule itself, don't count it twice
    # Starting points for the knobs that matter most for this style (backtest before trusting them)
    suggested: dict = field(default_factory=dict)
    sources: tuple[str, ...] = ()


def _hold(rule: str) -> Verdict:
    return Verdict("HOLD", 0.4, f"{rule} -> HOLD")


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _first_catalyst(news: NewsView) -> str:
    """The top catalyst headline, short enough for a one-line rule (the full text is in the news step)."""
    c = news.catalysts[0]
    return c if len(c) <= 120 else c[:117] + "..."


def _candles(df: pd.DataFrame) -> dict:
    """Today's bar and the candlestick patterns the setups use as confirmation."""
    o, h, l, c = (ta.last(df[k]) for k in ("Open", "High", "Low", "Close"))
    po, pc = ta.last(df["Open"], 1), ta.last(df["Close"], 1)
    return {
        "open": o, "high": h, "low": l, "last_close": c, "prev_close": pc,
        "hammer": ta.is_hammer(o, h, l, c),
        "bullish_engulfing": ta.is_bullish_engulfing(po, pc, o, c),
        "shooting_star": ta.is_shooting_star(o, h, l, c),
        "bearish_engulfing": ta.is_bearish_engulfing(po, pc, o, c),
    }


def _bull_pattern(f: dict) -> str:
    return "hammer" if f["hammer"] else "bullish engulfing" if f["bullish_engulfing"] else ""


def _bear_pattern(f: dict) -> str:
    return "shooting star" if f["shooting_star"] else "bearish engulfing" if f["bearish_engulfing"] else ""


# ---------------------------------------------------------------------------------------------------------
# 0. SMA trend + RSI -- the original simple strategy, unchanged
# ---------------------------------------------------------------------------------------------------------

def _sma_rsi(f: dict, news: NewsView) -> Verdict:
    trend_up = f["sma20"] > f["sma50"]  # short-term average above long-term = uptrend
    if trend_up and f["rsi14"] < 70:  # uptrend and not overbought
        action, conf = "BUY", 0.6
    elif not trend_up and f["rsi14"] > 30:  # downtrend and not oversold
        action, conf = "SELL", 0.6
    else:  # mixed signals
        action, conf = "HOLD", 0.4
    return Verdict(action, conf, f"SMA20 {'>' if trend_up else '<='} SMA50, RSI14={f['rsi14']:.0f} -> {action}")


# ---------------------------------------------------------------------------------------------------------
# 1. Trend following: golden cross + ADX (DBS, XBTFX "Turtle"/trend following, Evest, DataDriven MA strategy)
# ---------------------------------------------------------------------------------------------------------

def _trend_analyze(df: pd.DataFrame) -> dict:
    close = df["Close"]
    s200 = ta.sma(close, 200)
    adx, pdi, mdi = ta.adx(df)
    return {
        "last_close": ta.last(close), "sma50": ta.last(ta.sma(close, 50)), "sma200": ta.last(s200),
        "sma200_20d_ago": ta.last(s200, 20), "adx14": ta.last(adx), "plus_di": ta.last(pdi), "minus_di": ta.last(mdi),
    }


def _trend_decide(f: dict, news: NewsView) -> Verdict:
    golden = f["sma50"] > f["sma200"]
    death = f["sma50"] < f["sma200"]
    broken = f["last_close"] < f["sma200"] and f["minus_di"] > f["plus_di"]
    if death or broken:
        why = " and ".join(w for w, on in (("death cross (SMA50 < SMA200)", death),
                                           ("price below SMA200 with sellers in control (-DI > +DI)", broken)) if on)
        return Verdict("SELL", 0.7 if death and broken else 0.6, f"{why} -> SELL")
    if not golden or f["last_close"] <= f["sma50"]:
        return _hold(f"SMA50 {'>' if golden else '<='} SMA200 but price {f['last_close']:.2f} <= SMA50 {f['sma50']:.2f}: trend paused")
    if f["adx14"] < 20 or f["plus_di"] <= f["minus_di"]:
        return _hold(f"golden cross but ADX {f['adx14']:.0f} (need >= 20 with +DI > -DI): no trend strength")
    conf = 0.6 + (0.1 if f["adx14"] >= 25 else 0) + (0.05 if f["sma200"] > f["sma200_20d_ago"] else 0)
    return Verdict("BUY", conf, f"golden cross (SMA50 {f['sma50']:.2f} > SMA200 {f['sma200']:.2f}), price above SMA50, "
                                f"ADX {f['adx14']:.0f} with +DI > -DI{', SMA200 rising' if f['sma200'] > f['sma200_20d_ago'] else ''} -> BUY")


# ---------------------------------------------------------------------------------------------------------
# 2. Momentum: MACD + RSI + 3-month return (DBS, XBTFX, Evest)
# ---------------------------------------------------------------------------------------------------------

def _momentum_analyze(df: pd.DataFrame) -> dict:
    close = df["Close"]
    line, sig, hist = ta.macd(close)
    return {
        "last_close": ta.last(close), "sma50": ta.last(ta.sma(close, 50)),
        "return_3m": ta.last(close) / ta.last(close, 63) - 1,
        "macd": ta.last(line), "macd_signal": ta.last(sig), "macd_hist": ta.last(hist), "macd_hist_prev": ta.last(hist, 1),
        "rsi14": ta.last(ta.rsi(close)), "rel_volume": ta.rel_volume(df),
    }


def _momentum_decide(f: dict, news: NewsView) -> Verdict:
    faded = f["macd"] < f["macd_signal"] and f["rsi14"] < 50
    broken = f["last_close"] < f["sma50"] and f["macd"] < 0
    if faded or broken:
        why = " and ".join(w for w, on in (("MACD below its signal with RSI < 50 (momentum faded)", faded),
                                           ("price below SMA50 with MACD < 0", broken)) if on)
        return Verdict("SELL", 0.7 if faded and broken else 0.6, f"{why} -> SELL")
    if f["return_3m"] <= 0 or f["last_close"] <= f["sma50"]:
        return _hold(f"3-month return {_pct(f['return_3m'])}, price vs SMA50 {f['last_close']:.2f}/{f['sma50']:.2f}: no uptrend to ride")
    if not (f["macd"] > f["macd_signal"] and f["macd_hist"] > f["macd_hist_prev"]):
        return _hold("MACD not above its signal and accelerating: momentum not building")
    if f["rsi14"] >= 70:
        return _hold(f"RSI {f['rsi14']:.0f} >= 70: overbought, don't chase")
    if f["rsi14"] < 50:
        return _hold(f"RSI {f['rsi14']:.0f} < 50: not strong enough yet")
    conf = 0.6 + (0.1 if f["macd"] > 0 else 0) + (0.1 if f["rel_volume"] >= 1.5 else 0)
    return Verdict("BUY", conf, f"3-month return {_pct(f['return_3m'])}, MACD above signal and rising"
                                f"{' above zero' if f['macd'] > 0 else ''}, RSI {f['rsi14']:.0f} in 50-70, "
                                f"volume {f['rel_volume']:.1f}x -> BUY")


# ---------------------------------------------------------------------------------------------------------
# 3. Breakout: close above the 20-day high on heavy volume, Turtle 10-day-low exit (DBS, DataDriven, Evest, XBTFX)
# ---------------------------------------------------------------------------------------------------------

def _breakout_analyze(df: pd.DataFrame) -> dict:
    prior = df.iloc[:-1]  # the level to break is set by the days BEFORE today
    high20, low20 = float(prior["High"].iloc[-20:].max()), float(prior["Low"].iloc[-20:].min())
    c, h, l = ta.last(df["Close"]), ta.last(df["High"]), ta.last(df["Low"])
    return {
        "last_close": c, "high_20d": high20, "low_10d": float(prior["Low"].iloc[-10:].min()),
        "base_width_atr": (high20 - low20) / ta.last(ta.atr(prior)),  # 20-day range in "typical days"
        "close_in_range": (c - l) / (h - l) if h > l else 1.0,  # where today closed: 0 = at the low, 1 = at the high
        "rel_volume": ta.rel_volume(df), "sma50": ta.last(ta.sma(df["Close"], 50)),
    }


def _breakout_decide(f: dict, news: NewsView) -> Verdict:
    if f["last_close"] < f["low_10d"]:
        return Verdict("SELL", 0.6, f"close {f['last_close']:.2f} < 10-day low {f['low_10d']:.2f} (Turtle exit) -> SELL")
    if f["last_close"] <= f["high_20d"]:
        return _hold(f"close {f['last_close']:.2f} inside the 20-day range (high {f['high_20d']:.2f}): no breakout")
    if f["rel_volume"] < 1.5:
        return _hold(f"close above the 20-day high but volume only {f['rel_volume']:.1f}x average (need 1.5x): likely a false breakout")
    if f["close_in_range"] < 0.5:
        return _hold("broke the 20-day high but closed in the lower half of the day's range: rejected")
    tight = f["base_width_atr"] <= 4  # the 20 days before spanned <= 4 normal days' moves: a coiled consolidation
    conf = 0.6 + (0.1 if tight else 0) + (0.05 if f["last_close"] > f["sma50"] else 0)
    base = f", tight base ({f['base_width_atr']:.1f} ATR wide)" if tight else ""
    return Verdict("BUY", conf, f"close {f['last_close']:.2f} > 20-day high {f['high_20d']:.2f} on {f['rel_volume']:.1f}x volume"
                                f"{base}{', above SMA50' if f['last_close'] > f['sma50'] else ''} -> BUY")


# ---------------------------------------------------------------------------------------------------------
# 4. Mean reversion: Bollinger Bands + RSI (DataDriven "indicator overload", XBTFX mean reversion, DBS)
# ---------------------------------------------------------------------------------------------------------

def _meanrev_analyze(df: pd.DataFrame) -> dict:
    close = df["Close"]
    mid, upper, lower = ta.bollinger(close)
    r = ta.rsi(close)
    adx, pdi, mdi = ta.adx(df)
    return {
        **_candles(df),
        "bb_mid": ta.last(mid), "bb_upper": ta.last(upper), "bb_lower": ta.last(lower),
        "pierced_lower_band_3d": bool((df["Low"].iloc[-3:] < lower.iloc[-3:]).any()),
        "min_rsi_3d": float(r.iloc[-3:].min()), "rsi14": ta.last(r),
        "sma200": ta.last(ta.sma(close, 200)),  # NaN with < 200 bars: then just no trend bonus
        "adx14": ta.last(adx), "plus_di": ta.last(pdi), "minus_di": ta.last(mdi),
    }


def _meanrev_decide(f: dict, news: NewsView) -> Verdict:
    if f["last_close"] >= f["bb_mid"]:
        stretched = f["last_close"] >= f["bb_upper"] or f["rsi14"] > 70
        return Verdict("SELL", 0.7 if stretched else 0.6,
                       f"close {f['last_close']:.2f} back at/above the mean (middle band {f['bb_mid']:.2f})"
                       f"{', stretched to the upper band / RSI > 70' if stretched else ''}: reversion done -> SELL")
    setup = f["pierced_lower_band_3d"] and f["min_rsi_3d"] < 30
    if not setup:
        return _hold(f"no oversold setup (need a dip below the lower band {f['bb_lower']:.2f} with RSI < 30 in the last 3 days; "
                     f"min RSI {f['min_rsi_3d']:.0f})")
    if not (f["last_close"] > f["bb_lower"] and f["last_close"] > f["open"]):
        return _hold("oversold, waiting for a bullish candle that closes back inside the bands")
    if f["adx14"] >= 30 and f["minus_di"] > f["plus_di"]:
        return _hold(f"oversold, but ADX {f['adx14']:.0f} in a strong downtrend: a falling knife, not a dip")
    uptrend = f["last_close"] > f["sma200"]
    pattern = _bull_pattern(f)
    conf = 0.6 + (0.1 if uptrend else 0) + (0.05 if pattern else 0)
    return Verdict("BUY", conf, f"dipped below the lower band with RSI {f['min_rsi_3d']:.0f} < 30, closed back inside on an up candle"
                                f"{f' ({pattern})' if pattern else ''}{', long-term uptrend (above SMA200)' if uptrend else ''} -> BUY")


# ---------------------------------------------------------------------------------------------------------
# 5. Range trading: buy support, sell resistance, only in a sideways market (DBS, Evest, DataDriven S/R)
# ---------------------------------------------------------------------------------------------------------

def _range_analyze(df: pd.DataFrame) -> dict:
    win = df.iloc[-41:-1]  # the 40 days before today define the range
    support, resistance = float(win["Low"].min()), float(win["High"].max())
    zone = 0.2 * (resistance - support)  # "near" a boundary = within 20% of the range's height
    c = ta.last(df["Close"])
    adx, _, _ = ta.adx(df)
    return {
        **_candles(df),
        "support": support, "resistance": resistance, "range_height_pct": resistance / support - 1,
        "support_visits": ta.visits(win["Low"] <= support + zone), "resistance_visits": ta.visits(win["High"] >= resistance - zone),
        "range_position": (c - support) / (resistance - support) if resistance > support else 0.5,
        "adx14": ta.last(adx), "rsi14": ta.last(ta.rsi(df["Close"])),
    }


def _range_decide(f: dict, news: NewsView) -> Verdict:
    is_range = (f["adx14"] < 20 and f["support_visits"] >= 2 and f["resistance_visits"] >= 2
                and 0.04 <= f["range_height_pct"] <= 0.30)
    if f["last_close"] < f["support"]:
        return Verdict("SELL", 0.7 if is_range else 0.6, f"close {f['last_close']:.2f} broke below support {f['support']:.2f}: the range failed -> SELL")
    if not is_range:
        return _hold(f"no tradable range (ADX {f['adx14']:.0f} needs < 20; support tested {f['support_visits']}x, "
                     f"resistance {f['resistance_visits']}x, need 2x each; height {f['range_height_pct'] * 100:.0f}%)")
    where = f"range {f['support']:.2f}-{f['resistance']:.2f}, price at {f['range_position'] * 100:.0f}% of it"
    if f["range_position"] >= 0.75:
        pattern = _bear_pattern(f)
        return Verdict("SELL", 0.7 if pattern else 0.6, f"{where}: near resistance{f' ({pattern})' if pattern else ''} -> SELL")
    if f["range_position"] <= 0.25:
        if f["last_close"] <= f["open"]:
            return _hold(f"{where}: near support, waiting for an up candle to confirm the bounce")
        pattern = _bull_pattern(f)
        conf = 0.6 + (0.1 if pattern else 0) + (0.05 if f["rsi14"] < 40 else 0)
        return Verdict("BUY", conf, f"{where}: bouncing off support{f' ({pattern})' if pattern else ''}, RSI {f['rsi14']:.0f} -> BUY")
    return _hold(f"{where}: middle of the range")


# ---------------------------------------------------------------------------------------------------------
# 6. Swing trading: pullback to the 20 EMA in an uptrend (DataDriven MA strategy, DBS swing trading)
# ---------------------------------------------------------------------------------------------------------

def _pullback_analyze(df: pd.DataFrame) -> dict:
    close = df["Close"]
    e20, e50 = ta.ema(close, 20), ta.ema(close, 50)
    touched = (df["Low"].iloc[-2:] <= e20.iloc[-2:] * 1.005).any()  # today or yesterday dipped to (within 0.5% of) the EMA
    return {
        **_candles(df),
        "ema20": ta.last(e20), "ema50": ta.last(e50), "ema50_10d_ago": ta.last(e50, 10),
        "touched_ema20": bool(touched), "rsi14": ta.last(ta.rsi(close)),
    }


def _pullback_decide(f: dict, news: NewsView) -> Verdict:
    crossed_down = f["ema20"] < f["ema50"]
    below = f["last_close"] < f["ema50"]
    if crossed_down or below:
        why = " and ".join(w for w, on in (("EMA20 crossed below EMA50", crossed_down), ("price closed below EMA50", below)) if on)
        return Verdict("SELL", 0.7 if crossed_down and below else 0.6, f"{why}: the uptrend is broken -> SELL")
    if f["ema50"] <= f["ema50_10d_ago"]:
        return _hold("EMA20 above EMA50 but EMA50 not rising: no clear uptrend")
    if not f["touched_ema20"]:
        return _hold(f"uptrend, waiting for a pullback to EMA20 {f['ema20']:.2f} (price {(f['last_close'] / f['ema20'] - 1) * 100:.1f}% above it)")
    if not (f["last_close"] > f["ema20"] and f["last_close"] >= f["open"]):
        return _hold("pulled back to EMA20, waiting for a bullish candle that closes back above it")
    pattern = _bull_pattern(f)
    healthy = 40 <= f["rsi14"] <= 60
    conf = 0.6 + (0.1 if pattern else 0) + (0.05 if healthy else 0)
    return Verdict("BUY", conf, f"uptrend (EMA20 > rising EMA50), pullback touched EMA20 {f['ema20']:.2f} and was rejected"
                                f"{f' ({pattern})' if pattern else ''}, RSI {f['rsi14']:.0f} -> BUY")


# ---------------------------------------------------------------------------------------------------------
# 7. Reversal: RSI divergence + a reversal candle (DBS reversal/contrarian, Evest reversal)
# ---------------------------------------------------------------------------------------------------------

def _divergence(price: np.ndarray, r: np.ndarray, pivots: list[int], n: int, bullish: bool) -> tuple[bool, str]:
    """Compare the last two pivots of the last 40 bars. Bullish: lower low in price but higher low in RSI,
    with RSI oversold (< 35) at the first low. Bearish: the mirror image at highs (RSI > 65).
    The newer pivot must be at most 5 bars old, so the signal is still fresh."""
    pts = [i for i in pivots if i >= n - 40]
    if len(pts) < 2:
        return False, ""
    a, b = pts[-2], pts[-1]
    if n - 1 - b > 5:
        return False, ""
    if bullish:
        ok = price[b] < price[a] and r[b] > r[a] and r[a] < 35
    else:
        ok = price[b] > price[a] and r[b] < r[a] and r[a] > 65
    return ok, f"price {price[a]:.2f} -> {price[b]:.2f}, RSI {r[a]:.0f} -> {r[b]:.0f}"


def _reversal_analyze(df: pd.DataFrame) -> dict:
    r = ta.rsi(df["Close"]).to_numpy(dtype=float)
    n = len(df)
    bull, bull_detail = _divergence(df["Low"].to_numpy(dtype=float), r, ta.swing_lows(df["Low"]), n, True)
    bear, bear_detail = _divergence(df["High"].to_numpy(dtype=float), r, ta.swing_highs(df["High"]), n, False)
    return {
        **_candles(df), "prev_high": ta.last(df["High"], 1), "rsi14": float(r[-1]),
        "bullish_divergence": bull, "bullish_detail": bull_detail, "bearish_divergence": bear, "bearish_detail": bear_detail,
    }


def _reversal_decide(f: dict, news: NewsView) -> Verdict:
    if f["bearish_divergence"] and f["last_close"] < f["open"]:
        pattern = _bear_pattern(f)
        return Verdict("SELL", 0.7 if pattern else 0.6, f"bearish RSI divergence at the highs ({f['bearish_detail']}) "
                                                         f"and a down candle{f' ({pattern})' if pattern else ''} -> SELL")
    if not f["bullish_divergence"]:
        return _hold("no fresh RSI divergence (lower low in price with a higher low in RSI)")
    if not (f["last_close"] > f["open"] and f["last_close"] > f["prev_close"]):
        return _hold(f"bullish divergence ({f['bullish_detail']}), waiting for an up candle to confirm the turn")
    pattern = _bull_pattern(f)
    strong = f["last_close"] > f["prev_high"]
    conf = 0.6 + (0.1 if pattern else 0) + (0.05 if strong else 0)
    extra = (f" ({pattern})" if pattern else "") + (", closed above yesterday's high" if strong else "")
    return Verdict("BUY", conf, f"bullish RSI divergence ({f['bullish_detail']}) confirmed by an up candle{extra} -> BUY")


# ---------------------------------------------------------------------------------------------------------
# 8. Gap and go: a big opening gap that holds, on heavy volume (DBS gap trading, Evest)
# ---------------------------------------------------------------------------------------------------------

def _gap_analyze(df: pd.DataFrame) -> dict:
    c, h, l = ta.last(df["Close"]), ta.last(df["High"]), ta.last(df["Low"])
    o, pc = ta.last(df["Open"]), ta.last(df["Close"], 1)
    return {
        "last_close": c, "open": o, "high": h, "low": l, "prev_close": pc, "gap_pct": o / pc - 1,
        "atr_pct": ta.last(ta.atr(df.iloc[:-1])) / pc,  # a normal day's move, to judge the gap's size
        "rel_volume": ta.rel_volume(df), "close_in_range": (c - l) / (h - l) if h > l else 1.0,
    }


def _gap_decide(f: dict, news: NewsView) -> Verdict:
    min_gap = max(0.02, 0.5 * f["atr_pct"])  # at least 2% and at least half a normal day's move
    gap = f"gap {_pct(f['gap_pct'])} (need {min_gap * 100:.1f}%)"
    if f["gap_pct"] <= -min_gap:
        if f["last_close"] <= f["open"] and f["high"] < f["prev_close"]:
            return Verdict("SELL", 0.7 if f["rel_volume"] >= 1.5 else 0.6, f"{gap} down and not recovering "
                                                                            f"(below the open, never back to yesterday's close) -> SELL")
        return _hold(f"{gap} down but buyers are filling it")
    if f["gap_pct"] < min_gap:
        return _hold(f"{gap}: no significant gap today")
    if f["low"] <= f["prev_close"]:
        return _hold(f"{gap} up but it filled (touched yesterday's close {f['prev_close']:.2f}): no follow-through")
    if f["last_close"] < f["open"]:
        return _hold(f"{gap} up but fading below the open")
    if f["rel_volume"] < 1.5:
        return _hold(f"{gap} up on only {f['rel_volume']:.1f}x volume (need 1.5x)")
    conf = 0.6 + (0.1 if news.catalysts else 0) + (0.05 if f["close_in_range"] >= 0.75 else 0)
    return Verdict("BUY", conf, f"{gap} up held above the open on {f['rel_volume']:.1f}x volume"
                                f"{f'; catalyst: {_first_catalyst(news)}' if news.catalysts else '; no news catalyst found'} -> BUY")


# ---------------------------------------------------------------------------------------------------------
# 9. News catalyst: trade fresh news when the price confirms it (DBS news-based trading; post-earnings drift)
# ---------------------------------------------------------------------------------------------------------

def _news_analyze(df: pd.DataFrame) -> dict:
    return {
        "last_close": ta.last(df["Close"]), "prev_close": ta.last(df["Close"], 1),
        "day_return": ta.last(df["Close"]) / ta.last(df["Close"], 1) - 1, "rel_volume": ta.rel_volume(df),
    }


def _news_decide(f: dict, news: NewsView) -> Verdict:
    if news.sentiment == "unavailable":
        return _hold("news RAG unavailable (needs OPENAI_API_KEY and a news source key)")
    if news.sentiment not in ("bullish", "bearish"):
        return _hold(f"news {news.sentiment}: nothing to trade")
    bullish = news.sentiment == "bullish"
    if (f["day_return"] > 0) != bullish:
        return _hold(f"news {news.sentiment} but the price is {_pct(f['day_return'])} today: the market disagrees")
    # A plain opinion piece plus a small move stays below the default 0.6; a real event or heavy volume trades
    conf = 0.55 + (0.1 if news.catalysts else 0) + (0.1 if f["rel_volume"] >= 1.5 else 0)
    action = "BUY" if bullish else "SELL"
    return Verdict(action, conf, f"news {news.sentiment} and the price agrees ({_pct(f['day_return'])} today, "
                                 f"{f['rel_volume']:.1f}x volume)"
                                 f"{f'; catalyst: {_first_catalyst(news)}' if news.catalysts else '; no fresh catalyst'} -> {action}")


# ---------------------------------------------------------------------------------------------------------
# 10. Fibonacci retracement: buy the "golden zone" (50-61.8%) pullback of a strong move (DataDriven, Evest)
# ---------------------------------------------------------------------------------------------------------

def _fib_analyze(df: pd.DataFrame) -> dict:
    win = df.iloc[-60:]
    highs, lows = win["High"].to_numpy(dtype=float), win["Low"].to_numpy(dtype=float)
    hi = int(np.argmax(highs))  # the swing high of the last ~3 months
    lo = int(np.argmin(lows[:hi + 1]))  # the low the move started from (before the high)
    H, L = highs[hi], lows[lo]
    level = lambda x: H - x * (H - L)  # noqa: E731  retracement x of the move, measured down from the high
    return {
        **_candles(df), "swing_low": L, "swing_high": H, "impulse_pct": H / L - 1, "bars_since_high": len(win) - 1 - hi,
        "fib_50": level(0.5), "fib_618": level(0.618), "fib_786": level(0.786),
        "lowest_since_high": float(lows[hi + 1:].min()) if hi < len(win) - 1 else H,
        "sma200": ta.last(ta.sma(df["Close"], 200)),
    }


def _fib_decide(f: dict, news: NewsView) -> Verdict:
    if f["impulse_pct"] < 0.08 or f["bars_since_high"] < 3:
        return _hold(f"no clean up-move to retrace (move {_pct(f['impulse_pct'])}, need 8%; high {f['bars_since_high']} bars ago, need 3)")
    zone = f"golden zone {f['fib_618']:.2f}-{f['fib_50']:.2f} of the {f['swing_low']:.2f}->{f['swing_high']:.2f} move"
    if f["last_close"] < f["fib_786"]:
        return Verdict("SELL", 0.6, f"close {f['last_close']:.2f} below the 78.6% level {f['fib_786']:.2f}: the move failed -> SELL")
    if f["lowest_since_high"] < f["fib_786"]:
        return _hold(f"already broke the 78.6% level since the high: setup invalid ({zone})")
    if f["low"] > f["fib_50"]:
        return _hold(f"price {f['last_close']:.2f} hasn't pulled back into the {zone}")
    if not (f["last_close"] >= f["fib_618"] and f["last_close"] > f["open"]):
        return _hold(f"in the {zone}, waiting for a bullish candle that holds above 61.8%")
    pattern = _bull_pattern(f)
    uptrend = f["last_close"] > f["sma200"]
    conf = 0.6 + (0.1 if pattern else 0) + (0.05 if uptrend else 0)
    return Verdict("BUY", conf, f"pulled back into the {zone} and bounced on an up candle"
                                f"{f' ({pattern})' if pattern else ''}{', above SMA200' if uptrend else ''} -> BUY")


# ---------------------------------------------------------------------------------------------------------
# The catalog. Keep ids in sync with trading-service/app/schemas.py, backtest-service/app/models.py and
# frontend/src/strategies.ts.
# ---------------------------------------------------------------------------------------------------------

_DAILY = {"rebalance_days": 1}
STRATEGIES: dict[str, Strategy] = {s.id: s for s in [
    Strategy(
        "sma_rsi", "SMA trend + RSI (simple)", "trend",
        "The original rule: be long while the 20-day average is above the 50-day, unless RSI says overbought.",
        "SMA20 > SMA50 and RSI14 < 70", "SMA20 <= SMA50 and RSI14 > 30", "steady trends; the baseline to beat",
        50, sma_rsi_indicators, _sma_rsi, suggested={"rebalance_days": 5, "stop_pct": 0.04, "target_pct": 0.08}),
    Strategy(
        "trend_following", "Trend following (golden cross + ADX)", "trend",
        "Ride long-term uptrends: SMA50 above SMA200 with ADX confirming a real trend; exit on the death cross.",
        "SMA50 > SMA200, price > SMA50, ADX >= 20 with +DI > -DI", "SMA50 < SMA200, or price < SMA200 with -DI > +DI",
        "strong multi-month trends; the death cross exits, so the take-profit is set far away to let winners run",
        221, _trend_analyze, _trend_decide, suggested={"rebalance_days": 5, "stop_pct": 0.08, "target_pct": 1.0},
        sources=("DBS", "XBTFX", "Evest", "DataDrivenInvestor")),
    Strategy(
        "momentum", "Momentum (MACD + RSI)", "momentum",
        "Buy strength that is still building: positive 3-month return, MACD rising above its signal, RSI 50-70.",
        "3-month return > 0, price > SMA50, MACD > signal and rising, 50 <= RSI < 70",
        "MACD < signal with RSI < 50, or price < SMA50 with MACD < 0", "trending markets and leaders; avoid chasing RSI > 70",
        70, _momentum_analyze, _momentum_decide, suggested={"rebalance_days": 5, "stop_pct": 0.06, "target_pct": 0.15},
        sources=("DBS", "XBTFX", "Evest")),
    Strategy(
        "breakout", "Breakout (20-day high + volume)", "breakout",
        "Buy a close above the 20-day high on 1.5x+ volume; exit on a close below the 10-day low (Turtle rule).",
        "close > prior 20-day high, volume >= 1.5x average, close in the upper half of the day",
        "close < prior 10-day low", "stocks leaving a tight consolidation; check daily so the breakout day isn't missed",
        60, _breakout_analyze, _breakout_decide, suggested={**_DAILY, "stop_pct": 0.05, "target_pct": 0.15},
        sources=("DBS", "DataDrivenInvestor", "Evest", "XBTFX")),
    Strategy(
        "mean_reversion", "Mean reversion (Bollinger + RSI)", "mean reversion",
        "Buy an oversold dip below the lower Bollinger Band once a candle closes back inside; sell back at the mean.",
        "low < lower band with RSI < 30 in the last 3 days, then an up close back inside the bands (not in a strong downtrend)",
        "close >= middle band (the mean)", "range-bound stocks and dips inside long-term uptrends; check daily",
        60, _meanrev_analyze, _meanrev_decide, suggested={**_DAILY, "stop_pct": 0.05, "target_pct": 0.08},
        sources=("DataDrivenInvestor", "XBTFX", "DBS")),
    Strategy(
        "range_trading", "Range trading (support & resistance)", "mean reversion",
        "In a sideways market (ADX < 20), buy bounces at support and sell near resistance; exit if support breaks.",
        "ADX < 20, both edges of the 40-day range tested twice, price in the bottom 25% with an up candle",
        "price in the top 25% of the range, or a close below support", "sideways, choppy stocks",
        60, _range_analyze, _range_decide, suggested={**_DAILY, "stop_pct": 0.04, "target_pct": 0.08},
        sources=("DBS", "Evest", "DataDrivenInvestor")),
    Strategy(
        "ma_pullback", "Swing: pullback to the 20 EMA", "trend",
        "Buy the dip inside an uptrend: EMA20 above a rising EMA50, price pulls back to EMA20 and is rejected upward.",
        "EMA20 > rising EMA50, low touched EMA20 today/yesterday, up close back above it",
        "EMA20 < EMA50 or close < EMA50", "healthy uptrends with regular pullbacks; holds days to weeks",
        70, _pullback_analyze, _pullback_decide, suggested={**_DAILY, "stop_pct": 0.05, "target_pct": 0.12},
        sources=("DataDrivenInvestor", "DBS", "XBTFX")),
    Strategy(
        "reversal", "Reversal (RSI divergence)", "mean reversion",
        "Catch a turn: price makes a lower low but RSI a higher low (selling is exhausting), confirmed by an up candle.",
        "bullish RSI divergence (first low RSI < 35, newest low <= 5 bars ago) and an up close above yesterday's close",
        "bearish RSI divergence at the highs plus a down candle", "oversold stocks after a long decline; contrarian, lower win rate",
        60, _reversal_analyze, _reversal_decide, suggested={**_DAILY, "stop_pct": 0.05, "target_pct": 0.10},
        sources=("DBS", "Evest")),
    Strategy(
        "gap_and_go", "Gap and go", "event",
        "Buy a big opening gap up that holds all day on heavy volume, better still with a news catalyst behind it.",
        "open >= 2% (and >= half an ATR) above yesterday's close, never back to it, close >= open, volume >= 1.5x",
        "a gap down that stays below the open and never recovers", "earnings and news days; check daily, after the open",
        30, _gap_analyze, _gap_decide, suggested={**_DAILY, "decide_at": "open", "stop_pct": 0.03, "target_pct": 0.06},
        sources=("DBS", "Evest")),
    Strategy(
        "news_catalyst", "News catalyst (RAG-driven)", "event",
        "Trade what the news RAG finds when the market agrees: bullish news on an up day buys, bearish on a down day sells.",
        "news bullish and price up today; 0.55 base, +0.10 fresh catalyst (earnings, upgrade, deal...), +0.10 volume >= 1.5x",
        "news bearish and price down today (same scoring)", "event-driven stocks; needs news keys; check daily",
        30, _news_analyze, _news_decide, news_tilt=False,
        suggested={**_DAILY, "decide_at": "both", "stop_pct": 0.04, "target_pct": 0.10}, sources=("DBS", "XBTFX")),
    Strategy(
        "fibonacci", "Fibonacci retracement (golden zone)", "trend",
        "After a strong up-move (8%+), buy the pullback into the 50-61.8% golden zone once a bullish candle holds it.",
        "low reaches the 50% level, close >= 61.8% level on an up candle, 78.6% never broken",
        "close below the 78.6% level", "trending stocks after a sharp run-up",
        60, _fib_analyze, _fib_decide, suggested={**_DAILY, "stop_pct": 0.05, "target_pct": 0.12},
        sources=("DataDrivenInvestor", "Evest")),
]}


def catalog() -> list[dict]:
    """The strategies as plain data, for GET /strategies."""
    return [
        {"id": s.id, "name": s.name, "style": s.style, "summary": s.summary, "buy": s.buy, "sell": s.sell,
         "best_for": s.best_for, "min_bars": s.min_bars, "news": "part of the rule" if not s.news_tilt else "confidence tilt",
         "suggested": s.suggested, "sources": list(s.sources)}
        for s in STRATEGIES.values()
    ]


def format_facts(facts: dict) -> str:
    """Facts as one readable line for the steps trail: prices to 2 decimals, flags as yes/no, empty text skipped."""
    out = []
    for k, v in facts.items():
        if isinstance(v, (bool, np.bool_)):
            out.append(f"{k}={'yes' if v else 'no'}")
        elif isinstance(v, (int, float, np.floating, np.integer)):
            out.append(f"{k}={'n/a' if not np.isfinite(v) else round(float(v), 4 if abs(v) < 1 else 2)}")
        elif v:
            out.append(f"{k}={v}")
    return ", ".join(out)
