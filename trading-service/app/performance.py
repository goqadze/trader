"""A bot's equity history with the money added and taken out set apart: the bot page's gain chart and its worst
drawdowns.

Each day's equity point (EquitySnapshot) is in dollars, so Add money makes it jump up and a withdrawal makes it fall,
though neither is a gain or a loss: the money only changes pockets. Two ways take them out:

- The gain in dollars: equity - what was put in and not taken out by then. Adds and withdrawals don't move it; its last
  point is the Money history's result. Buy & hold's gain is the same with its value, which buys and sells its basket
  with each move (trader.add_capital, withdraw.take_out).
- The return in %, each day's gain against what the bot had that day, chained (a "time-weighted" return). A drawdown is
  a fall of this line from a high: how much the bot's rules lost, whatever money came or went on the way. In dollars it
  would look smaller after a withdrawal and bigger after an add.

A move is counted in the first point measured after it (the point's updated_at), so an add made after the day's last
update is in the next day's point, as is the money. The money of a day's move was in the bot for part of that day only:
its return counts it for half of it (the "simple Dietz" rule)."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .market import ny_date
from .models import Bot, EquitySnapshot, MoneyMove
from .schemas import DrawdownOut, DrawdownsOut, SnapshotOut

TOP = 5  # the drawdowns the bot page lists


def points(session: Session, bot: Bot) -> list[SnapshotOut]:
    """The bot's equity points, the earliest first, each with what was put in by then and the gains over it."""
    snaps = list(session.scalars(select(EquitySnapshot).where(EquitySnapshot.bot_id == bot.id).order_by(EquitySnapshot.day)))
    moves = sorted(session.execute(select(MoneyMove.at, MoneyMove.amount).where(MoneyMove.bot_id == bot.id)).all())
    out, put_in, i = [], bot.allocated_cash, 0
    for s in snaps:
        while i < len(moves) and moves[i].at <= s.updated_at:
            put_in += moves[i].amount
            i += 1
        # Days from before buy & hold kept its value: no money had been added yet, so the starting capital alone
        bench = s.benchmark if s.benchmark is not None else (
            round(bot.allocated_cash * s.price / bot.benchmark_price, 2) if bot.benchmark_price else None)
        out.append(SnapshotOut(day=s.day, equity=s.equity, cash=s.cash, shares=s.shares, price=s.price, benchmark=bench,
                               put_in=round(put_in, 2), gain=round(s.equity - put_in, 2),
                               benchmark_gain=None if bench is None else round(bench - put_in, 2)))
    return out


def _falls(line: list[tuple[date, float, float]]) -> list[DrawdownOut]:
    """Every fall of `line` (day, level, gain in dollars) from a high, until it's back at that high or the line ends."""
    out: list[DrawdownOut] = []
    peak, low = line[0], None

    def fall(back: date | None) -> DrawdownOut:
        return DrawdownOut(peak_day=peak[0], bottom_day=low[0], recovered_day=back,
                           depth_pct=round((low[1] / peak[1] - 1) * 100, 2), loss=round(low[2] - peak[2], 2),
                           days=((back or line[-1][0]) - peak[0]).days)

    for p in line[1:]:
        if p[1] >= peak[1]:
            if low is not None:
                out.append(fall(p[0]))
                low = None
            peak = p
        elif low is None or p[1] < low[1]:
            low = p
    if low is not None:
        out.append(fall(None))
    return [d for d in out if d.depth_pct < 0]  # a fall too small to show as -0.01%


def drawdowns(bot: Bot, pts: list[SnapshotOut]) -> DrawdownsOut:
    """The bot's worst falls from a high on its return line (module docstring), and buy & hold's deepest over the same
    days. The line starts at the bot's start, so a bot that lost from its first day has fallen from what it started with."""
    start = ny_date(bot.created_at)
    line, level, value, put_in = [(start, 1.0, 0.0)], 1.0, bot.allocated_cash, bot.allocated_cash
    for p in pts:
        moved = p.put_in - put_in
        base = value + moved / 2
        if base > 0:  # never 0 in practice: a withdrawal leaves money in the bot
            level *= 1 + (p.equity - value - moved) / base
        line.append((p.day, level, p.gain))
        value, put_in = p.equity, p.put_in
    falls = _falls(line)
    high = max(x[1] for x in line)
    held = None
    if bot.benchmark_price and pts:
        # Buy & hold's units change only with the money moved, so its return is its price's
        held = min([d.depth_pct for d in _falls([(start, bot.benchmark_price, 0.0)] + [(p.day, p.price, 0.0) for p in pts])],
                   default=0.0)
    return DrawdownsOut(worst=sorted(falls, key=lambda d: d.depth_pct)[:TOP], now_pct=round((level / high - 1) * 100, 2),
                        buy_hold_max_pct=held)
