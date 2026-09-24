"""The background loop that runs every bot. It wakes every TICK_SECONDS and, for each bot:

  1. resolves orders still pending at the broker          (any time)
     and keeps the stop-loss resting at the broker         (brokers that can hold one: Alpaca)
  2. checks the real account matches the bot's records   (market hours, real brokers only)
  3. refreshes the price, records equity, enforces stops  (market hours, every RISK_CHECK_MINUTES)
  4. makes the daily decision when it is due              (active bots, DECISION_MINUTES_BEFORE_CLOSE before the close,
                                                           every `rebalance_days` trading days)

Restart-safe by design: "did we already decide today?" lives in the database (Bot.last_decision_date),
not in memory, so a restart at 15:40 doesn't decide twice. If the service is down for the whole
decision window, that day is simply skipped and the next trading day catches up.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import select

from .brokers import BrokerError, get_broker
from .config import settings
from .db import SessionLocal, utcnow
from .decision_client import get_signal
from .market import decision_time, is_open, ny_date, session_bounds, sessions_since
from .models import Bot
from .trader import bot_lock, evaluate, log_event, protect, reconcile, sync_pending, watch

logger = logging.getLogger("trading-service")

# In-memory pacing only (losing these on restart is harmless: worst case one extra price check)
_last_watch: dict[int, datetime] = {}
_retry_after: dict[int, datetime] = {}  # a failed signal call is retried after a pause, not every tick
_last_error: dict[tuple[int, str], datetime] = {}  # don't write the same error to the audit log every 30s
state: dict = {"last_tick": None, "running": False}

SIGNAL_RETRY = timedelta(minutes=5)
ERROR_LOG_EVERY = timedelta(hours=1)


def decision_due(bot: Bot, now: datetime) -> bool:
    """True inside today's decision window if this bot hasn't decided today and N trading days have passed."""
    today = ny_date(now)
    at = decision_time(today, settings.decision_minutes_before_close)
    if at is None:
        return False  # weekend / holiday
    close = session_bounds(today)[1]
    if not (at <= now < close):
        return False
    if bot.last_decision_date == today:
        return False
    return sessions_since(bot.last_decision_date, today) >= bot.rebalance_days


def process_bot(bot_id: int, now: datetime, signal_fn=get_signal, broker_factory=get_broker) -> None:
    """One scheduler pass for one bot. Never raises: one broken bot must not stop the others."""
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
                    evaluate(session, bot, broker, now, "scheduled", signal_fn)
                    if bot.last_decision_date != ny_date(now):  # the signal call failed
                        _retry_after[bot.id] = now + SIGNAL_RETRY
                session.commit()
            except Exception as e:  # BrokerError, network, DB... record it and move on
                session.rollback()
                _log_error_throttled(session, bot_id, e, now)
    finally:
        lock.release()


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
    """One pass over every non-archived bot, a few in parallel (LLM decisions can take seconds each)."""
    with SessionLocal() as session:
        ids = list(session.scalars(select(Bot.id).where(Bot.status != "archived")))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: process_bot(i, now, signal_fn, broker_factory), ids))
    state["last_tick"] = now


async def run_forever() -> None:
    """Started by main.py on startup; cancelled on shutdown."""
    state["running"] = True
    logger.info("scheduler started (tick every %ss)", settings.tick_seconds)
    try:
        while True:
            try:
                await asyncio.to_thread(tick, utcnow())
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(settings.tick_seconds)
    finally:
        state["running"] = False
