"""Redraw the charts in frontend/public/guides/trading-strategies.html from the REAL strategy code.

Each chart is made-up prices shaped into one setup, run bar by bar through decision-service's strategies with
the bot's rule (BUY when flat, SELL when holding, confidence >= 0.6; stop/target left out), so every marker sits
where the actual rules fire. Re-run after changing a strategy:

    make guide-charts

It runs inside the decision-service container (it imports app.ta / app.strategies) and rewrites every
<!--chart:ID-->...<!--/chart--> block of the guide in place. Rebuild the frontend afterwards.
"""
import re
import sys

import numpy as np
import pandas as pd

from app import ta
from app.strategies import STRATEGIES, NewsView

W, H = 300, 150
L, R, T = 4, 42, 10  # right padding leaves room for line labels


def bars_from(closes, vol=1e6, spread=0.004, opens=None, highs=None, lows=None, vols=None):
    closes = np.asarray(closes, float)
    o = np.r_[closes[0], closes[:-1]] if opens is None else np.asarray(opens, float)
    h = np.maximum(o, closes) * (1 + spread) if highs is None else np.asarray(highs, float)
    lo = np.minimum(o, closes) * (1 - spread) if lows is None else np.asarray(lows, float)
    v = np.full(len(closes), vol) if vols is None else np.asarray(vols, float)
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": closes, "Volume": v},
                        index=pd.bdate_range("2024-01-01", periods=len(closes)))


def walk(segments, seed, noise=0.8, start=100.0):
    rng = np.random.default_rng(seed)
    p = [start]
    for n, pct in segments:
        step = (1 + pct) ** (1 / n)
        for _ in range(n):
            p.append(p[-1] * step * (1 + rng.normal(0, noise / 100)))
    return np.array(p)


def simulate(sid, df, news_at=None):
    """Entries/exits the bot would make with this strategy (no stop/target, min confidence 0.6)."""
    s = STRATEGIES[sid]
    marks, holding = [], False
    for i in range(s.min_bars, len(df)):
        news = (news_at or {}).get(i, NewsView("neutral"))
        v = s.decide(s.analyze(df.iloc[:i + 1]), news)
        if v.confidence < 0.6:
            continue
        if v.action == "BUY" and not holding:
            marks.append((i, "BUY")); holding = True
        elif v.action == "SELL" and holding:
            marks.append((i, "SELL")); holding = False
    return marks


