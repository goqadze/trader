"""Trading API: create bots (one symbol + one parameter set each), control them, read their history.

The React dashboard reaches this through nginx at /api/trading/* (see frontend/nginx.conf).
Interactive docs: http://localhost:8002/docs
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import scheduler
from .brokers import SHARED_ACCOUNT_BROKERS, BrokerError, catalog, get_broker
from .config import settings
from .db import get_session, init_db, utcnow
from .market import is_open, next_decision_time, ny_date, session_bounds
from .models import OPEN_ORDER_STATUSES, Bot, Decision, EquitySnapshot, Event, Order
from .schemas import BotCreate, BotOut, BotUpdate, DecisionOut, EventOut, OrderOut, SnapshotOut, StatusOut
from .trader import bot_lock, close_position, evaluate, log_event, open_orders, resting_stop

logger = logging.getLogger("trading-service")
logging.basicConfig(level=logging.INFO)

# Optional error monitoring (GlitchTip/Sentry). Activates only if SENTRY_DSN is set.
if os.getenv("SENTRY_DSN"):
    import sentry_sdk

    sentry_sdk.init(dsn=os.getenv("SENTRY_DSN"), traces_sample_rate=0.1)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    task = None
    if settings.scheduler_enabled:
        task = asyncio.create_task(scheduler.run_forever())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Trading Service", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_bot(session: Session, bot_id: int) -> Bot:
    bot = session.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(404, "bot not found")
    return bot


def _bot_out(session: Session, bot: Bot) -> BotOut:
    price = bot.last_price or bot.entry_price or 0.0
    equity = round(bot.cash + bot.shares * price, 2)
    live = next((b["live"] for b in catalog() if b["name"] == bot.broker), False)
    return BotOut.model_validate({
        **{c: getattr(bot, c) for c in BotOut.model_fields if hasattr(bot, c)},
        "live": live,
        "equity": equity,
        "return_pct": round((equity / bot.allocated_cash - 1) * 100, 2),
        "unrealized_pnl": round(bot.shares * price - bot.cost_basis, 2) if bot.shares else 0.0,
        "buy_hold_return_pct": round((price / bot.benchmark_price - 1) * 100, 2) if bot.benchmark_price and price else None,
        "pending_order": bool(open_orders(session, bot)),
        "stop_at_broker": resting_stop(session, bot) is not None,
    })


class _Locked:
    """`with _Locked(bot_id):` -- 409 instead of waiting if the scheduler is busy with this bot."""

    def __init__(self, bot_id: int):
        self.lock = bot_lock(bot_id)

    def __enter__(self):
        if not self.lock.acquire(timeout=5):
            raise HTTPException(409, "bot is busy (a decision or order is in progress); try again in a moment")

    def __exit__(self, *exc):
        self.lock.release()


def _broker_or_400(bot: Bot):
    try:
        return get_broker(bot)
    except BrokerError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status", response_model=StatusOut)
def status():
    """Market clock + scheduler heartbeat + which brokers are usable. The dashboard header shows this."""
    now = utcnow()
    bounds = session_bounds(ny_date(now))
    return StatusOut(
        now=now,
        market_open=is_open(now),
        session_open=bounds[0] if bounds else None,
        session_close=bounds[1] if bounds else None,
        next_decision_at=next_decision_time(now, settings.decision_minutes_before_close),
        decision_minutes_before_close=settings.decision_minutes_before_close,
        risk_check_minutes=settings.risk_check_minutes,
        scheduler_enabled=settings.scheduler_enabled,
        scheduler_running=scheduler.state["running"],
        scheduler_last_tick=scheduler.state["last_tick"],
        live_trading_allowed=settings.allow_live_trading,
        brokers=catalog(),
    )


# ---------------------------------------------------------------------------
# Bots
# ---------------------------------------------------------------------------

@app.get("/bots", response_model=list[BotOut])
def list_bots(include_archived: bool = False, session: Session = Depends(get_session)):
    q = select(Bot).order_by(Bot.created_at.desc())
    if not include_archived:
        q = q.where(Bot.status != "archived")
    return [_bot_out(session, b) for b in session.scalars(q)]


@app.post("/bots", response_model=BotOut, status_code=201)
def create_bot(body: BotCreate, session: Session = Depends(get_session)):
    symbol = body.symbol.upper()
    info = next((b for b in catalog() if b["name"] == body.broker), None)
    if info is None:
        raise HTTPException(422, f"unknown broker '{body.broker}'")
    if not info["available"]:
        raise HTTPException(422, f"broker '{body.broker}' unavailable: {info['reason']}")
    if info["live"] and not body.confirm_live:
        raise HTTPException(422, "real-money bot: set confirm_live=true to confirm")
    if body.broker in SHARED_ACCOUNT_BROKERS:
        clash = session.scalar(select(Bot).where(Bot.broker == body.broker, Bot.symbol == symbol, Bot.status != "archived"))
        if clash:
            raise HTTPException(409, f"bot #{clash.id} already trades {symbol} on {body.broker}; one bot per symbol "
                                     "per real account, or their positions would mix")

    params = body.model_dump(exclude={"name", "symbol", "broker", "allocated_cash", "confirm_live"})
    bot = Bot(name=body.name or f"{symbol} {body.mode}", symbol=symbol, broker=body.broker, status="active",
              allocated_cash=body.allocated_cash, cash=body.allocated_cash, peak_equity=body.allocated_cash, **params)

    # Validate the symbol with a real quote and remember the price as the buy & hold baseline
    try:
        q = _broker_or_400(bot).quote(symbol)
    except BrokerError as e:
        raise HTTPException(422, f"can't get a price for {symbol}: {e}")
    bot.benchmark_price, bot.last_price, bot.last_price_at = q.price, q.price, q.at

    session.add(bot)
    session.flush()
    log_event(session, bot.id, "created",
              f"Created {bot.name}: {symbol} on {bot.broker} with ${bot.allocated_cash:,.2f}, {bot.mode} mode, "
              f"min conf {bot.min_confidence}, every {bot.rebalance_days} trading days, "
              f"stop {bot.stop_pct:.1%} / target {bot.target_pct:.1%}", "warning" if info["live"] else "info")
    session.commit()
    return _bot_out(session, bot)


@app.get("/bots/{bot_id}", response_model=BotOut)
def get_bot(bot_id: int, session: Session = Depends(get_session)):
    return _bot_out(session, _get_bot(session, bot_id))


@app.patch("/bots/{bot_id}", response_model=BotOut)
def update_bot(bot_id: int, body: BotUpdate, session: Session = Depends(get_session)):
    """Change parameters. Takes effect from the next decision; an open position keeps its stop/target."""
    with _Locked(bot_id):
        bot = _get_bot(session, bot_id)
        if bot.status == "archived":
            raise HTTPException(409, "bot is archived")
        changes = []
        for field, value in body.model_dump(exclude_unset=True, exclude_none=True).items():
            old = getattr(bot, field)
            if old != value:
                setattr(bot, field, value)
                changes.append(f"{field} {old} → {value}")
        if changes:
            log_event(session, bot.id, "params", "Changed: " + ", ".join(changes))
            session.commit()
        return _bot_out(session, bot)


def _set_status(session: Session, bot: Bot, allowed_from: tuple[str, ...], to: str, message: str) -> BotOut:
    """Status changes take no bot lock on purpose: Pause and HALT must work even while a slow LLM
    decision is running. They only write the status column, and the scheduler re-reads the status
    before every new decision, so the pause takes effect from the next decision."""
    if bot.status not in allowed_from:
        raise HTTPException(409, f"bot is {bot.status}")
    bot.status = to
    log_event(session, bot.id, to, message)
    session.commit()
    return _bot_out(session, bot)


@app.post("/bots/{bot_id}/pause", response_model=BotOut)
def pause_bot(bot_id: int, session: Session = Depends(get_session)):
    return _set_status(session, _get_bot(session, bot_id), ("active",), "paused",
                       "Paused by user: no new decisions; stop-loss/target still protect an open position")


@app.post("/bots/{bot_id}/resume", response_model=BotOut)
def resume_bot(bot_id: int, session: Session = Depends(get_session)):
    bot = _get_bot(session, bot_id)
    _broker_or_400(bot)  # e.g. live trading was switched off since the bot was created
    if bot.status == "paused":
        # Restart the drawdown peak from today's equity, or a breaker-paused bot would trip again at once
        bot.peak_equity = round(bot.cash + bot.shares * (bot.last_price or bot.entry_price or 0), 2)
    return _set_status(session, bot, ("paused",), "active", "Resumed by user")


@app.post("/bots/{bot_id}/archive", response_model=BotOut)
def archive_bot(bot_id: int, session: Session = Depends(get_session)):
    """Retire a bot. History is kept; there is deliberately no hard delete (audit trail)."""
    with _Locked(bot_id):  # locked: the "is it flat?" check must not race with a buy
        bot = _get_bot(session, bot_id)
        if bot.shares or open_orders(session, bot) or resting_stop(session, bot):
            raise HTTPException(409, "close the position (and let pending orders finish) before archiving")
        return _set_status(session, bot, ("active", "paused"), "archived", "Archived by user")


@app.post("/bots/{bot_id}/run", response_model=DecisionOut)
def run_now(bot_id: int, session: Session = Depends(get_session)):
    """Decide right now. Trades only if the market is open AND the bot is active; otherwise it's a
    preview that shows what the bot would do without sending anything."""
    with _Locked(bot_id):
        bot = _get_bot(session, bot_id)
        if bot.status == "archived":
            raise HTTPException(409, "bot is archived")
        broker = _broker_or_400(bot)
        now = utcnow()
        kind = "manual" if is_open(now) and bot.status == "active" else "preview"
        return evaluate(session, bot, broker, now, kind)


@app.post("/bots/{bot_id}/close", response_model=OrderOut)
def close_now(bot_id: int, session: Session = Depends(get_session)):
    """Sell the whole position now at market (manual override; works on paused bots too)."""
    with _Locked(bot_id):
        bot = _get_bot(session, bot_id)
        now = utcnow()
        if not is_open(now):
            raise HTTPException(409, "market is closed; orders are only sent during regular hours")
        if bot.shares == 0:
            raise HTTPException(409, "no open position")
        if open_orders(session, bot):
            raise HTTPException(409, "an order is already pending")
        broker = _broker_or_400(bot)
        log_event(session, bot.id, "order", "Close position requested by user", now=now)
        order = close_position(session, bot, broker, "manual", now)
        if order is None:  # see close_position: the broker-held stop decided it
            raise HTTPException(409, "the stop-loss order at the broker already sold the position" if bot.shares == 0 else
                                "the broker hasn't confirmed canceling the stop-loss order yet; try again in a moment")
        return order


@app.post("/halt")
def halt_all(session: Session = Depends(get_session)):
    """Kill switch: pause every active bot at once (never blocks, see _set_status). Open positions keep
    their stop-loss/target. Resume bots one by one when you're ready."""
    bots = list(session.scalars(select(Bot).where(Bot.status == "active")))
    for bot in bots:
        bot.status = "paused"
        log_event(session, bot.id, "paused", "Paused by HALT ALL")
    log_event(session, None, "halt", f"HALT ALL: paused {len(bots)} bot(s)", "warning")
    session.commit()
    return {"paused": len(bots)}


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def _page(session, model, bot_id, order_col, limit, offset):
    q = select(model).where(model.bot_id == bot_id).order_by(order_col.desc()).limit(limit).offset(offset)
    return list(session.scalars(q))


