"""NYSE calendar helpers: which days are trading days, when does the market open/close, and when
should bots decide. Uses exchange_calendars, which knows holidays and early closes (e.g. 13:00 on
the day after Thanksgiving) -- a plain "Mon-Fri 9:30-16:00" rule gets those wrong."""

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

NY = ZoneInfo("America/New_York")


@lru_cache(maxsize=1)
def _cal():
    return xcals.get_calendar("XNYS")  # loading takes ~1s, so do it once


def ny_date(now: datetime) -> date:
    """The calendar date in New York -- a trading day is a New York date, not a UTC date."""
    return now.astimezone(NY).date()


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """(open, close) in UTC for a trading day, or None on weekends/holidays."""
    cal = _cal()
    if not cal.is_session(day.isoformat()):
        return None
    return (
        cal.session_open(day.isoformat()).to_pydatetime(),
        cal.session_close(day.isoformat()).to_pydatetime(),
    )


def is_open(now: datetime) -> bool:
    bounds = session_bounds(ny_date(now))
    return bounds is not None and bounds[0] <= now < bounds[1]


def decision_time(day: date, minutes_before_close: int) -> datetime | None:
    """When bots decide on a trading day: N minutes before that day's (possibly early) close."""
    bounds = session_bounds(day)
    return None if bounds is None else bounds[1] - timedelta(minutes=minutes_before_close)


# A trading day has up to two decision slots, in time order. Each bot picks one or both (Bot.decide_at).
SLOTS = ("open", "close")
OPEN_SLOT_LENGTH = timedelta(minutes=30)


def slot_window(day: date, slot: str, minutes_after_open: int, minutes_before_close: int) -> tuple[datetime, datetime] | None:
    """[start, end) in UTC of a decision slot on a trading day, or None on weekends/holidays.

    open:  starts N min after the open and lasts 30 min (10:00-10:30 New York on a normal day). Skipping
           the first minutes avoids the opening auction's jumpy prices and wide spreads.
    close: starts N min before the close and lasts until the close (15:30-16:00; 12:30-13:00 on half days).
    If the service is down for a slot's whole window, that slot is skipped (never made up later)."""
    bounds = session_bounds(day)
    if bounds is None:
        return None
    open_, close = bounds
    if slot == "open":
        start = open_ + timedelta(minutes=minutes_after_open)
        return start, min(start + OPEN_SLOT_LENGTH, close)
    if slot == "close":
        return close - timedelta(minutes=minutes_before_close), close
    raise ValueError(f"unknown decision slot {slot!r}")


def sessions_since(last: date | None, today: date) -> int:
    """Trading days after `last` up to and including `today`. Used for "decide every N trading days"."""
    if last is None:
        return 10**6  # never decided: always due
    if last >= today:
        return 0
    return len(_cal().sessions_in_range((last + timedelta(days=1)).isoformat(), today.isoformat()))


def month_end_after(day: date) -> date:
    """The first trading day after `day` that is the last trading day of its month (rotation bots rebalance then)."""
    sessions = _cal().sessions_in_range((day + timedelta(days=1)).isoformat(), (day + timedelta(days=70)).isoformat())
    for s, nxt in zip(sessions, sessions[1:]):
        if s.month != nxt.month:
            return s.date()
    raise RuntimeError(f"no month end found after {day}")


def next_decision_time(now: datetime, minutes_before_close: int) -> datetime:
    """The next scheduled decision moment at or after `now` (for the dashboard's "next decision" label)."""
    day = ny_date(now)
    for _ in range(15):  # at most ~2 weeks of holidays in a row
        at = decision_time(day, minutes_before_close)
        if at is not None and at >= now:
            return at
        day += timedelta(days=1)
    raise RuntimeError("no trading session found in the next 15 days")


def to_utc(ts) -> datetime:
    """pandas / naive timestamps -> aware UTC datetime."""
    ts = pd.Timestamp(ts)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.to_pydatetime().astimezone(timezone.utc)