class Chart:
    def __init__(self, df, start, main_bottom=None, strip=None):
        self.df, self.start = df, start
        self.n = len(df) - start
        self.bottom = main_bottom or (104 if strip else H - 8)
        self.parts, self.vals = [], []
        self.strip = strip  # (top, bottom) of the lower panel

    def x(self, i):
        return L + (i - self.start) * (W - L - R) / (self.n - 1)

    def track(self, *series):
        for s in series:
            self.vals.extend(np.asarray(s, float)[self.start:])

    def finalize_scale(self):
        v = np.array([x for x in self.vals if np.isfinite(x)])
        lo, hi = v.min(), v.max()
        pad = (hi - lo) * 0.08
        self.lo, self.hi = lo - pad, hi + pad

    def y(self, p):
        return T + (self.hi - p) * (self.bottom - T) / (self.hi - self.lo)

    def line(self, s, cls, label=None, dash=False):
        s = np.asarray(s, float)
        pts = [f"{self.x(i):.1f},{self.y(s[i]):.1f}" for i in range(self.start, len(s)) if np.isfinite(s[i])]
        self.parts.append(f'<polyline class="{cls}" points="{" ".join(pts)}"{" stroke-dasharray=\"3 3\"" if dash else ""}/>')
        if label:
            last = next(i for i in range(len(s) - 1, -1, -1) if np.isfinite(s[i]))
            self.parts.append(f'<text class="lbl {cls}-t" x="{self.x(len(s) - 1) + 3:.1f}" y="{self.y(s[last]) + 3:.1f}">{label}</text>')

    def band(self, upper, lower, cls="band"):
        u, lo = np.asarray(upper, float), np.asarray(lower, float)
        idx = [i for i in range(self.start, len(u)) if np.isfinite(u[i]) and np.isfinite(lo[i])]
        pts = [f"{self.x(i):.1f},{self.y(u[i]):.1f}" for i in idx] + [f"{self.x(i):.1f},{self.y(lo[i]):.1f}" for i in reversed(idx)]
        self.parts.append(f'<polygon class="{cls}" points="{" ".join(pts)}"/>')

    def level(self, p, cls, label, x0=None, dy=3):
        x0 = self.x(x0) if x0 is not None else L
        self.parts.append(f'<line class="{cls}" x1="{x0:.1f}" x2="{W - R:.1f}" y1="{self.y(p):.1f}" y2="{self.y(p):.1f}"/>')
        self.parts.append(f'<text class="lbl" x="{W - R + 3:.1f}" y="{self.y(p) + dy:.1f}">{label}</text>')

    def zone(self, p_lo, p_hi, cls, x0=None):
        x0 = self.x(x0) if x0 is not None else L
        self.parts.append(f'<rect class="{cls}" x="{x0:.1f}" y="{self.y(p_hi):.1f}" width="{W - R - x0:.1f}" height="{self.y(p_lo) - self.y(p_hi):.1f}"/>')

    def candles(self):
        d = self.df
        w = max(2.0, (W - L - R) / self.n * 0.6)
        for i in range(self.start, len(d)):
            o, h, l, c = d["Open"].iloc[i], d["High"].iloc[i], d["Low"].iloc[i], d["Close"].iloc[i]
            cls = "up" if c >= o else "down"
            x = self.x(i)
            self.parts.append(f'<line class="wick {cls}" x1="{x:.1f}" x2="{x:.1f}" y1="{self.y(h):.1f}" y2="{self.y(l):.1f}"/>')
            top, bot = self.y(max(o, c)), self.y(min(o, c))
            self.parts.append(f'<rect class="body {cls}" x="{x - w / 2:.1f}" y="{top:.1f}" width="{w:.1f}" height="{max(bot - top, 1):.1f}"/>')

    def marks(self, marks, price=None):
        price = self.df["Close"].to_numpy() if price is None else price
        for i, kind in marks:
            if i < self.start:
                continue
            x, y = self.x(i), self.y(price[i])
            if kind == "BUY":
                self.parts.append(f'<path class="m-buy" d="M{x:.1f},{y + 5:.1f} l-5,9 h10 z"/>'
                                  f'<text class="mk buy-t" x="{x:.1f}" y="{y + 23:.1f}" text-anchor="middle">BUY</text>')
            else:
                self.parts.append(f'<path class="m-sell" d="M{x:.1f},{y - 5:.1f} l-5,-9 h10 z"/>'
                                  f'<text class="mk sell-t" x="{x:.1f}" y="{y - 17:.1f}" text-anchor="middle">SELL</text>')

    def note(self, i, p, text, anchor="middle", dy=0):
        self.parts.append(f'<text class="lbl note-t" x="{self.x(i):.1f}" y="{self.y(p) + dy:.1f}" text-anchor="{anchor}">{text}</text>')

    def flag(self, i, text, good=True, top=True):
        x = self.x(i)
        cls = "buy" if good else "sell"
        y = T + 6 if top else self.bottom - 2
        self.parts.append(f'<line class="flagpole" x1="{x:.1f}" x2="{x:.1f}" y1="{T:.1f}" y2="{self.bottom:.1f}"/>'
                          f'<text class="lbl {cls}-t" x="{x + 3:.1f}" y="{y:.1f}">{text}</text>')

    def strip_bars(self, values, highlight=(), signed=False, label=None):
        top, bot = self.strip
        v = np.asarray(values, float)
        shown = v[self.start:]
        m = np.nanmax(np.abs(shown)) or 1
        w = max(1.5, (W - L - R) / self.n * 0.7)
        zero = (top + bot) / 2 if signed else bot
        span = (bot - top) / 2 if signed else bot - top
        for i in range(self.start, len(v)):
            if not np.isfinite(v[i]):
                continue
            hgt = abs(v[i]) / m * span
            y = zero - hgt if v[i] >= 0 else zero
            cls = ("hl" if i in highlight else ("pos" if v[i] >= 0 else "neg") if signed else "vol")
            self.parts.append(f'<rect class="sb {cls}" x="{self.x(i) - w / 2:.1f}" y="{y:.1f}" width="{w:.1f}" height="{max(hgt, 0.6):.1f}"/>')
        if label:
            self.parts.append(f'<text class="lbl" x="{W - R + 3:.1f}" y="{(top + bot) / 2 + 3:.1f}">{label}</text>')

    def strip_line(self, values, cls, lo, hi, label=None, guides=()):
        top, bot = self.strip
        yy = lambda p: top + (hi - p) * (bot - top) / (hi - lo)  # noqa: E731
        for g in guides:
            self.parts.append(f'<line class="guide" x1="{L}" x2="{W - R}" y1="{yy(g):.1f}" y2="{yy(g):.1f}"/>'
                              f'<text class="lbl" x="{W - R + 3:.1f}" y="{yy(g) + 3:.1f}">{g}</text>')
        v = np.asarray(values, float)
        pts = [f"{self.x(i):.1f},{yy(v[i]):.1f}" for i in range(self.start, len(v)) if np.isfinite(v[i])]
        self.parts.append(f'<polyline class="{cls}" points="{" ".join(pts)}"/>')
        if label:
            self.parts.append(f'<text class="lbl {cls}-t" x="{W - R + 3:.1f}" y="{top + 6:.1f}">{label}</text>')
        return yy

    def svg(self, desc):
        sep = f'<line class="sep" x1="{L}" x2="{W - R}" y1="{self.strip[0] - 3}" y2="{self.strip[0] - 3}"/>' if self.strip else ""
        return (f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{desc}">'
                f'<title>{desc}</title>{sep}{"".join(self.parts)}</svg>')


