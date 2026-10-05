"""Intraday strategies on 5-minute bars: each finds at most one setup a day and turns it into an order plan (side,
entry, stop, target, until when it may fill). backtest-service replays the plans bar by bar; a live intraday bot
would ask for today's plan as the bars come in.

No look-ahead by construction: a plan is searched for bar by bar, each step seeing only the bars that had
FINISHED by then (`day.iloc[:k + 1]`), and the plan says when it was placed (the close of its last bar), so it can
only fill on later bars. Technical only: no news, no LLM, so years replay in seconds.

Every strategy trades both ways. A short setup is found by running the long rules on the mirrored bars (every
price negated, high and low swapped): one set of rules, no separate short code to drift apart. Prices are rounded to
the instrument's tick: a cent for stocks and ETFs, 0.25 point for NQ / ES (futures.py)."""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd

from .tools import MARKET_TZ

BAR = timedelta(minutes=5)
OPEN = time(9, 30)
EXIT_BY = time(15, 55)  # any open position is closed at this bar's open (5 minutes before the close)


@dataclass
class Plan:
    date: str
    side: str  # long | short
    order: str  # limit | market (market = the next bar's open)
    entry: float | None  # the limit price; None for a market order
    stop: float
    target: float | None  # None: target_r times the risk, measured from the fill
    target_r: float | None
    placed_at: str  # ISO time the setup completed: the order can fill from this moment (the next bar) on
    valid_until: str  # an unfilled order is cancelled then
    exit_by: str  # an open position is closed then
    note: str


@dataclass(frozen=True)
class Context:
    """What a day's rules may know before it starts: the previous session's range, today's pre-market range (4:00 to
    9:30; None without enough pre-market bars), the price step to round to, and optionally a higher timeframe's trend
    (`trend(t)`: +1 up, -1 down, 0 unclear, from its bars finished by t; see trend_at) with its name for the notes."""

    prev_high: float
    prev_low: float
    pre_high: float | None = None
    pre_low: float | None = None
    tick: float = 0.01
    trend: Callable[[datetime], int] | None = None
    trend_name: str = ""

    def mirrored(self) -> "Context":
        trend = self.trend
        return Context(prev_high=-self.prev_low, prev_low=-self.prev_high,
                       pre_high=None if self.pre_low is None else -self.pre_low,
                       pre_low=None if self.pre_high is None else -self.pre_high, tick=self.tick,
                       trend=None if trend is None else (lambda t: -trend(t)), trend_name=self.trend_name)


def _at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=MARKET_TZ)


def _mirror(bars: pd.DataFrame) -> pd.DataFrame:
    """Prices negated with high and low swapped: a falling market becomes a rising one, so the long rules find the
    short setups."""
    return pd.DataFrame({"Open": -bars["Open"], "High": -bars["Low"], "Low": -bars["High"], "Close": -bars["Close"]},
                        index=bars.index)


def _round(v: float, tick: float = 0.01) -> float:
    return round(round(v / tick) * tick + 0.0, 4)


MIN_PREMARKET_BARS = 12  # an hour of pre-market 5-minute bars; with fewer, its range means little


def sessions(bars: pd.DataFrame, tick: float = 0.01, trend: Callable[[datetime], int] | None = None,
             trend_name: str = "") -> list[tuple[date, pd.DataFrame, Context | None]]:
    """Each regular session (9:30 on) with what was known before it: the previous session's range and the
    pre-market's (None for the first day, which has no previous session here). `bars` may include the pre-market."""
    out, prev = [], None
    for day, g in bars.groupby(bars.index.date):
        g = g.sort_index()
        rth, pre = g[g.index.time >= OPEN], g[g.index.time < OPEN]
        if rth.empty:
            continue
        ctx = None
        if prev is not None:
            enough = len(pre) >= MIN_PREMARKET_BARS
            ctx = Context(float(prev["High"].max()), float(prev["Low"].min()),
                          float(pre["High"].max()) if enough else None, float(pre["Low"].min()) if enough else None, tick,
                          trend, trend_name)
        out.append((day, rth, ctx))
        prev = rth
    return out