@app.get("/bots/{bot_id}/decisions", response_model=list[DecisionOut])
def list_decisions(bot_id: int, limit: int = Query(200, le=1000), offset: int = 0, session: Session = Depends(get_session)):
    _get_bot(session, bot_id)
    return _page(session, Decision, bot_id, Decision.id, limit, offset)


@app.get("/bots/{bot_id}/orders", response_model=list[OrderOut])
def list_orders(bot_id: int, limit: int = Query(200, le=1000), offset: int = 0, session: Session = Depends(get_session)):
    _get_bot(session, bot_id)
    return _page(session, Order, bot_id, Order.id, limit, offset)


@app.get("/bots/{bot_id}/events", response_model=list[EventOut])
def list_bot_events(bot_id: int, limit: int = Query(200, le=1000), offset: int = 0, session: Session = Depends(get_session)):
    _get_bot(session, bot_id)
    return _page(session, Event, bot_id, Event.id, limit, offset)


@app.get("/bots/{bot_id}/equity", response_model=list[SnapshotOut])
def list_equity(bot_id: int, session: Session = Depends(get_session)):
    _get_bot(session, bot_id)
    return list(session.scalars(select(EquitySnapshot).where(EquitySnapshot.bot_id == bot_id).order_by(EquitySnapshot.day)))


@app.get("/events", response_model=list[EventOut])
def list_events(limit: int = Query(100, le=1000), session: Session = Depends(get_session)):
    """Recent events across all bots (the overview page's activity feed)."""
    return list(session.scalars(select(Event).order_by(Event.id.desc()).limit(limit)))


@app.get("/summary")
def summary(session: Session = Depends(get_session)):
    """Counts for the overview header."""
    by_status = dict(session.execute(select(Bot.status, func.count()).group_by(Bot.status)).all())
    pending = session.scalar(select(func.count()).select_from(Order)
                             .where(Order.order_type == "market", Order.status.in_(OPEN_ORDER_STATUSES)))
    return {"bots_by_status": by_status, "pending_orders": pending}