out, report = {}, {}


def done(sid, chart, desc, marks):
    out[sid] = chart.svg(desc)
    report[sid] = [(int(i), k) for i, k in marks]


# ---- 0. sma_rsi -------------------------------------------------------------------------------------------
df = bars_from(walk([(60, -0.10), (70, 0.22), (60, -0.14)], seed=11, noise=0.9))
c = df["Close"]; s20, s50 = ta.sma(c, 20), ta.sma(c, 50)
m = simulate("sma_rsi", df)
ch = Chart(df, 55); ch.track(c, s20, s50); ch.finalize_scale()
ch.line(c, "price"); ch.line(s20, "l1", "SMA20"); ch.line(s50, "l2", "SMA50"); ch.marks(m)
done("sma_rsi", ch, "Price with SMA20 and SMA50: buy when SMA20 rises above SMA50, sell when it falls back below", m)

# ---- 1. trend following -----------------------------------------------------------------------------------
df = bars_from(walk([(240, -0.22), (170, 0.55), (130, -0.25)], seed=5, noise=1.0))
c = df["Close"]; s50, s200 = ta.sma(c, 50), ta.sma(c, 200)
m = simulate("trend_following", df)
ch = Chart(df, 225); ch.track(c, s50, s200); ch.finalize_scale()
ch.line(c, "price"); ch.line(s50, "l1", "SMA50"); ch.line(s200, "l2", "SMA200"); ch.marks(m)
done("trend_following", ch, "Months-long chart: buy after the golden cross once ADX confirms, sell at the death cross", m)

# ---- 2. momentum ------------------------------------------------------------------------------------------
df = bars_from(walk([(80, 0.02), (45, 0.20), (25, 0.04), (40, -0.12)], seed=21, noise=0.8))
c = df["Close"]; s50 = ta.sma(c, 50); _, _, hist = ta.macd(c)
m = simulate("momentum", df)
ch = Chart(df, 75, strip=(112, 144)); ch.track(c, s50); ch.finalize_scale()
ch.line(c, "price"); ch.line(s50, "l2", "SMA50"); ch.marks(m); ch.strip_bars(hist, signed=True, label="MACD")
done("momentum", ch, "Price with SMA50 and the MACD histogram below: buy while momentum builds, sell when it fades", m)