# --- Opening range breakout ------------------------------------------------------------------------------------
# The published 5-minute ORB (Zarattini & Aziz, 2023): trade in the direction of the first 5-minute candle from the
# next bar's open, stop at that candle's other end, target 10x the risk, otherwise out at the end of the day.

ORB_TARGET_R = 10.0


def orb(day: date, bars: pd.DataFrame, ctx: Context | None, sides: str = "both", entry: str = "market") -> Plan | None:
    """`entry` is ignored: ORB always enters at market."""
    if bars.empty or bars.index[0].time() != OPEN:
        return None  # no 9:30 bar (missing data): no opening range
    first = bars.iloc[0]
    body = float(first["Close"] - first["Open"])
    if abs(body) < 1e-9:
        return None  # a doji: no direction
    side = "long" if body > 0 else "short"
    if side == "short" and sides == "long":
        return None
    stop = float(first["Low"] if side == "long" else first["High"])
    placed = _at(day, OPEN) + BAR
    return Plan(
        date=day.isoformat(), side=side, order="market", entry=None, stop=_round(stop, ctx.tick if ctx else 0.01),
        target=None, target_r=ORB_TARGET_R,
        placed_at=placed.isoformat(), valid_until=(placed + BAR).isoformat(), exit_by=_at(day, EXIT_BY).isoformat(),
        note=(f"First 5-minute candle closed {'up' if side == 'long' else 'down'} ({first['Open']:.2f} -> {first['Close']:.2f}): "
              f"{'buy' if side == 'long' else 'short'} at 9:35, stop at its {'low' if side == 'long' else 'high'} {stop:.2f}, "
              f"target {ORB_TARGET_R:.0f}x the risk, else out at 15:55"),
    )


# --- ICT: liquidity sweep -> market structure shift -> fair value gap ------------------------------------------
# In the New York morning window: price sweeps a pool of stops (yesterday's low, or the 9:30-10:00 low), a strong
# candle then closes above the last swing high and leaves a bullish fair value gap in the discount half of the move;
# buy back into the gap's middle with the stop under the sweep and the nearest buy-side liquidity that pays at least
# 2x the risk as the target. Shorts mirror it. The method's own vocabulary, made into exact rules; whether it has an
# edge is what the backtest is for.

ICT_WINDOW_END = time(11, 0)  # the setup must complete, and the order fill, by then (the "NY AM kill zone")
OPENING_RANGE_END = time(10, 0)  # the 9:30-10:00 range: its low/high is a liquidity pool from 10:00 on
SWEEP_RECLAIM_BARS = 3  # a sweep counts when a close back above the level follows within this many bars
SWING_LOOKBACK_BARS = 6  # the swing high to break: the highest high of the 30 minutes before the sweep
MIN_R = 2.0  # skip setups whose nearest target pays less than 2x the risk
STOP_BUFFER = 0.0002  # stop this far (0.02%) beyond the sweep's extreme, at least a cent


