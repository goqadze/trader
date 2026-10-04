"""Intraday strategies on 5-minute bars: each finds at most one setup a day and turns it into an order plan (side,
entry, stop, target, until when it may fill). backtest-service replays the plans bar by bar; a live intraday bot
would ask for today's plan as the bars come in.

No look-ahead by construction: a plan is searched for bar by bar, each step seeing only the bars that had
FINISHED by then (`day.iloc[:k + 1]`), and the plan says when it was placed (the close of its last bar), so it can
only fill on later bars. Technical only: no news, no LLM, so years replay in seconds.

Both strategies trade both ways. A short setup is found by running the long rules on the mirrored bars (every
price negated, high and low swapped): one set of rules, no separate short code to drift apart."""

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
    """What a day's rules may know before it starts: the previous session's range."""

    prev_high: float
    prev_low: float

    def mirrored(self) -> "Context":
        return Context(prev_high=-self.prev_low, prev_low=-self.prev_high)


def _at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=MARKET_TZ)


def _mirror(bars: pd.DataFrame) -> pd.DataFrame:
    """Prices negated with high and low swapped: a falling market becomes a rising one, so the long rules find the
    short setups."""
    return pd.DataFrame({"Open": -bars["Open"], "High": -bars["Low"], "Low": -bars["High"], "Close": -bars["Close"]},
                        index=bars.index)


def _cents(v: float) -> float:
    return round(v + 0.0, 2)


def sessions(bars: pd.DataFrame) -> list[tuple[date, pd.DataFrame, Context | None]]:
    """Each regular session with the previous one's range (None for the first, which has no previous day here)."""
    out, prev = [], None
    for day, g in bars.groupby(bars.index.date):
        g = g.sort_index()
        out.append((day, g, Context(float(prev["High"].max()), float(prev["Low"].min())) if prev is not None else None))
        prev = g
    return out


# --- Opening range breakout ------------------------------------------------------------------------------------
# The published 5-minute ORB (Zarattini & Aziz, 2023): trade in the direction of the first 5-minute candle from the
# next bar's open, stop at that candle's other end, target 10x the risk, otherwise out at the end of the day.

ORB_TARGET_R = 10.0


def orb(day: date, bars: pd.DataFrame, ctx: Context | None, sides: str = "both") -> Plan | None:
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
        date=day.isoformat(), side=side, order="market", entry=None, stop=_cents(stop), target=None, target_r=ORB_TARGET_R,
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


def _ict_long(day: date, seen: pd.DataFrame, ctx: Context) -> dict | None:
    """A long setup that completes exactly at the last bar of `seen` (its fair value gap's third candle), or None.
    Works on plain or mirrored prices; returns raw numbers for the caller to turn into a Plan."""
    k = len(seen) - 1
    if k < 3:
        return None
    hi, lo, op, cl = (seen[c].to_numpy() for c in ("High", "Low", "Open", "Close"))
    times = [t.time() for t in seen.index]
    # The fair value gap: candle 1's high below candle 3's low, candle 2 the bullish displacement between them
    c1, c2, c3 = k - 2, k - 1, k
    if not (lo[c3] > hi[c1] and cl[c2] > op[c2]):
        return None
    or_bars = [i for i, t in enumerate(times) if t < OPENING_RANGE_END]
    or_low = float(lo[or_bars].min()) if or_bars and times[k] >= OPENING_RANGE_END else None
    levels = [("yesterday's low", ctx.prev_low)] + ([("the 9:30-10:00 low", or_low)] if or_low is not None else [])
    # The most recent sweep before the displacement: a bar trading below a level, then a close back above it
    for s in range(c2 - 1, -1, -1):
        for name, level in levels:
            if name == "the 9:30-10:00 low" and times[s] < OPENING_RANGE_END:
                continue  # the opening range isn't known until 10:00
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
            stop = sweep_low - max(0.01, abs(entry) * STOP_BUFFER)
            risk = entry - stop
            # The nearest buy-side liquidity paying at least MIN_R: today's high so far, the opening range high, yesterday's high
            pools = [("today's high", float(hi[:k + 1].max())), ("yesterday's high", ctx.prev_high)]
            if or_bars:
                pools.append(("the 9:30-10:00 high", float(hi[or_bars].max())))
            pools = sorted((p for p in pools if p[1] - entry >= MIN_R * risk), key=lambda p: p[1])
            if not pools:
                return None  # no target worth the risk: skip the day's setup
            target_name, target = pools[0]
            return {"entry": entry, "stop": stop, "target": target, "level_name": name, "level": level,
                    "swept_at": seen.index[s], "shift_at": seen.index[c2], "swing_high": swing_high, "gap": (gap_low, gap_high),
                    "target_name": target_name, "r": (target - entry) / risk}
    return None


def ict(day: date, bars: pd.DataFrame, ctx: Context | None, sides: str = "both") -> Plan | None:
    """The first setup of the day (long or short) that completes in the window."""
    if ctx is None or bars.empty:
        return None
    mirrored = _mirror(bars)
    for k in range(len(bars)):
        placed = bars.index[k] + BAR  # the setup's last candle has closed
        if placed.time() >= ICT_WINDOW_END:
            return None  # completing at 11:00 would leave no time to fill
        for side in ("long", "short") if sides == "both" else ("long",):
            seen = (bars if side == "long" else mirrored).iloc[:k + 1]
            found = _ict_long(day, seen, ctx if side == "long" else ctx.mirrored())
            if not found:
                continue
            sign = 1 if side == "long" else -1
            entry, stop, target = sign * found["entry"], sign * found["stop"], sign * found["target"]
            gap = sorted(sign * g for g in found["gap"])
            name = found["level_name"].replace("low", "high") if side == "short" else found["level_name"]
            return Plan(
                date=day.isoformat(), side=side, order="limit", entry=_cents(entry), stop=_cents(stop), target=_cents(target),
                target_r=None, placed_at=placed.isoformat(), valid_until=_at(day, ICT_WINDOW_END).isoformat(),
                exit_by=_at(day, EXIT_BY).isoformat(),
                note=(f"Swept {name} {sign * found['level']:.2f} at {found['swept_at']:%H:%M}, shift through "
                      f"{sign * found['swing_high']:.2f} at {found['shift_at']:%H:%M}, FVG {gap[0]:.2f}-{gap[1]:.2f}: "
                      f"{'buy' if side == 'long' else 'short'} limit {entry:.2f}, stop {stop:.2f}, target "
                      f"{found['target_name'].replace('high', 'low') if side == 'short' else found['target_name']} "
                      f"{target:.2f} ({found['r']:.1f}R), valid until 11:00"),
            )
    return None


INTRADAY_STRATEGIES = {"orb": orb, "ict_sweep_fvg": ict}


def plans(bars: pd.DataFrame, strategy: str, first: date, last: date, sides: str = "both") -> list[dict]:
    """Every day's plan in [first, last] (days without a setup are left out). `bars` should start a few days before
    `first`, so the first day has a previous session."""
    find = INTRADAY_STRATEGIES[strategy]
    out = []
    for day, g, ctx in sessions(bars):
        if first <= day <= last and (plan := find(day, g, ctx, sides)):
            out.append(asdict(plan))
    return out