# ---- 3. breakout ------------------------------------------------------------------------------------------
rng = np.random.default_rng(3)
base = list(walk([(70, 0.06)], seed=31, noise=0.7))
lvl = base[-1]
box = [lvl * (1 + 0.012 * np.sin(i / 2.2) + rng.normal(0, 0.003)) for i in range(28)]
up = [box[-1] * 1.045]
for _ in range(14):
    up.append(up[-1] * (1 + 0.006 + rng.normal(0, 0.006)))
down = [up[-1] * (1 - 0.012 * (k + 1)) for k in range(7)]
closes = np.array(base + box + up + down)
vols = np.full(len(closes), 1e6) * rng.uniform(0.8, 1.2, len(closes))
bo = len(base) + len(box)
vols[bo] = 2.8e6
df = bars_from(closes, vols=vols)
c = df["Close"]
hi20 = df["High"].shift(1).rolling(20).max(); lo10 = df["Low"].shift(1).rolling(10).min()
m = simulate("breakout", df)
ch = Chart(df, 60, strip=(114, 144)); ch.track(c, hi20, lo10); ch.finalize_scale()
ch.line(hi20, "l1", "20d high", dash=True); ch.line(lo10, "l3", "10d low", dash=True); ch.line(c, "price"); ch.marks(m)
ch.strip_bars(df["Volume"], highlight={bo}, label="volume")
done("breakout", ch, "A tight range, then a close above the 20-day high on heavy volume; exit on a close below the 10-day low", m)

# ---- 4. mean reversion -----------------------------------------------------------------------------------
p = list(walk([(230, 0.22)], seed=41, noise=1.1))
for d in (-0.028, -0.034, -0.02):
    p.append(p[-1] * (1 + d))
for d in (0.018, 0.012, 0.01, 0.009, 0.008, 0.007, 0.006, 0.004, 0.005, 0.003, 0.004, 0.002):
    p.append(p[-1] * (1 + d))
df = bars_from(p)
c = df["Close"]; mid, upb, lob = ta.bollinger(c)
m = simulate("mean_reversion", df)
ch = Chart(df, len(df) - 60); ch.track(c, upb, lob); ch.finalize_scale()
ch.band(upb, lob); ch.line(mid, "l2", "mean"); ch.line(c, "price"); ch.marks(m)
done("mean_reversion", ch, "Bollinger Bands: buy when an oversold dip closes back inside the lower band, sell at the middle band", m)

# ---- 5. range trading ------------------------------------------------------------------------------------
rng = np.random.default_rng(51)
closes = np.array([100 * (1 + 0.045 * np.sin(i / 2.6) + rng.normal(0, 0.004)) for i in range(130)])
df = bars_from(closes)
m = simulate("range_trading", df)
win = df.iloc[-41:-1]
sup, res = win["Low"].min(), win["High"].max()
ch = Chart(df, len(df) - 70); ch.track(df["Close"], [sup] * len(df), [res] * len(df)); ch.finalize_scale()
q = (res - sup) * 0.25
ch.zone(sup, sup + q, "zone-buy"); ch.zone(res - q, res, "zone-sell")
ch.level(sup, "lvl", "support"); ch.level(res, "lvl", "resist.")
ch.line(df["Close"], "price"); ch.marks(m)
done("range_trading", ch, "A sideways range: buy bounces in the bottom quarter near support, sell in the top quarter near resistance", m)

# ---- 6. pullback to the 20 EMA ----------------------------------------------------------------------------
seg = []
for k in range(9):
    seg += [(7, 0.05), (4, -0.03)]
df = bars_from(walk([(40, 0.0)] + seg, seed=61, noise=0.5))
c = df["Close"]; e20, e50 = ta.ema(c, 20), ta.ema(c, 50)
m = simulate("ma_pullback", df)
ch = Chart(df, 50); ch.track(c, e20, e50); ch.finalize_scale()
ch.line(c, "price"); ch.line(e20, "l1", "EMA20"); ch.line(e50, "l2", "EMA50"); ch.marks(m)
done("ma_pullback", ch, "An uptrend with pullbacks: buy when a dip touches EMA20 and bounces, sell if price closes below EMA50", m)

