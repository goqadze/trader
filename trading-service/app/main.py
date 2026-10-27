"""Trading API: create bots (one symbol + one parameter set each, a momentum rotation across a universe, or a dip buyer
watching a list of symbols), control them, read their history.

The React dashboard reaches this through nginx at /api/trading/* (see frontend/nginx.conf), and nginx only
lets signed-in users through. Sign-in and accounts are in auth.py (/auth/*).
Interactive docs: http://localhost:8002/docs
"""

import asyncio
import json
import logging
import os
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, defer

from . import auth, dip, fees, journal, notify, risk_report, rotation, scheduler, studies, withdraw
from . import sectors as badges
from .brokers import SHARED_ACCOUNT_BROKERS, BrokerError, catalog, get_broker
from .config import settings
from .db import SessionLocal, get_session, init_db, utcnow
from .market import is_open, next_decision_time, ny_date, session_bounds
from .models import (DIP, HOLDINGS_STRATEGIES, OPEN_ORDER_STATUSES, ROTATION, Bot, Decision, DipPreset, DipStudy, DipTest,
                     EquitySnapshot, Event, Holding, Notification, Order, Signal, WatchItem)
from .schemas import (AccountOut, BotBrief, BotCreate, BotKind, BotOut, BotSetupSave, BotSort, BotTableOut, BotTotals, BotUpdate, CapitalAdd, CapitalWithdraw, DecisionOut, DipBotCreate, DipBotUpdate, DipPresetIn,
                      DipPresetOut, DipStudyIn, DipStudyOut, DipStudySummaryOut, DipTestDetailOut, DipTestIn, DipTestOut, DipTestPageOut,
                      EmailAlertsIn, EmailAlertsOut, EventOut, HoldingOut, NotificationOut, RiskReportOut, NotifyCategoryOut, OrderOut,
                      PRESET_CONFIGS, RatingIn, RotationBotCreate, RotationPresetIn, SectorsIn, SignalOut, SnapshotOut, StatusOut, StudyStrategy,
                      WatchItemOut, WatchSymbols, WithdrawalPlanOut, WithdrawalSaleOut, check_window)
from .trader import (add_capital, benchmark_value, bot_equity, bot_lock, capital, close_position, evaluate, holdings,
                     log_event, open_orders, resting_stop, return_pct)

logger = logging.getLogger("trading-service")
logging.basicConfig(level=logging.INFO)

# Optional error monitoring (GlitchTip/Sentry). Activates only if SENTRY_DSN is set.
if os.getenv("SENTRY_DSN"):
    import sentry_sdk

    sentry_sdk.init(dsn=os.getenv("SENTRY_DSN"), traces_sample_rate=0.1)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    auth.startup_checks()
    tasks = []
    if settings.scheduler_enabled:  # the background work: the bots, the email alerts they queue, the daily risk report,
        # Alpaca's daily fees booked to the live bots
        tasks = [asyncio.create_task(scheduler.run_forever()), asyncio.create_task(notify.run_forever()),
                 asyncio.create_task(risk_report.run_forever()), asyncio.create_task(fees.run_forever())]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Trading Service", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=4096)  # a dip study's grid is a few MB of JSON
app.include_router(auth.router)
app.include_router(journal.router)  # each user's own trading journal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DECIDE_AT_LABEL = {"close": "before the close", "open": "after the open", "both": "after the open and before the close"}


def _get_bot(session: Session, bot_id: int) -> Bot:
    bot = session.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(404, "bot not found")
    return bot


def _live_setups(session: Session) -> dict[tuple[int, str], list[int]]:
    """The tested setups that real money trades: (test id, code) -> the ids of the live bots started from it, archived
    ones left out. So a paper bot's setup can say a live bot uses it too (BotOut.setup_live)."""
    live = [b["name"] for b in catalog() if b["live"]]
    out: dict[tuple[int, str], list[int]] = {}
    for b in session.scalars(select(Bot).where(Bot.broker.in_(live), Bot.status != "archived").order_by(Bot.id)):
        if b.setup:
            out.setdefault((b.setup["test_id"], b.setup["code"]), []).append(b.id)
    return out


def _price(bot: Bot) -> float:
    """What its one position is worth a share (a rotation or dip bot's "price": what its universe held equally is worth)."""
    return bot.last_price or bot.entry_price or 0.0


def _value(h: Holding) -> float:
    """A holding at its last price (not priced yet: what it cost)."""
    return round(h.shares * (h.last_price or h.cost_basis / h.shares), 2)


def _unrealized(bot: Bot, held: list[Holding], price: float) -> float:
    """Its open positions' gain or loss in dollars: its holdings', or its one position's at `price` (the Position column)."""
    if held:
        return round(sum(round(_value(h) - h.cost_basis, 2) for h in held), 2)
    return round(bot.shares * price - bot.cost_basis if bot.shares else 0.0, 2)


def _setup_all(bot: Bot) -> float | None:
    """Its tested setup's All, worked out from the windows the bot keeps, so bots made before it was shown have it too."""
    return studies.all_return(bot.setup["periods"], bot.setup["results"]) if bot.setup else None


def _bot_out(session: Session, bot: Bot, yahoo: dict[str, str] | None = None,
             live_setups: dict[tuple[int, str], list[int]] | None = None) -> BotOut:
    """yahoo: the symbols' Yahoo sectors (sectors.yahoo_sectors) and live_setups (_live_setups), read once by a caller
    listing many bots."""
    price = _price(bot)
    held_rows = holdings(session, bot)
    equity = bot_equity(session, bot, price, held_rows)
    live = next((b["live"] for b in catalog() if b["name"] == bot.broker), False)
    now = utcnow()
    items = dip.watchlist(session, bot) if bot.strategy == DIP else []
    watched = {i.symbol for i in items}
    rows = []
    for h in held_rows:
        value = _value(h)
        rows.append(HoldingOut(symbol=h.symbol, shares=h.shares, cost_basis=h.cost_basis, last_price=h.last_price,
                               last_price_at=h.last_price_at, value=value, unrealized_pnl=round(value - h.cost_basis, 2),
                               weight_pct=round(value / equity * 100, 1) if equity else 0.0, opened_at=h.opened_at,
                               entry_price=h.entry_price, reference_price=h.reference_price, target_price=h.target_price,
                               stop_price=h.stop_price, on_watchlist=bot.strategy != DIP or h.symbol in watched))
    held = {r.symbol for r in rows}
    sector_badges, sectors_auto, from_symbols = badges.of_bot(bot, [i.symbol for i in items],
                                                              badges.yahoo_sectors(session) if yahoo is None else yahoo)

    def zone(i) -> float | None:  # the fall that buys it: by the rules as they are now (a daily move: the last check's)
        return i.buy_drop if bot.drop_mode == "volatility" else dip.buy_fall(bot, i.reference_price)

    watchlist = [WatchItemOut.model_validate(i).model_copy(update={
        "held": i.symbol in held,
        "buy_drop": zone(i),
        "buy_below": round(i.reference_price * (1 - zone(i)), 2) if i.reference_price and zone(i) is not None else None,
        "rebound_at": round(i.trough_price * (1 + (bot.rebound_pct or 0.01)), 2)
        if bot.rebound and i.dip_reference and i.trough_price else None}) for i in items]
    return BotOut.model_validate({
        **{c: getattr(bot, c) for c in BotOut.model_fields if hasattr(bot, c)},
        "live": live,
        "sectors": sector_badges,
        "sectors_auto": sectors_auto,
        "sectors_from_symbols": from_symbols,
        "setup": {**bot.setup, "all": _setup_all(bot)} if bot.setup else None,
        "setup_live": [i for i in (_live_setups(session) if live_setups is None else live_setups).get(
            (bot.setup["test_id"], bot.setup["code"]), []) if i != bot.id] if bot.setup else [],
        "equity": equity,
        "capital": capital(bot),
        "return_pct": return_pct(bot, equity),
        "unrealized_pnl": _unrealized(bot, held_rows, price),
        "buy_hold_return_pct": return_pct(bot, bh) if (bh := benchmark_value(bot, price)) else None,
        "pending_order": bool(open_orders(session, bot)),
        "stop_at_broker": resting_stop(session, bot) is not None,
        "next_decision_at": scheduler.next_decision_at(bot, now),
        "holdings": rows,
        "rebalancing": bot.rotation_plan is not None,
        "watchlist": watchlist,
    })


