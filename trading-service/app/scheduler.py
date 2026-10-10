"""The background loop that runs every bot. It wakes every TICK_SECONDS and, for each bot:

  1. resolves orders still pending at the broker          (any time)
     and keeps the stop-loss resting at the broker         (brokers that can hold one: Alpaca)
  2. checks the real account matches the bot's records   (market hours, real brokers only)
  3. refreshes the price, records equity, enforces stops  (market hours, every RISK_CHECK_MINUTES)
  4. makes a decision when one is due                     (active bots, every `rebalance_days` trading days, at the
                                                           slots the bot chose: 30 min after the open and/or 30 min
                                                           before the close, see market.slot_window)
Momentum rotation bots (see rotation.py) take the same steps their own way: no stop-loss to keep at the broker,
prices and the account check every RISK_CHECK_MINUTES, a rebalance at each month's last close, and the orders of a
rebalance still in progress sent on the following ticks. Dip buyers (see dip.py) too: prices and the account every
RISK_CHECK_MINUTES, and a check of every watched symbol at the end of each bar of their interval (paused ones only
check their holdings' exits).

Restart-safe by design: "which slots are already done?" comes from the database (Bot.last_decision_at),
not from memory, so a restart at 15:40 doesn't decide twice. If the service is down for a slot's whole
window, that slot is simply skipped and the next one catches up.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from .brokers import BrokerError, get_broker
from .config import settings
from .db import SessionLocal, utcnow
from .decision_client import get_signal
from .market import NY, is_open, ny_date, session_bounds, sessions_since, slot_window
from . import dip, rotation, splits
from .models import DIP, ROTATION, Bot
from .trader import bot_lock, evaluate, log_event, protect, reconcile, sync_pending, watch

logger = logging.getLogger("trading-service")

# In-memory pacing only (losing these on restart is harmless: worst case one extra price check)
_last_watch: dict[int, datetime] = {}
_retry_after: dict[int, datetime] = {}  # a failed signal call is retried after a pause, not every tick
_last_error: dict[tuple[int, str], datetime] = {}  # don't write the same error to the audit log every 30s
# The heartbeat /health reads: when the loop started, the last tick that finished, and the last tick that failed
state: dict = {"last_tick": None, "running": False, "started_at": None, "last_error": None}

SIGNAL_RETRY = timedelta(minutes=5)
ERROR_LOG_EVERY = timedelta(hours=1)


# Bot.decide_at -> its slots, in time order
BOT_SLOTS = {"close": ("close",), "open": ("open",), "both": ("open", "close")}


def _window(day: date, slot: str) -> tuple[datetime, datetime] | None:
    return slot_window(day, slot, settings.decision_minutes_after_open, settings.decision_minutes_before_close)


def _last_decided(bot: Bot) -> datetime | None:
    """When the bot last made a real decision. Bots from before decision slots existed only stored the
    date; count that whole day as decided."""
    if bot.last_decision_at is not None:
        return bot.last_decision_at
    if bot.last_decision_date is not None:
        return datetime.combine(bot.last_decision_date, time(23, 59), tzinfo=NY)
    return None


def _slot_open(bot: Bot, day: date, slot: str, last: datetime | None) -> bool:
    """Does this slot on this trading day still need a decision? (The caller checks the clock.)

    - The day must be a decision day: `rebalance_days` trading days since the last one, or the same day
      (so a bot on "both" that decided at 10:00 still decides again at 15:30).
    - The slot is done once any decision -- scheduled, or a manual "Run now" -- happened after the
      previous slot's window ended (for the day's first slot: after the open)."""
    last_day = ny_date(last) if last else None
    if last_day != day and sessions_since(last_day, day) < bot.rebalance_days:
        return False
    slots = BOT_SLOTS[bot.decide_at]
    i = slots.index(slot)
    done_after = _window(day, slots[i - 1])[1] if i > 0 else session_bounds(day)[0]
    return last is None or last < done_after


def due_slot(bot: Bot, now: datetime) -> str | None:
    """The slot this bot should decide for right now ("open" / "close"), or None."""
    today = ny_date(now)
    last = _last_decided(bot)
    for slot in BOT_SLOTS[bot.decide_at]:
        window = _window(today, slot)
        if window is None:
            return None  # weekend / holiday
        if window[0] <= now < window[1] and _slot_open(bot, today, slot, last):
            return slot
    return None


def decision_due(bot: Bot, now: datetime) -> bool:
    return due_slot(bot, now) is not None


def next_decision_at(bot: Bot, now: datetime) -> datetime | None:
    """When the scheduler will next decide for this bot (the dashboard shows it). None unless active."""
    if bot.strategy == ROTATION:
        return rotation.next_rebalance_at(bot, now)
    if bot.strategy == DIP:
        return dip.next_check_at(bot, now)
    if bot.status != "active":
        return None
    last = _last_decided(bot)
    day = ny_date(now)
    for _ in range(100):  # rebalance_days <= 60 trading days, plus weekends and holidays
        for slot in BOT_SLOTS[bot.decide_at]:
            window = _window(day, slot)
            if window is None:
                break  # not a trading day
            if window[1] > now and _slot_open(bot, day, slot, last):
                return max(window[0], now)
        day += timedelta(days=1)
    return None


def process_bot(bot_id: int, now: datetime, signal_fn=get_signal, broker_factory=get_broker, closes_fn=None,
                dip_fns: dict | None = None) -> None:
    """One scheduler pass for one bot. Never raises: one broken bot must not stop the others.
    closes_fn: the rotation bots' daily price download; dip_fns: the dip bots' bars_fn / news_fn / trend_fn (tests
    pass fakes)."""
    lock = bot_lock(bot_id)
    if not lock.acquire(blocking=False):
        return  # busy: an API action or a slow LLM decision from the previous tick; try next tick
    try:
        with SessionLocal() as session:
            bot = session.get(Bot, bot_id)
            if bot is None or bot.status == "archived":
                return
            try:
                broker = broker_factory(bot)
                sync_pending(session, bot, broker, now)
                if bot.strategy == ROTATION:
                    _process_rotation(session, bot, broker, now, closes_fn)
                    session.commit()
                    return
                if bot.strategy == DIP:
                    _process_dip(session, bot, broker, now, closes_fn, dip_fns or {})
                    session.commit()
                    return
                protect(session, bot, broker, now)
                if not is_open(now):
                    return
                if not reconcile(session, bot, broker, now):
                    session.commit()
                    return
                if now - _last_watch.get(bot.id, datetime.min.replace(tzinfo=now.tzinfo)) >= timedelta(minutes=settings.risk_check_minutes):
                    _last_watch[bot.id] = now
                    watch(session, bot, broker, now)
                if bot.status == "active" and decision_due(bot, now) and now >= _retry_after.get(bot.id, now):
                    before = bot.last_decision_at
                    evaluate(session, bot, broker, now, "scheduled", signal_fn)
                    if bot.last_decision_at == before:  # the signal call failed
                        _retry_after[bot.id] = now + SIGNAL_RETRY
                session.commit()
            except Exception as e:  # BrokerError, network, DB... record it and move on
                session.rollback()
                _log_error_throttled(session, bot_id, e, now)
    finally:
        lock.release()


def _process_rotation(session, bot: Bot, broker, now: datetime, closes_fn) -> None:
    """The scheduler pass for a rotation bot, after its pending orders were synced."""
    if not is_open(now):
        return
    if now - _last_watch.get(bot.id, datetime.min.replace(tzinfo=now.tzinfo)) >= timedelta(minutes=settings.risk_check_minutes):
        _last_watch[bot.id] = now
        if not rotation.reconcile(session, bot, broker, now):
            return
        rotation.watch(session, bot, broker, now, closes_fn)
    if bot.status != "active":
        return  # paused: no new rebalance, and a half-done one waits (HALT means stop trading)
    if rotation.due(bot, now) and now >= _retry_after.get(bot.id, now):
        before = bot.last_decision_at
        rotation.rebalance(session, bot, broker, now, "scheduled", closes_fn)
        if bot.last_decision_at == before:  # the ranking or the quotes failed
            _retry_after[bot.id] = now + SIGNAL_RETRY
    elif bot.rotation_plan:
        rotation.step(session, bot, broker, now)  # the rest of a rebalance whose first orders were still filling


def _process_dip(session, bot: Bot, broker, now: datetime, closes_fn, fns: dict) -> None:
    """The scheduler pass for a dip buyer, after its pending orders were synced: the account and the prices every few
    minutes (like a rotation bot: its holdings and the starting watchlist's value), a check at each interval."""
    if not is_open(now):
        return
    if now - _last_watch.get(bot.id, datetime.min.replace(tzinfo=now.tzinfo)) >= timedelta(minutes=settings.risk_check_minutes):
        _last_watch[bot.id] = now
        if not rotation.reconcile(session, bot, broker, now):
            return
        rotation.watch(session, bot, broker, now, closes_fn)
    if dip.due(bot, now) and now >= _retry_after.get(bot.id, now):
        before = bot.last_decision_at
        dip.check(session, bot, broker, now, "scheduled", **fns)
        if bot.last_decision_at == before:  # the price download failed
            _retry_after[bot.id] = now + SIGNAL_RETRY


def _log_error_throttled(session, bot_id: int, e: Exception, now: datetime) -> None:
    msg = f"{type(e).__name__}: {e}" if not isinstance(e, BrokerError) else str(e)
    key = (bot_id, msg)
    if now - _last_error.get(key, datetime.min.replace(tzinfo=now.tzinfo)) < ERROR_LOG_EVERY:
        return
    _last_error[key] = now
    if not isinstance(e, BrokerError):
        logger.exception("bot=%s scheduler error", bot_id)
    try:
        log_event(session, bot_id, "error", msg, "error", now)
        session.commit()
    except Exception:
        logger.exception("could not write error event")


def tick(now: datetime, signal_fn=get_signal, broker_factory=get_broker) -> None:
    """One pass over every non-archived bot, a few in parallel (LLM decisions can take seconds each). Stock splits
    first, one bot after another: dip bots sharing a symbol must all have them before any compares with the account."""
    try:
        splits.apply_all(now)
    except Exception:
        logger.exception("split pass failed")
    with SessionLocal() as session:
        ids = list(session.scalars(select(Bot.id).where(Bot.status != "archived")))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: process_bot(i, now, signal_fn, broker_factory), ids))
    state["last_tick"] = now


async def run_forever() -> None:
    """Started by main.py on startup; cancelled on shutdown."""
    state["running"], state["started_at"] = True, utcnow()
    logger.info("scheduler started (tick every %ss)", settings.tick_seconds)
    try:
        while True:
            try:
                await asyncio.to_thread(tick, utcnow())
            except Exception as e:
                state["last_error"] = f"{type(e).__name__}: {e}"[:300]
                logger.exception("scheduler tick failed")
            await asyncio.sleep(settings.tick_seconds)
    finally:
        state["running"] = False
