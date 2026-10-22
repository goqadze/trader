"""Alpaca's own fees on the live account, booked to the bots that caused them.

Alpaca takes two small fees a trading day from the whole account, not per order: a CAT fee (the Consolidated Audit
Trail: $0.01 on any day with trades, buys too) and a REG fee (SEC and FINRA on the day's sales, rounded up to a cent).
Orders come back from Alpaca without them (brokers/alpaca.py), so without this the bots' cash would be a cent or two a
day more than the account has, and the dashboard's free cash (the account's cash minus the bots') would slowly go
negative.

Every FEES_EVERY_S this reads the account's fee lines of the last FEES_DAYS days and books each new one once
(AccountFee): it comes out of the cash and the realized P&L of the bot that caused most of it that day, archived bots
too: the most sales in dollars for a REG fee, the most filled orders for a CAT fee. An archived bot's cash is no longer
counted in free cash, so a fee of a bot archived since shows there as money of no bot that paid it. A fee of a day
none of the bots traded (the owner's own trades), or of another kind (not CAT or REG), is kept with no bot, to be seen
in the activity. A bot busy with its pass is tried again the next time.

Only the live account: the paper account is shared with the Mac, whose bots would book the same fee a second time."""

import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .brokers import BrokerError
from .brokers.alpaca import AlpacaBroker
from .config import settings
from .db import SessionLocal, utcnow
from .market import ny_date
from .models import AccountFee, Bot, Order
from .trader import bot_lock, log_event

logger = logging.getLogger("trading-service")

BROKER = "alpaca-live"
FEES_DAYS = 30  # each read goes this far back, so a fee line Alpaca posts late is still found
FEES_EVERY_S = 3600
LOCK_WAIT_S = 30  # a bot's pass takes a few seconds; longer than this and the fee waits for the next read
KINDS = ("CAT", "REG")  # the per-day trading fees; anything else is no bot's


def _day_text(day: date) -> str:
    return f"{day:%b} {day.day}"


def _payer(session: Session, day: date, reg: bool) -> tuple[Bot | None, str]:
    """The live bot that caused most of a day's fee, and why in words; None when no bot traded that day. Orders count
    on their New York day. Ties go to the oldest bot."""
    start = datetime.combine(day, time(), tzinfo=timezone.utc) - timedelta(days=1)
    orders = session.scalars(select(Order).join(Bot, Bot.id == Order.bot_id).where(
        Bot.broker == BROKER, Order.filled_qty > 0, Order.created_at >= start, Order.created_at < start + timedelta(days=3)))
    trades: dict[int, int] = defaultdict(int)
    sold: dict[int, float] = defaultdict(float)
    for o in orders:
        if ny_date(o.created_at) == day:
            trades[o.bot_id] += 1
            if o.side == "SELL":
                sold[o.bot_id] += o.filled_qty * (o.avg_price or 0.0)
    if not trades:
        return None, ""
    if reg and sold:
        bot_id = min(sold, key=lambda i: (-sold[i], i))
        return session.get(Bot, bot_id), f"it sold the most that day (${sold[bot_id]:,.2f} of ${sum(sold.values()):,.2f})"
    bot_id = min(trades, key=lambda i: (-trades[i], i))
    n = sum(trades.values())
    return session.get(Bot, bot_id), f"it had the most orders filled that day ({trades[bot_id]} of {n})"


def book(session: Session, fees: list[dict], now: datetime) -> int:
    """Books the fee lines (AlpacaBroker.fees) not booked before, as the module says; returns how many it booked."""
    known = set(session.scalars(select(AccountFee.activity_id).where(AccountFee.activity_id.in_([f["id"] for f in fees]))))
    booked = 0
    for f in fees:
        if f["id"] in known:
            continue
        kind = f["description"].split(" ", 1)[0].upper()
        what = f"Alpaca's {kind} fee for {_day_text(f['day'])}: ${f['amount']:.2f} ({f['description']})"
        bot, why = _payer(session, f["day"], kind == "REG") if kind in KINDS else (None, "")
        if bot is None:
            reason = "none of the bots traded that day" if kind in KINDS else "not a fee on the bots' trades"
            session.add(AccountFee(broker=BROKER, activity_id=f["id"], day=f["day"], amount=f["amount"],
                                   description=f["description"], bot_id=None))
            log_event(session, None, "fee", f"{what}. No bot pays it: {reason}.", now=now)
            session.commit()
            booked += 1
            continue
        lock = bot_lock(bot.id)
        if not lock.acquire(timeout=LOCK_WAIT_S):
            continue  # its pass is running: the next read books it
        try:
            session.refresh(bot)  # the cash as the scheduler left it
            bot.cash = round(bot.cash - f["amount"], 2)
            bot.realized_pnl = round(bot.realized_pnl - f["amount"], 2)
            session.add(AccountFee(broker=BROKER, activity_id=f["id"], day=f["day"], amount=f["amount"],
                                   description=f["description"], bot_id=bot.id))
            log_event(session, bot.id, "fee", f"{what}. Paid by this bot: {why}.", now=now)
            session.commit()
        finally:
            lock.release()
        booked += 1
    return booked


def run_once(now: datetime | None = None, broker: AlpacaBroker | None = None) -> int:
    """One read of the live account's fee lines and the booking of the new ones. Nothing without the live keys, or on a
    database that never had a live bot (the Mac's)."""
    now = now or utcnow()
    with SessionLocal() as session:
        if session.scalar(select(Bot.id).where(Bot.broker == BROKER).limit(1)) is None:
            return 0
        if broker is None:
            if not (settings.alpaca_live_key_id and settings.alpaca_live_secret_key):
                return 0
            broker = AlpacaBroker(settings.alpaca_live_key_id, settings.alpaca_live_secret_key, live=True)
        return book(session, broker.fees(ny_date(now) - timedelta(days=FEES_DAYS)), now)


async def run_forever() -> None:
    """Started by main.py next to the scheduler; cancelled on shutdown."""
    while True:
        try:
            await asyncio.to_thread(run_once)
        except BrokerError as e:
            logger.warning("Alpaca's fees not read: %s", e)
        except Exception:
            logger.exception("booking Alpaca's fees failed")
        await asyncio.sleep(FEES_EVERY_S)