def _symbol_clash(session: Session, broker: str, symbols: list[str], exclude: int | None = None,
                  holdings_bot: bool = False) -> tuple[str, Bot] | None:
    """On a real (paper or live) account, a bot that can't share one of these symbols with the new one: (symbol, that bot).
    Dip and rotation bots share symbols with each other (holdings_bot=True: the new one is one of them): each books its
    own fills in its own holdings and sells only its own shares, the account check adds up all their records
    (rotation.reconcile), and no order crosses another bot's (trader._refusal). A single-symbol bot keeps its symbol to
    itself: its stop-loss rests at the broker as an open sell order, Alpaca rejects any buy of that symbol while it's
    open (wash-trade protection), and its account check counts only its own shares. The simulator keeps every bot apart.
    exclude: a bot that may trade them (a dip bot adding symbols to its own watchlist)."""
    if broker not in SHARED_ACCOUNT_BROKERS:
        return None
    for other in session.scalars(select(Bot).where(Bot.broker == broker, Bot.status != "archived", Bot.id != (exclude or 0))):
        if holdings_bot and other.strategy in HOLDINGS_STRATEGIES:
            continue  # two dip or rotation bots: fine
        if other.strategy == ROTATION:
            theirs = set(other.universe or [])
        elif other.strategy == DIP:  # what it watches now, and what it still holds after a symbol was removed
            theirs = {i.symbol for i in dip.watchlist(session, other)} | {h.symbol for h in holdings(session, other)}
        else:
            theirs = {other.symbol}
        for s in symbols:
            if s in theirs:
                return s, other
    return None


def _clash_text(clash: tuple[str, Bot], broker: str) -> str:
    symbol, other = clash
    return (f"bot #{other.id} already trades {symbol} on {broker}. On a real account a single-symbol bot keeps its symbol "
            "to itself: its stop-loss waits at Alpaca as an open sell order (Alpaca rejects buys of that symbol "
            "meanwhile). Dip and rotation bots share symbols with each other")


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

TICK_LATE = timedelta(minutes=5)  # no finished tick for this long: the scheduler is stuck (a tick takes seconds)