def _sweep_shift_gap(seen: pd.DataFrame, levels: list[tuple[str, float, time | None]], pools: list[tuple[str, float]],
                     tick: float) -> dict | None:
    """The long pattern completing exactly at the last bar of `seen` (its fair value gap's third candle), or None:
    a sweep of one of `levels` (name, price, the time it exists from), a shift through the swing high before it whose
    candle leaves the gap, the gap in the discount half of the move. The target: the nearest of today's high so far
    and `pools` paying at least MIN_R. Works on plain or mirrored prices; returns raw numbers for the caller to turn
    into a Plan."""
    k = len(seen) - 1
    if k < 3:
        return None
    hi, lo, op, cl = (seen[c].to_numpy() for c in ("High", "Low", "Open", "Close"))
    times = [t.time() for t in seen.index]
    # The fair value gap: candle 1's high below candle 3's low, candle 2 the bullish displacement between them
    c1, c2, c3 = k - 2, k - 1, k
    if not (lo[c3] > hi[c1] and cl[c2] > op[c2]):
        return None
    # The most recent sweep before the displacement: a bar trading below a level, then a close back above it
    for s in range(c2 - 1, -1, -1):
        for name, level, known_from in levels:
            if known_from is not None and times[s] < known_from:
                continue  # e.g. the opening range isn't known until 10:00
            if lo[s] >= level:
                continue
            reclaim = next((c for c in range(s, min(s + SWEEP_RECLAIM_BARS, c2 - 1) + 1) if cl[c] > level), None)
            if reclaim is None:
                continue
            swing_high = float(hi[max(0, s - SWING_LOOKBACK_BARS):s].max()) if s > 0 else float(hi[s])
            # The shift: the displacement candle is the FIRST close above that swing high since the reclaim
            if cl[c2] <= swing_high or any(cl[i] > swing_high for i in range(reclaim, c2)):
                continue
            sweep_low = float(lo[s:c2 + 1].min())
            range_high = float(hi[s:k + 1].max())
            gap_low, gap_high = float(hi[c1]), float(lo[c3])
            entry = (gap_low + gap_high) / 2  # the gap's midpoint ("consequent encroachment")
            if entry > (sweep_low + range_high) / 2:
                continue  # the gap sits in premium: not a discount entry
            stop = sweep_low - max(tick, abs(entry) * STOP_BUFFER)
            risk = entry - stop
            targets = [("today's high", float(hi[:k + 1].max()))] + pools
            targets = sorted((p for p in targets if p[1] - entry >= MIN_R * risk), key=lambda p: p[1])
            if not targets:
                return None  # no target worth the risk: skip the day's setup
            target_name, target = targets[0]
            return {"entry": entry, "stop": stop, "target": target, "level_name": name, "level": level,
                    "swept_at": seen.index[s], "shift_at": seen.index[c2], "swing_high": swing_high, "gap": (gap_low, gap_high),
                    "target_name": target_name, "r": (target - entry) / risk}
    return None


def _ict_long(day: date, seen: pd.DataFrame, ctx: Context) -> dict | None:
    """Sweep of yesterday's low or the 9:30-10:00 low; target the nearest buy-side liquidity: today's high so far,
    yesterday's high or the 9:30-10:00 high."""
    times = [t.time() for t in seen.index]
    or_bars = [i for i, t in enumerate(times) if t < OPENING_RANGE_END]
    or_low = float(seen["Low"].iloc[or_bars].min()) if or_bars and times[-1] >= OPENING_RANGE_END else None
    levels = [("yesterday's low", ctx.prev_low, None)] + ([("the 9:30-10:00 low", or_low, OPENING_RANGE_END)] if or_low is not None else [])
    pools = [("yesterday's high", ctx.prev_high)] + ([("the 9:30-10:00 high", float(seen["High"].iloc[or_bars].max()))] if or_bars else [])
    return _sweep_shift_gap(seen, levels, pools, ctx.tick)


def _flip(text: str, side: str) -> str:
    """A long setup's words for the short side: low and high swap."""
    return text.replace("low", "\0").replace("high", "low").replace("\0", "high") if side == "short" else text