# ---- 7. reversal (RSI divergence) --------------------------------------------------------------------------
p = list(walk([(60, 0.0)], seed=71, noise=0.5))
for d in (-0.015, -0.02, -0.025, -0.02, -0.03, -0.02, -0.018, -0.015):
    p.append(p[-1] * (1 + d))
for d in (0.015, 0.012, 0.01, 0.008, 0.009, 0.006):
    p.append(p[-1] * (1 + d))
for d in (-0.009, 0.003, -0.011, 0.002, -0.01, -0.007, 0.003, -0.009, -0.008, 0.002, -0.01, -0.006):
    p.append(p[-1] * (1 + d))
for d in (0.006, 0.018, 0.012, 0.01, 0.008, 0.009, 0.004, 0.007):
    p.append(p[-1] * (1 + d))
df = bars_from(p)
c = df["Close"]; r = ta.rsi(c)
m = simulate("reversal", df)
start = len(df) - 50
ch = Chart(df, start, strip=(110, 144)); ch.track(c, df["Low"]); ch.finalize_scale()
ch.line(c, "price"); ch.marks(m)
lows = [i for i in ta.swing_lows(df["Low"]) if i >= start]
yy = ch.strip_line(r, "l1", 0, 100, "RSI", guides=(30,))
if len(lows) >= 2 and m:
    a, b = [i for i in lows if i < m[0][0]][-2:]
    ch.parts.append(f'<line class="div" x1="{ch.x(a):.1f}" y1="{ch.y(df["Low"].iloc[a]):.1f}" x2="{ch.x(b):.1f}" y2="{ch.y(df["Low"].iloc[b]):.1f}"/>')
    ch.parts.append(f'<line class="div" x1="{ch.x(a):.1f}" y1="{yy(r.iloc[a]):.1f}" x2="{ch.x(b):.1f}" y2="{yy(r.iloc[b]):.1f}"/>')
    ch.note(b - 1.5, df["Low"].iloc[b], "lower low", anchor="end", dy=10)
done("reversal", ch, "Price makes a lower low while RSI makes a higher low (divergence), then an up candle confirms the turn", m)

# ---- 8. gap and go -----------------------------------------------------------------------------------------
rng = np.random.default_rng(81)
closes = list(100 * (1 + np.cumsum(rng.normal(0, 0.006, 40))))
opens = [closes[0]] + closes[:-1]
highs = [max(o, c) * 1.004 for o, c in zip(opens, closes)]
lows = [min(o, c) * 0.996 for o, c in zip(opens, closes)]
vols = list(rng.uniform(0.8e6, 1.2e6, 40))
pc = closes[-1]
g_o, g_c = pc * 1.045, pc * 1.062  # gap-up day: opens 4.5% higher, never looks back, closes near the high
opens.append(g_o); closes.append(g_c); highs.append(g_c * 1.003); lows.append(g_o * 0.994); vols.append(3.2e6)
for d in (0.008, -0.004, 0.011, 0.006, -0.003, 0.009):
    o = closes[-1] * (1 + rng.normal(0, 0.002)); cc = closes[-1] * (1 + d)
    opens.append(o); closes.append(cc); highs.append(max(o, cc) * 1.004); lows.append(min(o, cc) * 0.996); vols.append(1.4e6)
df = bars_from(closes, opens=opens, highs=highs, lows=lows, vols=vols)
m = simulate("gap_and_go", df)
gd = 40
ch = Chart(df, len(df) - 22, strip=(114, 144)); ch.track(df["High"], df["Low"]); ch.finalize_scale()
ch.candles(); ch.marks(m, price=df["Low"].to_numpy() * 0.995)
ch.note(gd - 1.3, (df["Close"].iloc[gd - 1] + df["Open"].iloc[gd]) / 2, "gap", anchor="end", dy=-6)
ch.strip_bars(df["Volume"], highlight={gd}, label="volume")
done("gap_and_go", ch, "Daily candles: the stock opens well above yesterday's close, never fills the gap, on heavy volume", m)

# ---- 9. news catalyst --------------------------------------------------------------------------------------
rng = np.random.default_rng(91)
p = list(100 * (1 + np.cumsum(rng.normal(0, 0.005, 40))))
vols = list(rng.uniform(0.8e6, 1.2e6, 40))
up_day = len(p)
p.append(p[-1] * 1.035); vols.append(2.4e6)
for _ in range(14):
    p.append(p[-1] * (1 + 0.004 + rng.normal(0, 0.005))); vols.append(1.1e6)