@app.get("/health")
def health(session: Session = Depends(get_session)):
    """For the watchdog (and you): the database answers and the scheduler ticks. 503 with what's wrong otherwise."""
    problems = []
    try:
        session.execute(text("SELECT 1"))
    except Exception as e:
        problems.append(f"the database doesn't answer ({type(e).__name__}): the bots can't check or trade")
    st = scheduler.state
    if settings.scheduler_enabled:
        since = st["last_tick"] or st["started_at"]
        live_since = st.get("live_tick") or st["started_at"]  # the live account's bots have a pass of their own
        late = max(TICK_LATE, timedelta(seconds=10 * settings.tick_seconds))
        error = f" (last error: {st['last_error']})" if st["last_error"] else ""
        if not st["running"]:
            problems.append("the scheduler isn't running: the bots don't check or trade")
        else:
            if since is not None and utcnow() - since > late:
                minutes = int((utcnow() - since).total_seconds() // 60)
                problems.append(f"the scheduler hasn't finished a round for {minutes} minutes: the bots don't check or "
                                f"trade{error}")
            if live_since is not None and utcnow() - live_since > late:
                minutes = int((utcnow() - live_since).total_seconds() // 60)
                problems.append(f"the live bots' pass hasn't finished for {minutes} minutes: the real-money bots don't "
                                f"check or trade{error}")
    return JSONResponse({"status": "down" if problems else "ok", "problems": problems,
                         "last_tick": st["last_tick"].isoformat() if st["last_tick"] else None},
                        status_code=503 if problems else 200)


@app.get("/status", response_model=StatusOut)
def status(session: Session = Depends(get_session)):
    """Market clock + scheduler heartbeat + which brokers are usable. The dashboard header shows this."""
    now = utcnow()
    bounds = session_bounds(ny_date(now))
    # The soonest decision of any active bot; with none, the next regular close slot
    upcoming = [t for b in session.scalars(select(Bot).where(Bot.status == "active"))
                if (t := scheduler.next_decision_at(b, now)) is not None]
    return StatusOut(
        now=now,
        market_open=is_open(now),
        session_open=bounds[0] if bounds else None,
        session_close=bounds[1] if bounds else None,
        next_decision_at=min(upcoming, default=next_decision_time(now, settings.decision_minutes_before_close)),
        decision_minutes_before_close=settings.decision_minutes_before_close,
        decision_minutes_after_open=settings.decision_minutes_after_open,
        risk_check_minutes=settings.risk_check_minutes,
        scheduler_enabled=settings.scheduler_enabled,
        scheduler_running=scheduler.state["running"],
        scheduler_last_tick=scheduler.state["last_tick"],
        live_trading_allowed=settings.allow_live_trading,
        brokers=catalog(),
    )


ACCOUNT_TTL = 30.0  # seconds one read of an account is reused: every open dashboard asks every 30 s
_account_reads: dict[str, tuple[float, dict | None, str, object]] = {}  # broker -> (monotonic time, funds, error, read at)


def _funds(name: str) -> tuple[dict | None, str, object]:
    """The account's cash and buying powers (Broker.funds), an error if it couldn't be read, and when it was read; at
    most one Alpaca request per account every ACCOUNT_TTL seconds."""
    hit = _account_reads.get(name)
    if hit and time.monotonic() - hit[0] < ACCOUNT_TTL:
        return hit[1:]
    try:
        read = (get_broker(SimpleNamespace(broker=name)).funds(), "", utcnow())
    except BrokerError as e:
        read = (None, str(e), utcnow())
    _account_reads[name] = (time.monotonic(), *read)
    return read


@app.get("/accounts", response_model=list[AccountOut])
def accounts(session: Session = Depends(get_session)):
    """Per real account (Alpaca paper, Alpaca live; only those with keys): its cash minus the cash of every bot on it
    that isn't archived (a paused bot keeps its cash for when it resumes). What's left belongs to no bot: room for a
    new bot or Add money. Cash, not buying power: after the close Alpaca's non-margin buying power leaves out the day's
    sales until they settle (T+1), and open orders hold some back, so it can dip under the cash for a night without any
    money being gone. It still caps the bots' buys, so it comes along as `spendable`.
    The simulator has no account (each bot's cash is its own), so it isn't listed. The Mac and the server keep separate
    bots on the same Alpaca paper account: each counts only its own."""
    out = []
    for b in catalog():
        if b["name"] not in SHARED_ACCOUNT_BROKERS or not b["available"]:
            continue
        name = b["name"]
        bots = list(session.scalars(select(Bot).where(Bot.broker == name, Bot.status != "archived")))
        held = round(sum(x.cash for x in bots), 2)
        open_buys = session.scalar(select(func.count()).select_from(Order).join(Bot, Order.bot_id == Bot.id).where(
            Bot.broker == name, Order.side == "BUY", Order.status.in_(OPEN_ORDER_STATUSES))) or 0
        funds, error, at = _funds(name)
        f = funds or {}
        cash = f.get("cash")
        out.append(AccountOut(broker=name, label=b["label"], live=b["live"], cash=cash, buying_power=f.get("buying_power"),
                              non_marginable_buying_power=f.get("non_marginable_buying_power"), spendable=f.get("spendable"),
                              bots_cash=held, bots=len(bots), free=None if cash is None else round(cash - held, 2),
                              open_buys=open_buys, error=error, checked_at=at))
    return out


# ---------------------------------------------------------------------------
# Bots
# ---------------------------------------------------------------------------

@app.get("/bots", response_model=list[BotOut])
def list_bots(include_archived: bool = False, session: Session = Depends(get_session)):
    q = select(Bot).order_by(Bot.created_at.desc())
    if not include_archived:
        q = q.where(Bot.status != "archived")
    yahoo, live_setups = badges.yahoo_sectors(session), _live_setups(session)
    return [_bot_out(session, b, yahoo, live_setups) for b in session.scalars(q)]


@dataclass
class _Row:
    """A bot's keys in the bot table: what it is sorted, filtered and added up by."""

    bot: Bot
    sectors: list[str]
    equity: float
    keys: dict[str, float | None]  # BotSort -> its value (None: no tested setup or buy & hold, sorted last either way)


def _kind(bot: Bot) -> str:
    """Its BotKind, for the bot table's Bot filter."""
    return "dip" if bot.strategy == DIP else "rotation" if bot.strategy == ROTATION else "symbol"


@app.get("/bots/table", response_model=BotTableOut)
def bot_table(broker: str = "all", include_archived: bool = False, sector: list[str] = Query([]),
              kind: list[BotKind] = Query([]), sort: BotSort | None = None, order: Literal["asc", "desc"] = "desc", offset: int = Query(0, ge=0),
              limit: int = Query(50, ge=1, le=500), session: Session = Depends(get_session)):
    """The overview's bot table, one page at a time: one broker's bots ("all", or a broker with none: every bot), those
    of any of the `kind`s (dip buyer, momentum rotation, single symbol) and with any of the `sector` badges, sorted by a
    column (none: newest first; ties keep that order; no tested setup or buy & hold: last either way), from `offset`. With it, what
    the page shows of all of them: bots per broker, the broker's totals, kind and sector choices, the active bots for
    Halt all, and every bot's kind for the activity feed.
    Return and Position need every bot's equity: all their holdings and watchlists are read at once, and only the
    page's bots are built in full (_bot_out: watchlists, orders, setups ...)."""
    q = select(Bot).order_by(Bot.created_at.desc(), Bot.id.desc())
    if not include_archived:
        q = q.where(Bot.status != "archived")
    bots = list(session.scalars(q))
    held: dict[int, list[Holding]] = {}
    for h in session.scalars(select(Holding).order_by(Holding.symbol)):
        held.setdefault(h.bot_id, []).append(h)
    watched: dict[int, list[str]] = {}
    for bot_id, symbol in session.execute(select(WatchItem.bot_id, WatchItem.symbol)):
        watched.setdefault(bot_id, []).append(symbol)
    yahoo = badges.yahoo_sectors(session)

    def row(bot: Bot) -> _Row:
        price, mine = _price(bot), held.get(bot.id, [])
        equity = bot_equity(session, bot, price, mine)
        ret, bh = return_pct(bot, equity), benchmark_value(bot, price)
        return _Row(bot, badges.of_bot(bot, watched.get(bot.id, []), yahoo)[0], equity, {
            "id": bot.id, "rating": bot.rating, "score": (bot.setup or {}).get("score"), "all": _setup_all(bot),
            "ret": ret, "pos": _unrealized(bot, mine, price),
            # How far its return is ahead of buy & hold, in points (the Return column's two numbers, as shown)
            "beat": round(ret - return_pct(bot, bh), 2) if bh else None})

    rows = [row(b) for b in bots]
    brokers = Counter(r.bot.broker for r in rows)
    shown = broker if broker in brokers else "all"
    rows = [r for r in rows if shown == "all" or r.bot.broker == shown]
    running = [r for r in rows if r.bot.status != "archived"]
    put_in = sum(capital(r.bot) for r in running)
    taken = sum(r.bot.withdrawn_cash or 0.0 for r in running)
    equity = sum(r.equity for r in running)
    totals = BotTotals(capital=round(put_in, 2), added=round(sum(r.bot.added_cash or 0.0 for r in running), 2),
                       withdrawn=round(taken, 2), equity=round(equity, 2), pnl=round(equity + taken - put_in, 2),
                       active=sum(r.bot.status == "active" for r in running), count=len(running))
    sectors = Counter(s for r in rows for s in r.sectors)
    kinds = Counter(_kind(r.bot) for r in rows)
    if kind:
        rows = [r for r in rows if _kind(r.bot) in kind]
    if sector:
        rows = [r for r in rows if set(r.sectors) & set(sector)]
    if sort:
        rows = (sorted((r for r in rows if r.keys[sort] is not None), key=lambda r: r.keys[sort], reverse=order == "desc")
                + [r for r in rows if r.keys[sort] is None])
    if offset >= len(rows):  # the filters left fewer: the last page
        offset = (len(rows) - 1) // limit * limit if rows else 0
    live = {b["name"] for b in catalog() if b["live"]}
    page, live_setups = rows[offset:offset + limit], _live_setups(session)
    return BotTableOut(
        items=[_bot_out(session, r.bot, yahoo, live_setups) for r in page], total=len(rows), offset=offset, broker=shown,
        brokers=dict(brokers.most_common()), sectors=dict(sorted(sectors.items(), key=lambda kv: (-kv[1], kv[0]))),
        kinds=dict(kinds.most_common()), totals=totals, active=sum(b.status == "active" for b in bots),
        active_live=sum(b.status == "active" and b.broker in live for b in bots),
        brief=[BotBrief(id=b.id, strategy=b.strategy, symbol=b.symbol) for b in bots])


@app.get("/sectors", response_model=list[str])
def list_sectors():
    """The sector badges the dashboard offers when setting a bot's by hand (any other name works too)."""
    return badges.BADGES


@app.put("/bots/{bot_id}/sectors", response_model=BotOut)
def set_bot_sectors(bot_id: int, body: SectorsIn, session: Session = Depends(get_session)):
    """Sets a bot's sector badges by hand ([] = none), or with null hands them back to its symbols. Only a label:
    nothing about how the bot trades changes, so it works on any bot, archived ones too."""
    bot = _get_bot(session, bot_id)
    if body.sectors != bot.sectors:
        bot.sectors = body.sectors
        said = "automatic (from its symbols)" if body.sectors is None else ", ".join(body.sectors) or "none"
        log_event(session, bot.id, "params", f"Sector badges: {said}")
        session.commit()
    return _bot_out(session, bot)


@app.put("/bots/{bot_id}/rating", response_model=BotOut)
def set_bot_rating(bot_id: int, body: RatingIn, session: Session = Depends(get_session)):
    """Sets a bot's stars, 0 (not rated) to 5: the owner's favourites, for sorting the bot table. Only a label, so
    it works on any bot, archived ones too, and leaves no event: a few clicks while sorting would push the trades
    out of the recent activity."""
    bot = _get_bot(session, bot_id)
    if body.rating != bot.rating:
        bot.rating = body.rating
        session.commit()
    return _bot_out(session, bot)


@app.post("/bots/{bot_id}/setup/save", response_model=DipPresetOut)
def save_bot_setup(bot_id: int, body: BotSetupSave, session: Session = Depends(get_session)):
    """Save the tested setup a bot was started from (its Tested setup popup) as a saved setup of its kind, with its test
    results: pick it later to start a new bot, or to switch a running dip bot to it (Edit). From the bot's copy, so it
    works after the study is gone, and on any bot, archived ones too. A name that exists is replaced, as in the forms."""
    bot = _get_bot(session, bot_id)
    if not bot.setup:
        raise HTTPException(404, "the bot wasn't started from a tested setup")
    strategy = bot.setup["strategy"]
    try:  # the rules as a saved setup keeps them (one added since the test gets its default)
        config = PRESET_CONFIGS[strategy].model_validate(bot.setup["config"]).model_dump(mode="json")
    except ValidationError as e:
        raise HTTPException(422, f"its tested rules can't be saved: {e.errors()[0]['msg']}")
    return _save_preset(session, body.name, config, strategy, _saved_copy(bot.setup))


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
    if clash := _symbol_clash(session, body.broker, [symbol]):
        raise HTTPException(409, _clash_text(clash, body.broker))

    params = body.model_dump(exclude={"name", "symbol", "broker", "allocated_cash", "confirm_live"})
    bot = Bot(name=body.name or f"{symbol} {body.strategy}", symbol=symbol, broker=body.broker, status="active",
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
              f"Created {bot.name}: {symbol} on {bot.broker} with ${bot.allocated_cash:,.2f}, strategy {bot.strategy}, "
              f"min conf {bot.min_confidence}, every {bot.rebalance_days} trading days {DECIDE_AT_LABEL[bot.decide_at]}, "
              f"stop {bot.stop_pct:.1%} / target {bot.target_pct:.1%}", "warning" if info["live"] else "info")
    session.commit()
    return _bot_out(session, bot)


@app.post("/bots/rotation", response_model=BotOut, status_code=201)
def create_rotation_bot(body: RotationBotCreate, session: Session = Depends(get_session)):
    """A momentum rotation bot: holds the strongest `top_n` of the universe, rebalanced at each month's last close
    (rotation.py). A real-money broker needs confirm_live."""
    live = _usable_broker_or_422(body.broker, body.confirm_live)
    if clash := _symbol_clash(session, body.broker, body.universe, holdings_bot=True):
        raise HTTPException(409, _clash_text(clash, body.broker))
    setup = _new_bot_setup(session, body, "rotation")

    # The universe's prices today: they validate the symbols and start the benchmark (the universe held equally)
    now = utcnow()
    try:
        prices = rotation.universe_prices(body.universe, ny_date(now))
    except BrokerError as e:
        raise HTTPException(422, f"can't get prices for the universe: {e}")
    if missing := [s for s in body.universe if s not in prices]:
        raise HTTPException(422, f"no prices for {', '.join(missing)} (unknown symbols?)")

    params = body.model_dump(exclude={"name", "broker", "allocated_cash", "confirm_live", "setup_test_id", "setup_preset_id"})
    bot = Bot(name=body.name or f"Momentum top {body.top_n} of {len(body.universe)}", symbol="ROTATION",
              broker=body.broker, status="active", strategy=ROTATION, allocated_cash=body.allocated_cash,
              cash=body.allocated_cash, peak_equity=body.allocated_cash, benchmark_prices=prices,
              benchmark_price=body.allocated_cash, last_price=body.allocated_cash, last_price_at=now, setup=setup, **params)
    session.add(bot)
    session.flush()
    skip = f", skipping the latest {body.skip_months} month{'s' if body.skip_months > 1 else ''}" if body.skip_months else ""
    log_event(session, bot.id, "created",
              f"Created {bot.name}: momentum rotation on {bot.broker} with ${bot.allocated_cash:,.2f}"
              f"{' (REAL MONEY)' if live else ''}. Holds the strongest "
              f"{body.top_n} of {', '.join(body.universe)} by {body.lookback_months}-month momentum{skip}"
              f"{', only those that rose' if body.abs_filter else ''}; rebalances at each month's last close"
              f"{'; buys fractions of a share' if body.fractional else '; whole shares only'}"
              f"{'.' + _setup_text(setup) if setup else ''}", "warning" if live else "info", now)
    session.commit()
    return _bot_out(session, bot)


def _usable_broker_or_422(name: str, confirm_live: bool) -> bool:
    """A broker a new bot may use: known, configured, and a real-money one only with confirm_live. True = real money."""
    info = next((b for b in catalog() if b["name"] == name), None)
    if info is None:
        raise HTTPException(422, f"unknown broker '{name}'")
    if not info["available"]:
        raise HTTPException(422, f"broker '{name}' unavailable: {info['reason']}")
    if info["live"] and not confirm_live:
        raise HTTPException(422, "real-money bot: set confirm_live=true to confirm")
    return info["live"]


def _prices_or_422(symbols: list[str]) -> dict[str, float]:
    """Today's prices of these symbols (Yahoo's daily closes): they prove each symbol exists and has data."""
    try:
        prices = rotation.universe_prices(symbols, ny_date(utcnow()))
    except BrokerError as e:
        raise HTTPException(422, f"can't get prices for {', '.join(symbols)}: {e}")
    if missing := [s for s in symbols if s not in prices]:
        raise HTTPException(422, f"no prices for {', '.join(missing)} (unknown symbols?)")
    return prices


# A tested setup's numbers a bot keeps (the grid's columns and the popup's): not the curves or the per-symbol rows
SETUP_NUMBERS = ("total_return_pct", "buy_hold_return_pct", "max_drawdown_pct", "buy_hold_max_drawdown_pct", "num_trades",
                 "win_rate_pct", "stop_exits")


def _setup_snapshot(session: Session, test_id: int, strategy: str) -> dict:
    """A copy of the Test results test a new bot is started from (schemas.BotSetup): its name, study, rules and results
    now, so the bot keeps them when the study is replaced or deleted. strategy: the new bot's kind, "dip" or "rotation"."""
    test = session.get(DipTest, test_id)
    if test is None:
        raise HTTPException(422, f"tested setup {test_id} not found (its study may have been replaced): start the bot again from Test results")
    study = session.get(DipStudy, test.study_id)
    if study.strategy != strategy:
        raise HTTPException(422, f"tested setup {test.code} is a {study.strategy} setup, not a {strategy} one")
    return {"test_id": test.id, "code": test.code, "name": test.name, "study_id": study.id, "study": study.name,
            "strategy": study.strategy, "watchlist": test.watchlist, "score": test.score, "pick": test.pick, "note": test.note,
            "extra": test.extra, "periods": study.periods, "config": test.config,
            "results": {p: {k: r[k] for k in SETUP_NUMBERS if k in r} for p, r in (test.results or {}).items() if isinstance(r, dict)},
            "started_at": utcnow().isoformat()}


def _saved_copy(setup: dict) -> dict:
    """A bot's tested setup as a saved setup keeps it (DipPreset.setup): without when that bot got it."""
    return {k: v for k, v in setup.items() if k not in ("started_at", "switched")}


def _preset_setup(session: Session, preset_id: int, strategy: str, switched: bool = False) -> tuple[DipPreset, dict]:
    """A saved setup and the tested setup it was saved from, as a bot's copy (Bot.setup) starting now: for a new bot
    started from it, or a running one switched to it (switched). strategy: the bot's kind, "dip" or "rotation"."""
    preset = session.get(DipPreset, preset_id)
    if preset is None:
        raise HTTPException(422, f"saved setup {preset_id} not found (deleted?): pick it again")
    if preset.strategy != strategy:
        raise HTTPException(422, f"“{preset.name}” is a {STRATEGY_WORDS[preset.strategy]} setup, not a {STRATEGY_WORDS[strategy]} one")
    if not preset.setup:
        raise HTTPException(422, f"“{preset.name}” wasn't saved from a tested setup, so it has no test results to keep")
    return preset, {**preset.setup, "started_at": utcnow().isoformat(), **({"switched": True} if switched else {})}


def _new_bot_setup(session: Session, body: DipBotCreate | RotationBotCreate, strategy: str) -> dict | None:
    """The tested setup a new bot keeps: Test results' test, or the one a saved setup was saved from (None: neither)."""
    if body.setup_test_id:
        return _setup_snapshot(session, body.setup_test_id, strategy)
    if body.setup_preset_id:
        return _preset_setup(session, body.setup_preset_id, strategy)[1]
    return None


def _setup_text(setup: dict | None) -> str:
    return f" Started from the tested setup {setup['name']} ({setup['study']})." if setup else ""


@app.post("/bots/dip", response_model=BotOut, status_code=201)
def create_dip_bot(body: DipBotCreate, session: Session = Depends(get_session)):
    """A dip buyer: watches `symbols` and buys any that fell drop_pct during the last `lookback` days (or hours),
    selling it back up (dip.py). A real-money broker needs confirm_live. The watchlist can change while it runs."""
    live = _usable_broker_or_422(body.broker, body.confirm_live)
    if clash := _symbol_clash(session, body.broker, body.symbols, holdings_bot=True):
        raise HTTPException(409, _clash_text(clash, body.broker))
    setup = _new_bot_setup(session, body, "dip")
    prices = _prices_or_422(body.symbols)  # also the benchmark: the starting watchlist held in equal parts
    now = utcnow()
    params = body.model_dump(exclude={"name", "broker", "allocated_cash", "symbols", "confirm_live", "setup_test_id",
                                      "setup_preset_id"})
    bot = Bot(name=body.name or f"Dip buyer on {len(body.symbols)} symbol{'s' if len(body.symbols) > 1 else ''}",
              symbol="DIP", broker=body.broker, status="active", strategy=DIP, allocated_cash=body.allocated_cash,
              cash=body.allocated_cash, peak_equity=body.allocated_cash, universe=body.symbols, benchmark_prices=prices,
              benchmark_price=body.allocated_cash, last_price=body.allocated_cash, last_price_at=now, setup=setup, **params)
    session.add(bot)
    session.flush()
    for s in body.symbols:
        session.add(WatchItem(bot_id=bot.id, symbol=s, status="watching", added_at=now))
    log_event(session, bot.id, "created", f"Created {bot.name}: dip buyer on {bot.broker} with ${bot.allocated_cash:,.2f}"
              f"{' (REAL MONEY)' if live else ''}, watching {', '.join(body.symbols)}. {_dip_rules_text(bot)}{_setup_text(setup)}",
              "warning" if live else "info", now)
    session.commit()
    return _bot_out(session, bot)


def _dip_rules_text(bot: Bot) -> str:
    sell = "back at that price" if bot.target_mode == "reference" else f"{bot.rise_pct:.1%} above the buy"
    extras = [x for x in (f"waits to turn up {bot.rebound_pct:.1%} from the low" if bot.rebound else "",
                          f"sell after {bot.max_hold_days} trading days" if bot.max_hold_days else "",
                          "bearish news blocks a buy" if bot.news else "", "only in an uptrend" if bot.trend_filter else "",
                          "buys fractions of a share" if bot.fractional else "") if x]
    return (f"Checks every {bot.interval}; buys {dip.fall_label(bot)} under the {dip.window_label(bot)}, "
            f"{bot.max_positions} slots; sells {sell}, stop {bot.stop_pct:.1%} (then blacklisted"
            f"{f' for {bot.reenable_days} trading days' if bot.reenable_days else ''})"
            + (f"; {', '.join(extras)}" if extras else "") + ".")


def _dip_bot(session: Session, bot_id: int) -> Bot:
    bot = _get_bot(session, bot_id)
    if bot.strategy != DIP:
        raise HTTPException(422, "not a dip bot")
    if bot.status == "archived":
        raise HTTPException(409, "bot is archived")
    return bot


def _watch_item(session: Session, bot: Bot, symbol: str) -> WatchItem:
    item = session.scalar(select(WatchItem).where(WatchItem.bot_id == bot.id, WatchItem.symbol == symbol.upper()))
    if item is None:
        raise HTTPException(404, f"{symbol.upper()} isn't on the watchlist")
    return item


@app.patch("/bots/{bot_id}/dip", response_model=BotOut)
def update_dip_bot(bot_id: int, body: DipBotUpdate, session: Session = Depends(get_session)):
    """Change a dip buyer's rules (the news switch, the interval, the fall, ...) while it runs. From the next check on;
    a held position keeps the target and stop it was bought with."""
    with _Locked(bot_id):
        bot = _dip_bot(session, bot_id)
        fields = body.model_dump(exclude_unset=True, exclude_none=True)
        symbols, preset_id = fields.pop("symbols", None), fields.pop("setup_preset_id", None)
        merged = {k: fields.get(k, getattr(bot, k)) for k in ("interval", "lookback", "lookback_unit")}
        try:
            check_window(merged["interval"], merged["lookback"], merged["lookback_unit"])
        except ValueError as e:
            raise HTTPException(422, str(e))
        # Everything that can refuse comes first, so a refusal leaves the bot as it was
        preset, setup = _preset_setup(session, preset_id, "dip", switched=True) if preset_id else (None, None)
        items = dip.watchlist(session, bot) if symbols is not None else []
        new = [s for s in symbols if s not in {i.symbol for i in items}] if symbols is not None else []
        gone = [i for i in items if i.symbol not in symbols] if symbols is not None else []
        if new:
            if clash := _symbol_clash(session, bot.broker, new, exclude=bot.id, holdings_bot=True):
                raise HTTPException(409, _clash_text(clash, bot.broker))
            _prices_or_422(new)

        now = utcnow()
        changed = False
        changes = []
        for field, value in fields.items():
            old = getattr(bot, field)
            if old != value:
                setattr(bot, field, value)
                changes.append(f"{field} {old} → {value}")
        if changes:
            log_event(session, bot.id, "params", "Changed: " + ", ".join(changes), now=now)
            changed = True
        if new or gone:  # a symbol it holds stays held: it exits at its target or stop, like one removed by hand
            held = {h.symbol for h in holdings(session, bot)}
            for s in new:
                session.add(WatchItem(bot_id=bot.id, symbol=s, status="watching", added_at=now))
            for i in gone:
                session.delete(i)
            kept = [i.symbol for i in gone if i.symbol in held]
            log_event(session, bot.id, "params", "Watchlist: " + "; ".join(x for x in (
                new and f"added {', '.join(new)}", gone and f"removed {', '.join(i.symbol for i in gone)}") if x)
                + (f" (still held: {', '.join(kept)} exit{'s' if len(kept) == 1 else ''} at the target or stop)" if kept else ""),
                now=now)
            changed = True
        if setup and (not bot.setup or (bot.setup["test_id"], bot.setup["code"]) != (setup["test_id"], setup["code"])):
            bot.setup = setup
            log_event(session, bot.id, "params", f"Switched to the saved setup “{preset.name}”: its tested setup is now "
                      f"{setup['name']} ({setup['study']})", now=now)
            changed = True
        if changed:
            session.commit()
        return _bot_out(session, bot)


@app.post("/bots/{bot_id}/watchlist", response_model=BotOut)
def add_to_watchlist(bot_id: int, body: WatchSymbols, session: Session = Depends(get_session)):
    """Add symbols to a dip bot's watchlist while it runs: checked from the next check on."""
    with _Locked(bot_id):
        bot = _dip_bot(session, bot_id)
        have = {i.symbol for i in dip.watchlist(session, bot)}
        new = [s for s in body.symbols if s not in have]
        if not new:
            raise HTTPException(409, "already on the watchlist")
        if len(have) + len(new) > 60:
            raise HTTPException(422, "a watchlist holds at most 60 symbols")
        if clash := _symbol_clash(session, bot.broker, new, exclude=bot.id, holdings_bot=True):
            raise HTTPException(409, _clash_text(clash, bot.broker))
        _prices_or_422(new)
        now = utcnow()
        for s in new:
            session.add(WatchItem(bot_id=bot.id, symbol=s, status="watching", added_at=now))
        log_event(session, bot.id, "params", f"Watchlist: added {', '.join(new)}", now=now)
        session.commit()
        return _bot_out(session, bot)


@app.delete("/bots/{bot_id}/watchlist/{symbol}", response_model=BotOut)
def remove_from_watchlist(bot_id: int, symbol: str, session: Session = Depends(get_session)):
    """Stop watching a symbol. If the bot holds it, the position stays and still exits at its target or stop (or sell it
    now from the holdings); it just won't be bought again."""
    with _Locked(bot_id):
        bot = _dip_bot(session, bot_id)
        item = _watch_item(session, bot, symbol)
        held = any(h.symbol == item.symbol for h in holdings(session, bot))
        session.delete(item)
        log_event(session, bot.id, "params", f"Watchlist: removed {item.symbol}"
                  + (" (still held: it exits at its target or stop)" if held else ""))
        session.commit()
        return _bot_out(session, bot)


@app.post("/bots/{bot_id}/watchlist/{symbol}/enable", response_model=BotOut)
def enable_symbol(bot_id: int, symbol: str, session: Session = Depends(get_session)):
    """Take a symbol off the blacklist: the bot may buy its dips again from the next check."""
    with _Locked(bot_id):
        bot = _dip_bot(session, bot_id)
        item = _watch_item(session, bot, symbol)
        if item.status != "blacklisted":
            raise HTTPException(409, f"{item.symbol} isn't blacklisted")
        dip.reenable(item)
        log_event(session, bot.id, "params", f"{item.symbol} re-enabled by user")
        session.commit()
        return _bot_out(session, bot)


@app.post("/bots/{bot_id}/watchlist/{symbol}/blacklist", response_model=BotOut)
def blacklist_symbol(bot_id: int, symbol: str, session: Session = Depends(get_session)):
    """Blacklist a symbol by hand: no more buys until you re-enable it (a held position still exits as usual)."""
    with _Locked(bot_id):
        bot = _dip_bot(session, bot_id)
        item = _watch_item(session, bot, symbol)
        if item.status == "blacklisted":
            raise HTTPException(409, f"{item.symbol} is already blacklisted")
        dip.blacklist(session, bot, item, utcnow(), "by you")
        session.commit()
        return _bot_out(session, bot)


@app.post("/bots/{bot_id}/holdings/{symbol}/sell", response_model=OrderOut)
def sell_holding(bot_id: int, symbol: str, session: Session = Depends(get_session)):
    """Sell one of a dip bot's holdings at market now (manual). It stays on the watchlist: buyable again tomorrow."""
    with _Locked(bot_id):
        bot = _dip_bot(session, bot_id)
        now = utcnow()
        if not is_open(now):
            raise HTTPException(409, "market is closed; orders are only sent during regular hours")
        if any(o.symbol == symbol.upper() for o in open_orders(session, bot)):
            raise HTTPException(409, "an order for it is already pending")
        broker = _broker_or_400(bot)
        try:
            order = dip.sell_one(session, bot, broker, symbol.upper(), now)
        except ValueError as e:
            raise HTTPException(409, str(e))
        log_event(session, bot.id, "order", f"Sell {symbol.upper()} requested by user", now=now)
        session.commit()
        return order


# A rotation bot's universe and rules are fixed for its life, like a bot's symbol: create a new bot instead. Fractional
# shares only change how its next trades are sized
ROTATION_EDITABLE = {"name", "fee_pct", "slippage_pct", "max_drawdown_pct", "fractional"}


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
        fields = body.model_dump(exclude_unset=True, exclude_none=True)
        if bot.strategy == ROTATION and (fixed := sorted(set(fields) - ROTATION_EDITABLE)):
            raise HTTPException(422, f"a rotation bot can only change its name, slippage, fee, breaker and fractional shares, not "
                                     f"{', '.join(fixed)}; create a new bot for other settings")
        if bot.strategy == DIP and (other := sorted(set(fields) - ROTATION_EDITABLE - {"stop_pct"})):
            raise HTTPException(422, f"a dip bot's rules change through PATCH /bots/{bot_id}/dip, not {', '.join(other)}")
        if "fractional" in fields and bot.strategy not in (ROTATION, DIP):
            raise HTTPException(422, "a single-symbol bot buys whole shares only")
        changes = []
        for field, value in fields.items():
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
        bot.peak_equity = bot_equity(session, bot, bot.last_price or bot.entry_price)
    return _set_status(session, bot, ("paused",), "active", "Resumed by user")


@app.post("/bots/{bot_id}/archive", response_model=BotOut)
def archive_bot(bot_id: int, session: Session = Depends(get_session)):
    """Retire a bot. History is kept; there is deliberately no hard delete (audit trail)."""
    with _Locked(bot_id):  # locked: the "is it flat?" check must not race with a buy
        bot = _get_bot(session, bot_id)
        if bot.shares or holdings(session, bot) or open_orders(session, bot) or resting_stop(session, bot):
            raise HTTPException(409, "close the position (and let pending orders finish) before archiving")
        return _set_status(session, bot, ("active", "paused"), "archived", "Archived by user")


def _is_live(bot: Bot) -> bool:
    return next((b["live"] for b in catalog() if b["name"] == bot.broker), False)


CAPITAL_MAX = 10_000_000  # all the money put in one bot, like the most it can start with


@app.post("/bots/{bot_id}/capital", response_model=BotOut)
def add_bot_capital(bot_id: int, body: CapitalAdd, session: Session = Depends(get_session)):
    """Put more money in a running bot. It spends it by its own rules: a dip bot's slots (1/max_positions of the
    equity) are bigger from its next buy on, and with top_up its next check that can buy first brings each holding up to
    a full slot (dip.top_up); a rotation bot sizes its picks with it at its next rebalance; a single-symbol bot's next
    buy uses it. The return is measured on all the money put in from then on, and buy & hold gets the same money on the
    same day (trader.add_capital). On a real account, move the money to the broker yourself: no bot spends more than
    the account's buying power."""
    with _Locked(bot_id):
        bot = _get_bot(session, bot_id)
        if bot.status == "archived":
            raise HTTPException(409, "bot is archived")
        if body.top_up and bot.strategy != DIP:
            raise HTTPException(422, "only a dip bot tops up its holdings; a rotation bot resizes them at its next rebalance")
        live = _is_live(bot)
        if live and not body.confirm_live:
            raise HTTPException(422, "real-money bot: set confirm_live=true to confirm")
        if capital(bot) + body.amount > CAPITAL_MAX:
            raise HTTPException(422, f"at most ${CAPITAL_MAX:,.0f} in one bot; it has ${capital(bot):,.2f}")
        broker = _broker_or_400(bot)
        now = utcnow()
        add_capital(session, bot, body.amount, now)
        held = holdings(session, bot)
        if bot.strategy == DIP:
            slot = f"${bot_equity(session, bot, None) / bot.max_positions:,.2f} (1/{bot.max_positions} of the equity)"
            if body.top_up and held:
                bot.top_up = True
                how = (f"Its next check that can buy tops up its {len(held)} holding{'s' if len(held) > 1 else ''} to a "
                       f"full slot of about {slot}, then buys new dips with the same slots.")
            else:
                how = (("Nothing held to top up. " if body.top_up else "What it holds stays as it is. ")
                       + f"New buys get slots of about {slot}.")
        elif bot.strategy == ROTATION:
            how = f"Invested at its next rebalance: each pick 1/{bot.top_n} of the equity."
        else:
            how = f"Its next buy uses {bot.position_pct:.0%} of its cash."
        note = ""
        if bot.broker in SHARED_ACCOUNT_BROKERS:  # a real account: does it have the money?
            try:
                bp = broker.buying_power()
            except BrokerError as e:
                note = f" The account's buying power is unknown ({e})."
            else:
                cash = sum(b.cash for b in session.scalars(select(Bot).where(Bot.broker == bot.broker, Bot.status != "archived")))
                if bp is not None and bp < cash:
                    note = (f" The {bot.broker} account has ${bp:,.2f} of its own money to spend, less than the ${cash:,.2f} "
                            "its bots hold as cash: their buys stop there (never on borrowed money). Move the money to "
                            "the account first.")
        log_event(session, bot.id, "capital", f"Added ${body.amount:,.2f}{' of REAL MONEY' if live else ''}: "
                  f"${capital(bot):,.2f} put in (started with ${bot.allocated_cash:,.2f}). {how}{note}",
                  "warning" if live or note else "info", now)
        session.commit()
        return _bot_out(session, bot)


@app.get("/bots/{bot_id}/withdraw", response_model=WithdrawalPlanOut)
def withdrawal_plan(bot_id: int, amount: float = Query(gt=0, le=10_000_000), session: Session = Depends(get_session)):
    """What taking `amount` out would do now, at the bot's last prices (the dashboard's preview): nothing is sent and
    the broker isn't asked. The real one sizes its sales on fresh quotes, so they can differ a little."""
    bot = _get_bot(session, bot_id)
    p = withdraw.plan(session, bot, amount)
    why = withdraw.problem(session, bot, p, utcnow())
    return WithdrawalPlanOut(
        amount=amount, most=p.most, equity=p.equity, cash=p.cash, from_cash=p.from_cash, part=round(p.part, 6),
        raises=p.raises, left_in_cash=round(p.cash + p.raises - amount, 2), problem=why[0] if why else None,
        sales=[WithdrawalSaleOut(symbol=x.symbol, held=x.held, qty=x.qty, price=x.price, raises=round(x.raises(p.keep), 2),
                                 whole=x.whole) for x in p.sales])


@app.post("/bots/{bot_id}/withdraw", response_model=BotOut)
def withdraw_bot_capital(bot_id: int, body: CapitalWithdraw, session: Session = Depends(get_session)):
    """Take money out of a running bot: from its cash first (realized profit included), then by selling the same share
    of each holding at market now. What you put in stays; the return counts the money taken out, and buy & hold gives up
    the same money on the same day (withdraw.py has the rules). On Alpaca no money leaves the account: it becomes the
    account's free cash."""
    with _Locked(bot_id):
        bot = _get_bot(session, bot_id)
        if bot.status == "archived":
            raise HTTPException(409, "bot is archived")
        live = _is_live(bot)
        if live and not body.confirm_live:
            raise HTTPException(422, "real-money bot: set confirm_live=true to confirm")
        broker = _broker_or_400(bot)
        now = utcnow()
        try:
            story = withdraw.withdraw(session, bot, broker, body.amount, now, live)
        except withdraw.Refused as e:
            session.rollback()
            raise HTTPException(e.status, str(e))
        log_event(session, bot.id, withdraw.REASON, story, "warning" if live else "info", now)
        session.commit()
        return _bot_out(session, bot)


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
        if bot.strategy == ROTATION:
            return rotation.rebalance(session, bot, broker, now, kind)
        if bot.strategy == DIP:
            return dip.check(session, bot, broker, now, kind)
        return evaluate(session, bot, broker, now, kind)


@app.post("/bots/{bot_id}/close", response_model=OrderOut | list[OrderOut])
def close_now(bot_id: int, session: Session = Depends(get_session)):
    """Sell the whole position now at market (manual override; works on paused bots too). A rotation bot sells all
    its holdings and answers with one order per symbol."""
    with _Locked(bot_id):
        bot = _get_bot(session, bot_id)
        now = utcnow()
        if not is_open(now):
            raise HTTPException(409, "market is closed; orders are only sent during regular hours")
        if bot.strategy in (ROTATION, DIP):
            if not holdings(session, bot):
                raise HTTPException(409, "no open positions")
            if open_orders(session, bot):
                raise HTTPException(409, "orders are still pending; try again in a moment")
            broker = _broker_or_400(bot)
            log_event(session, bot.id, "order", "Close all positions requested by user", now=now)
            return rotation.close_all(session, bot, broker, now)
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


@app.get("/bots/{bot_id}/signals", response_model=list[SignalOut])
def list_bot_signals(bot_id: int, limit: int = Query(200, le=1000), after_id: int = 0, session: Session = Depends(get_session)):
    """A dip bot's recommendations, newest first. after_id: only newer ones (the dashboard's notifications poll this)."""
    _get_bot(session, bot_id)
    q = select(Signal).where(Signal.bot_id == bot_id, Signal.id > after_id).order_by(Signal.id.desc()).limit(limit)
    return list(session.scalars(q))


@app.get("/signals", response_model=list[SignalOut])
def list_signals(limit: int = Query(100, le=1000), after_id: int = 0, session: Session = Depends(get_session)):
    """Every dip bot's recommendations, newest first (the Dip buyer page's feed and notifications)."""
    q = select(Signal).where(Signal.id > after_id).order_by(Signal.id.desc()).limit(limit)
    return list(session.scalars(q))


STRATEGY_WORDS = {"dip": "dip buyer", "rotation": "momentum rotation"}


def _presets(session: Session, strategy: str) -> list[DipPreset]:
    return list(session.scalars(select(DipPreset).where(DipPreset.strategy == strategy).order_by(func.lower(DipPreset.name))))


def _save_preset(session: Session, name: str, config: dict, strategy: str, setup: dict | None = None) -> DipPreset:
    """Save a setup under a name; a name that exists (ignoring case) is replaced, unless it is the other strategy's.
    setup: the tested setup it is saved from (DipPreset.setup); None = from a form, and a replaced one loses its own."""
    preset = session.scalar(select(DipPreset).where(func.lower(DipPreset.name) == name.lower()))
    if preset is None:
        preset = DipPreset(name=name, strategy=strategy, config=config, setup=setup)
        session.add(preset)
    elif preset.strategy != strategy:
        raise HTTPException(409, f"a {STRATEGY_WORDS.get(preset.strategy, preset.strategy)} setup is called “{preset.name}”: "
                                 "pick another name")
    else:
        preset.name, preset.config, preset.setup, preset.updated_at = name, config, setup, utcnow()
    session.commit()
    return preset


def _test_copy(session: Session, test_id: int | None, strategy: str) -> dict | None:
    """The Test results test a setup is saved from, as the saved setup keeps it (None: not saved from a test)."""
    return _saved_copy(_setup_snapshot(session, test_id, strategy)) if test_id else None


def _delete_preset(session: Session, preset_id: int, strategy: str) -> None:
    preset = session.get(DipPreset, preset_id)
    if preset is None or preset.strategy != strategy:
        raise HTTPException(404, "saved setup not found")
    session.delete(preset)
    session.commit()


@app.get("/dip/presets", response_model=list[DipPresetOut])
def list_dip_presets(session: Session = Depends(get_session)):
    """The dip buyer setups you saved, by name."""
    return _presets(session, "dip")


@app.post("/dip/presets", response_model=DipPresetOut)
def save_dip_preset(body: DipPresetIn, session: Session = Depends(get_session)):
    """Save a dip buyer setup under a name. Saving under a name that exists (ignoring case) replaces that setup."""
    return _save_preset(session, body.name, body.config.model_dump(mode="json"), "dip", _test_copy(session, body.test_id, "dip"))


@app.delete("/dip/presets/{preset_id}", status_code=204)
def delete_dip_preset(preset_id: int, session: Session = Depends(get_session)):
    _delete_preset(session, preset_id, "dip")


@app.get("/rotation/presets", response_model=list[DipPresetOut])
def list_rotation_presets(session: Session = Depends(get_session)):
    """The momentum rotation setups you saved, by name."""
    return _presets(session, "rotation")


@app.post("/rotation/presets", response_model=DipPresetOut)
def save_rotation_preset(body: RotationPresetIn, session: Session = Depends(get_session)):
    """Save a momentum rotation setup under a name. Saving under a name that exists (ignoring case) replaces it."""
    return _save_preset(session, body.name, body.config.model_dump(mode="json"), "rotation",
                        _test_copy(session, body.test_id, "rotation"))


@app.delete("/rotation/presets/{preset_id}", status_code=204)
def delete_rotation_preset(preset_id: int, session: Session = Depends(get_session)):
    _delete_preset(session, preset_id, "rotation")


def _study_out(session: Session, study: DipStudy) -> DipStudyOut:
    n = session.scalar(select(func.count()).select_from(DipTest).where(DipTest.study_id == study.id))
    return DipStudyOut.model_validate(study).model_copy(update={"tests": n or 0})


@app.get("/dip/studies", response_model=list[DipStudyOut])
def list_dip_studies(strategy: StudyStrategy = "dip", session: Session = Depends(get_session)):
    """The test batches (parameter sweeps) of the dip buyer, or with strategy=rotation the momentum rotation's, newest
    first, each with how many setups it tested."""
    q = select(DipStudy).where(DipStudy.strategy == strategy).order_by(DipStudy.id.desc())
    return [_study_out(session, s) for s in session.scalars(q)]


@app.put("/dip/studies", response_model=DipStudyOut)
def save_dip_study(body: DipStudyIn, session: Session = Depends(get_session)):
    """Create a study, or replace the one with that name (ignoring case): its tests are dropped, add them again."""
    periods = {k: [v[0].isoformat(), v[1].isoformat()] for k, v in body.periods.items()}
    study = session.scalar(select(DipStudy).where(func.lower(DipStudy.name) == body.name.lower()))
    if study is None:
        study = DipStudy(name=body.name, strategy=body.strategy, description=body.description, periods=periods)
        session.add(study)
    else:
        session.execute(delete(DipTest).where(DipTest.study_id == study.id))
        study.name, study.strategy, study.description, study.periods, study.created_at = (
            body.name, body.strategy, body.description, periods, utcnow())
    session.commit()
    studies.forget(study.id)
    return _study_out(session, study)


def _get_study(session: Session, study_id: int) -> DipStudy:
    study = session.get(DipStudy, study_id)
    if study is None:
        raise HTTPException(404, "test batch not found")
    return study


@app.post("/dip/studies/{study_id}/tests", response_model=DipStudyOut)
def add_dip_tests(study_id: int, body: list[DipTestIn], session: Session = Depends(get_session)):
    """Add tested setups to a study (a code it already has is replaced). Each setup must be one of the study's
    strategy (a dip buyer's or a momentum rotation's, as a saved setup keeps it)."""
    study = _get_study(session, study_id)
    codes = [t.code for t in body]
    if len(set(codes)) != len(codes):
        raise HTTPException(422, "each test needs its own code")
    kind = PRESET_CONFIGS[study.strategy]
    rows = []
    for t in body:
        try:
            config = kind.model_validate(t.config).model_dump(mode="json")
        except ValidationError as e:
            raise HTTPException(422, f"{t.code}: not a {STRATEGY_WORDS[study.strategy]} setup: {e.errors()[0]['msg']}")
        rows.append(DipTest(study_id=study_id, **(t.model_dump(mode="json") | {"config": config})))
    session.execute(delete(DipTest).where(DipTest.study_id == study_id, DipTest.code.in_(codes)))
    session.add_all(rows)
    session.commit()
    studies.forget(study_id)
    return _study_out(session, study)


@app.get("/dip/studies/{study_id}/tests", response_model=DipTestPageOut)
def list_dip_tests(study_id: int, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                   sort: str = "pick", order: Literal["asc", "desc"] = "asc", q: str = Query("", max_length=100),
                   picks: bool = False, profit_both: bool = False, beat_both: bool = False, filters: str = "{}",
                   session: Session = Depends(get_session)):
    """One page of a study's tested setups (without their curves: GET /dip/tests/{id}), filtered and sorted by any
    column of the grid; by default the picks first, then the best score. `filters`: JSON {"watchlist": [names],
    "extra": [filters], "sector": [badges], "<rule>": [values]} (the values GET /dip/studies/{id}/summary lists; a
    test with any of the sectors stays); missing numbers sort last either way. Each test comes with its sector badges."""
    study = _get_study(session, study_id)
    if sort not in studies.sort_keys(list(study.periods), study.strategy):
        raise HTTPException(422, f"can't sort by {sort!r}")
    try:
        chosen = json.loads(filters)
        if not isinstance(chosen, dict) or not all(isinstance(v, list) for v in chosen.values()):
            raise ValueError
    except ValueError:
        raise HTTPException(422, 'filters: JSON like {"watchlist": ["ETFs"], "stop": [0.2]}')
    idx = studies.index(session, study)
    total, ids = studies.page(idx, offset, limit, sort, order, q, picks, profit_both, beat_both, chosen)
    found = {t.id: t for t in session.scalars(select(DipTest).where(DipTest.id.in_(ids)).options(defer(DipTest.detail)))}
    return DipTestPageOut(total=total, items=[
        DipTestOut.model_validate(found[i]).model_copy(update={"sectors": idx.sectors.get(i, [])}) for i in ids if i in found])


@app.get("/dip/studies/{study_id}/summary", response_model=DipStudySummaryOut)
def dip_study_summary(study_id: int, session: Session = Depends(get_session)):
    """The grid's frame for a study: the counts, each column's filter choices (the rules its tests differ in) and what
    matters: each rule's like-for-like effect on the score."""
    return studies.index(session, _get_study(session, study_id)).summary


@app.get("/dip/tests/{test_id}", response_model=DipTestDetailOut)
def get_dip_test(test_id: int, session: Session = Depends(get_session)):
    """One tested setup with its equity curves and per-symbol results."""
    test = session.get(DipTest, test_id)
    if test is None:
        raise HTTPException(404, "test not found")
    return test


@app.delete("/dip/studies/{study_id}", status_code=204)
def delete_dip_study(study_id: int, session: Session = Depends(get_session)):
    study = _get_study(session, study_id)
    session.execute(delete(DipTest).where(DipTest.study_id == study_id))  # SQLite doesn't cascade by itself
    session.delete(study)
    session.commit()
    studies.forget(study_id)


def _email_alerts_out(cfg: dict) -> EmailAlertsOut:
    return EmailAlertsOut(
        **cfg, smtp_configured=notify.smtp_ready(), smtp_server=notify.smtp_server(), sender=notify.sender(),
        dashboard_url=notify.dashboard_url(), available=[NotifyCategoryOut(key=k, label=label, description=d) for k, (label, d) in notify.CATEGORIES.items()])


@app.get("/email-alerts", response_model=EmailAlertsOut)
def get_email_alerts(session: Session = Depends(get_session)):
    """Who gets the email alerts and about what, and whether a mail server is set up (SMTP_* in .env)."""
    return _email_alerts_out(notify.load(session))


@app.put("/email-alerts", response_model=EmailAlertsOut)
def save_email_alerts(body: EmailAlertsIn, session: Session = Depends(get_session)):
    cfg = notify.save(session, body.model_dump(), utcnow())
    session.commit()
    return _email_alerts_out(cfg)


@app.post("/email-alerts/test", response_model=NotificationOut)
def send_test_email(session: Session = Depends(get_session)):
    """Send a test email to the saved addresses right away; a 502 carries the mail server's answer."""
    if not notify.smtp_ready():
        raise HTTPException(422, "no mail server set up: add SMTP_HOST (and the rest of SMTP_*) to trading-service/.env")
    recipients = notify.load(session)["recipients"]
    if not recipients:
        raise HTTPException(422, "save an address to send to first")
    try:
        return notify.send_test(session, recipients, utcnow())
    except Exception as e:
        raise HTTPException(502, f"the mail server refused: {type(e).__name__}: {e}")


@app.get("/risk-report", response_model=RiskReportOut)
def get_risk_report(session: Session = Depends(get_session)):
    """Today's risk report, built now (the market's and the sectors' news, their moves and links, the macro calendar):
    what the daily email says, without sending it. Takes a little while (a news verdict per sector, once a day)."""
    now = utcnow()
    report = risk_report.build(session, ny_date(now), now)
    session.commit()  # the sectors it looked up are kept
    return RiskReportOut(subject=report["subject"], text=report["text"], overall=report["overall"])


def _send_risk_report_now() -> None:
    try:
        with SessionLocal() as session:
            risk_report.queue(session, utcnow())
    except Exception:
        logger.exception("the risk report asked for on the dashboard failed")


@app.get("/risk-report/live")
def get_risk_report_live(session: Session = Depends(get_session)):
    """Today's risk report as data, for a reader that sorts it out itself (the user's pre-open routine): the market and
    the calendar, every sector's level, news and ETF moves, and each real-money bot's slots (held: sector, level, P&L,
    distance to the stop; free: the watched symbols it could buy, with their levels). Small: under 20 kB."""
    now = utcnow()
    r = risk_report.build(session, ny_date(now), now)
    session.commit()  # the sectors it looked up are kept
    return {"subject": r["subject"], "overall": r["overall"], "news_until": r["news_until"],
            "market": {"mood": r["market"].get("mood"), "reason": r["market"].get("reason")}, "macro": r["macro"],
            "sectors": r["sector_info"], "live_bots": r["live"], "paper_exposure": r["exposure"]["paper"]}


@app.post("/risk-report/send", status_code=202)
def send_risk_report(background: BackgroundTasks, session: Session = Depends(get_session)):
    """Build today's risk report and email it, whether or not the daily one is chosen or went out already. Answers at
    once; the report is built in the background (about a minute: each sector's news) and emailed when ready."""
    if not notify.smtp_ready():
        raise HTTPException(422, "no mail server set up: add SMTP_HOST (and the rest of SMTP_*) to trading-service/.env")
    cfg = notify.load(session)
    if not cfg["enabled"] or not cfg["recipients"]:
        raise HTTPException(422, "turn the email alerts on and save an address to send to first")
    background.add_task(_send_risk_report_now)
    return {"status": "building"}


@app.get("/email-alerts/history", response_model=list[NotificationOut])
def email_alert_history(limit: int = Query(50, le=500), session: Session = Depends(get_session)):
    """The latest email alerts, newest first: sent, waiting (a retry), failed or skipped; each with its bot's name and
    account, so a real-money one shows as such."""
    rows = list(session.scalars(select(Notification).order_by(Notification.id.desc()).limit(limit)))
    ids = {n.bot_id for n in rows if n.bot_id}
    bots = {b.id: b for b in session.scalars(select(Bot).where(Bot.id.in_(ids)))} if ids else {}
    out = []
    for n in rows:
        bot = bots.get(n.bot_id)
        out.append(NotificationOut.model_validate(n).model_copy(update={"bot_name": bot.name, "broker": bot.broker} if bot else {}))
    return out


@app.get("/events", response_model=list[EventOut])
def list_events(limit: int = Query(100, le=1000), broker: str | None = None, session: Session = Depends(get_session)):
    """Recent events across all bots (the overview page's activity feed); with a broker, only its bots' (no system
    events), so a quiet live bot's few events aren't pushed out of the latest `limit` by busy paper bots."""
    q = select(Event)
    if broker:
        q = q.join(Bot, Bot.id == Event.bot_id).where(Bot.broker == broker)
    return list(session.scalars(q.order_by(Event.id.desc()).limit(limit)))


@app.get("/summary")
def summary(session: Session = Depends(get_session)):
    """Counts for the overview header."""
    by_status = dict(session.execute(select(Bot.status, func.count()).group_by(Bot.status)).all())
    pending = session.scalar(select(func.count()).select_from(Order)
                             .where(Order.order_type == "market", Order.status.in_(OPEN_ORDER_STATUSES)))
    return {"bots_by_status": by_status, "pending_orders": pending}