def _first_setup(day: date, bars: pd.DataFrame, ctx: Context | None, sides: str, find_long, describe,
                 entry_mode: str = "limit") -> Plan | None:
    """The first setup of the day (long or short) that completes in the morning window. `entry_mode` "limit": an
    order at the gap's middle, valid until 11:00 (it fills only if the price comes back); "market": in at the next
    bar's open, right after the setup completed (it always fills, at a worse price). Same setup, stop and target
    either way. `find_long(day, seen, ctx)` finds a long one in prices as given (shorts: the mirrored prices);
    `describe(found, side, sign, entry, stop, target, gap, placed, entry_mode)` writes the plan's note."""
    if ctx is None or bars.empty:
        return None
    mirrored, ctx_mirrored = _mirror(bars), ctx.mirrored()
    for k in range(len(bars)):
        placed = bars.index[k] + BAR  # the setup's last candle has closed
        if placed.time() >= ICT_WINDOW_END:
            return None  # completing at 11:00 would leave no time to fill
        for side in ("long", "short") if sides == "both" else ("long",):
            seen = (bars if side == "long" else mirrored).iloc[:k + 1]
            side_ctx = ctx if side == "long" else ctx_mirrored
            found = find_long(day, seen, side_ctx)
            if not found:
                continue
            if side_ctx.trend is not None and side_ctx.trend(placed) <= 0:
                continue  # against the higher timeframe's trend (or no clear trend): not this side
            sign = 1 if side == "long" else -1
            entry, stop, target = sign * found["entry"], sign * found["stop"], sign * found["target"]
            gap = sorted(sign * g for g in found["gap"])
            market = entry_mode == "market"
            return Plan(
                date=day.isoformat(), side=side, order="market" if market else "limit",
                entry=None if market else _round(entry, ctx.tick), stop=_round(stop, ctx.tick),
                target=_round(target, ctx.tick), target_r=None, placed_at=placed.isoformat(),
                valid_until=(placed + BAR if market else _at(day, ICT_WINDOW_END)).isoformat(),
                exit_by=_at(day, EXIT_BY).isoformat(),
                note=describe(found, side, sign, entry, stop, target, gap, placed, entry_mode)
                + (f". With the {ctx.trend_name} trend ({'up' if side == 'long' else 'down'})" if ctx.trend else ""),
            )
    return None


def _order_words(found: dict, side: str, sign: int, entry: float, stop: float, target: float, gap: list[float],
                 placed: datetime, entry_mode: str) -> str:
    verb = "buy" if side == "long" else "short"
    order = (f"{verb} at market at {placed:%H:%M} (the gap's middle {entry:.2f} would pay {found['r']:.1f}R)"
             if entry_mode == "market" else f"{verb} limit {entry:.2f}")
    return (f"shift through {sign * found['swing_high']:.2f} at {found['shift_at']:%H:%M}, FVG {gap[0]:.2f}-{gap[1]:.2f}: "
            f"{order}, stop {stop:.2f}, target {_flip(found['target_name'], side)} {target:.2f}"
            + ("" if entry_mode == "market" else f" ({found['r']:.1f}R), valid until 11:00"))


def ict(day: date, bars: pd.DataFrame, ctx: Context | None, sides: str = "both", entry: str = "limit") -> Plan | None:
    """The first sweep -> shift -> FVG setup of the day (long or short) that completes in the window."""
    def describe(found, side, sign, entry_price, stop, target, gap, placed, entry_mode):
        return (f"Swept {_flip(found['level_name'], side)} {sign * found['level']:.2f} at {found['swept_at']:%H:%M}, "
                + _order_words(found, side, sign, entry_price, stop, target, gap, placed, entry_mode))

    return _first_setup(day, bars, ctx, sides, _ict_long, describe, entry)


# --- ICT Power of 3: accumulation -> manipulation -> distribution ------------------------------------------------
# The day's three phases, as exact rules. Accumulation: the pre-market range (4:00-9:30). Manipulation (the "Judas
# swing"): in the first hour the price runs the pre-market low's stops against the day's direction and closes back
# inside within 3 bars. Distribution: a close above the swing high before the sweep (the shift), with a fair value
# gap anywhere in that move (from the reclaim to 3 bars after the shift) sitting in its discount half; buy back into
# the gap's middle, stop under the manipulation's low, target the other side of the accumulation (the pre-market
# high) or yesterday's high, whichever is nearer and pays at least 2x the risk. Premium/discount decides the side:
# only longs when the day opens in the discount (lower) half of yesterday's range, only shorts in the premium half.
# Looser than ict_sweep_fvg on one point, which there must be the shift candle's own gap: on MNQ that rule left about
# 5 setups a year, this one about 15.