down_day = len(p)
p.append(p[-1] * 0.965); vols.append(2.2e6)
for _ in range(5):
    p.append(p[-1] * (1 - 0.003 + rng.normal(0, 0.004))); vols.append(1.1e6)
df = bars_from(p, vols=vols)
news = {up_day: NewsView("bullish", ("[x, 3h ago] Company beats earnings estimates",)),
        down_day: NewsView("bearish", ("[x, 2h ago] Regulator opens probe",))}
m = simulate("news_catalyst", df, news)
ch = Chart(df, 30, strip=(114, 144)); ch.track(df["Close"]); ch.finalize_scale()
ch.flag(up_day, "earnings beat"); ch.flag(down_day, "probe", good=False, top=False)
ch.line(df["Close"], "price"); ch.marks(m)
ch.strip_bars(df["Volume"], highlight={up_day, down_day}, label="volume")
done("news_catalyst", ch, "Fresh bullish news on an up day with heavy volume buys; bearish news on a down day sells", m)

# ---- 10. fibonacci -------------------------------------------------------------------------------------------
closes = [100.0 + np.sin(i / 3) * 0.6 for i in range(40)]
for i in range(1, 21):
    closes.append(100 + i * 1.05 + np.sin(i) * 0.4)
for i in range(1, 10):
    closes.append(closes[40 + 19] - i * 1.25)
p0 = closes[-1]
opens = [closes[0]] + closes[:-1]
highs = [max(o, c) + 0.35 for o, c in zip(opens, closes)]
lows = [min(o, c) - 0.35 for o, c in zip(opens, closes)]
# the bounce candle: dips into the zone, closes back up
o, lo, cc = p0 - 0.2, p0 - 1.6, p0 + 0.9
opens.append(o); lows.append(lo); closes.append(cc); highs.append(cc + 0.3)
for d in (0.8, 0.9, 0.7, 1.0, 0.6, 0.9):
    o = closes[-1]; cc = o + d
    opens.append(o); closes.append(cc); highs.append(cc + 0.35); lows.append(o - 0.35)
df = bars_from(closes, opens=opens, highs=highs, lows=lows)
m = simulate("fibonacci", df)
f = STRATEGIES["fibonacci"].analyze(df.iloc[:m[0][0] + 1]) if m else STRATEGIES["fibonacci"].analyze(df)
ch = Chart(df, 32); ch.track(df["Close"], df["Low"], df["High"]); ch.finalize_scale()
x0 = 40
ch.zone(f["fib_618"], f["fib_50"], "zone-gold", x0=x0)
ch.level(f["swing_high"], "lvl", "100%", x0=x0); ch.level(f["fib_50"], "lvl", "50%", x0=x0, dy=-1)
ch.level(f["fib_618"], "lvl", "61.8%", x0=x0, dy=8); ch.level(f["fib_786"], "lvl-sell", "78.6%", x0=x0)
ch.level(f["swing_low"], "lvl", "0%", x0=x0)
ch.line(df["Close"], "price"); ch.marks(m, price=df["Low"].to_numpy())
done("fibonacci", ch, "After a strong move up, buy the bounce inside the 50 to 61.8% golden zone; sell below the 78.6% level", m)

missing = [sid for sid in STRATEGIES if not report.get(sid)]
if missing:
    sys.exit(f"no BUY/SELL marker for {missing}: reshape their synthetic prices so the rules fire")

path = sys.argv[1]
html = open(path).read()
for sid, svg in out.items():
    html, n = re.subn(rf"<!--chart:{sid}-->.*?<!--/chart-->", lambda _: f"<!--chart:{sid}-->{svg}<!--/chart-->", html, flags=re.S)
    if n != 1:
        sys.exit(f"{path}: expected one <!--chart:{sid}--> block, found {n}")
open(path, "w").write(html)
print(f"redrew {len(out)} charts in {path}: " + ", ".join(f"{k} {len(v)}" for k, v in report.items()))