AMD_SWEEP_BY = time(10, 30)  # the manipulation happens in the first hour
AMD_GAP_AFTER_SHIFT = 3  # a gap's middle candle may come up to this many bars after the shift


def _amd_long(day: date, seen: pd.DataFrame, ctx: Context) -> dict | None:
    """A long Power of 3 setup known at the last bar of `seen` (the driver asks bar by bar, so the first bar it's
    found at is when it completed), or None."""
    if ctx.pre_low is None or ctx.pre_high is None:
        return None  # no accumulation range to work with
    if float(seen["Open"].iloc[0]) >= (ctx.prev_high + ctx.prev_low) / 2:
        return None  # opened in the premium half of yesterday's range: no long today
    k = len(seen) - 1
    hi, lo, op, cl = (seen[c].to_numpy() for c in ("High", "Low", "Open", "Close"))
    times = [t.time() for t in seen.index]
    # Manipulation: the first run below the pre-market low in the first hour that closes back above it within 3 bars
    for s in (i for i in range(k + 1) if times[i] < AMD_SWEEP_BY and lo[i] < ctx.pre_low):
        reclaim = next((c for c in range(s, min(s + SWEEP_RECLAIM_BARS, k) + 1) if cl[c] > ctx.pre_low), None)
        if reclaim is not None:
            break
    else:
        return None
    swing_high = float(hi[max(0, s - SWING_LOOKBACK_BARS):s].max()) if s > 0 else float(hi[s])
    shift = next((i for i in range(reclaim, k + 1) if cl[i] > swing_high), None)
    if shift is None:
        return None
    # Distribution: the first bullish gap in the move (candle i bullish, candle i+1's low above candle i-1's high)
    gap_at = next((i for i in range(max(reclaim, 1), min(shift + AMD_GAP_AFTER_SHIFT, k - 1) + 1)
                   if lo[i + 1] > hi[i - 1] and cl[i] > op[i]), None)
    if gap_at is None:
        return None
    sweep_low, top = float(lo[s:k + 1].min()), float(hi[s:k + 1].max())
    gap_low, gap_high = float(hi[gap_at - 1]), float(lo[gap_at + 1])
    entry = (gap_low + gap_high) / 2
    if entry > (sweep_low + top) / 2:
        return None  # the gap sits in premium: not a discount entry
    stop = sweep_low - max(ctx.tick, abs(entry) * STOP_BUFFER)
    risk = entry - stop
    pools = sorted((p for p in [("the pre-market high", ctx.pre_high), ("yesterday's high", ctx.prev_high)]
                    if p[1] - entry >= MIN_R * risk), key=lambda p: p[1])
    if not pools:
        return None
    target_name, target = pools[0]
    return {"entry": entry, "stop": stop, "target": target, "level_name": "the pre-market low", "level": ctx.pre_low,
            "swept_at": seen.index[s], "shift_at": seen.index[shift], "swing_high": swing_high, "gap": (gap_low, gap_high),
            "target_name": target_name, "r": (target - entry) / risk}


def ict_amd(day: date, bars: pd.DataFrame, ctx: Context | None, sides: str = "both", entry: str = "limit") -> Plan | None:
    """The day's Power of 3 setup, if the first hour shows one."""
    def describe(found, side, sign, entry_price, stop, target, gap, placed, entry_mode):
        equilibrium = (ctx.prev_high + ctx.prev_low) / 2
        return (f"Accumulation: pre-market {ctx.pre_low:.2f}-{ctx.pre_high:.2f}; opened {float(bars['Open'].iloc[0]):.2f}, in the "
                f"{'discount' if side == 'long' else 'premium'} half of yesterday's range (middle {equilibrium:.2f}). "
                f"Manipulation: swept {_flip(found['level_name'], side)} at {found['swept_at']:%H:%M}. Distribution: "
                + _order_words(found, side, sign, entry_price, stop, target, gap, placed, entry_mode))

    return _first_setup(day, bars, ctx, sides, _amd_long, describe, entry)


INTRADAY_STRATEGIES = {"orb": orb, "ict_sweep_fvg": ict, "ict_amd": ict_amd}


# --- Higher-timeframe trend filter (the ICT strategies, optional) --------------------------------------------------
# An ICT trader reads the bigger picture before taking a 5-minute entry. As an exact rule: trade only in the direction
# of a higher timeframe's trend, where up = its last FINISHED bar closed above the average of its last 20 closes
# (longs only), down = below (shorts only). Regular-hours bars: 1h (9:30-10:30, ..., 15:30-16:00), 4h (9:30-13:30,
# 13:30-16:00), 1d (the session). It filters setups out, so it can only mean fewer trades.

HTF_MINUTES = {"1h": 60, "4h": 240, "1d": None}
HTF_NAMES = {"1h": "1-hour", "4h": "4-hour", "1d": "daily"}
HTF_AVERAGE = 20  # bars in the trend's average
HTF_HISTORY_DAYS = 45  # calendar days of bars before a backtest's start, so its first days have 20 daily bars


def htf_closes(rth: pd.DataFrame, htf: str) -> tuple[list[pd.Timestamp], list[float]]:
    """Each higher-timeframe bar's end time and close, built from regular-hours 5-minute bars (indexed by start)."""
    ends, closes = [], []
    for day, g in rth.groupby(rth.index.date):
        g = g.sort_index()
        session_end = g.index[-1] + BAR
        if HTF_MINUTES[htf] is None:
            ends.append(session_end)
            closes.append(float(g["Close"].iloc[-1]))
            continue
        open_, size = pd.Timestamp(_at(day, OPEN)), pd.Timedelta(minutes=HTF_MINUTES[htf])
        for b, part in g.groupby((g.index - open_) // size):
            ends.append(min(open_ + (int(b) + 1) * size, session_end))
            closes.append(float(part["Close"].iloc[-1]))
    return ends, closes


def trend_at(ends: list[pd.Timestamp], closes: list[float]) -> Callable[[datetime], int]:
    """trend(t): +1 / -1 / 0 from the higher-timeframe bars that had finished by t (0 with fewer than 20)."""
    index = pd.DatetimeIndex(ends)

    def trend(t: datetime) -> int:
        n = int(index.searchsorted(pd.Timestamp(t), side="right"))
        if n < HTF_AVERAGE:
            return 0
        window = closes[n - HTF_AVERAGE:n]
        average = sum(window) / HTF_AVERAGE
        return 1 if window[-1] > average else -1 if window[-1] < average else 0

    return trend


def plans(bars: pd.DataFrame, strategy: str, first: date, last: date, sides: str = "both", tick: float = 0.01,
          entry: str = "limit", htf: str = "off") -> list[dict]:
    """Every day's plan in [first, last] (days without a setup are left out). `bars` should start a few days before
    `first`, so the first day has a previous session (HTF_HISTORY_DAYS with a trend filter), and include the
    pre-market for ict_amd. `entry`: the ICT strategies' order, "limit" at the gap's middle or "market" right after
    the setup (ORB is always market). `htf`: the ICT strategies' trend filter, "off", "1h", "4h" or "1d"."""
    find = INTRADAY_STRATEGIES[strategy]
    trend = None
    if htf != "off":
        rth = bars[bars.index.time >= OPEN]
        trend = trend_at(*htf_closes(rth, htf))
    out = []
    for day, g, ctx in sessions(bars, tick, trend, HTF_NAMES.get(htf, "")):
        if first <= day <= last and (plan := find(day, g, ctx, sides, entry)):
            out.append(asdict(plan))
    return out
