"""Trading API: create bots (one symbol + one parameter set each, a momentum rotation across a universe, or a dip buyer
watching a list of symbols), control them, read their history.

The React dashboard reaches this through nginx at /api/trading/* (see frontend/nginx.conf), and nginx only
lets signed-in users through. Sign-in and accounts are in auth.py (/auth/*).
Interactive docs: http://localhost:8002/docs
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, defer

from . import auth, dip, notify, rotation, scheduler
from .brokers import SHARED_ACCOUNT_BROKERS, BrokerError, catalog, get_broker
from .config import settings
from .db import get_session, init_db, utcnow
from .market import is_open, next_decision_time, ny_date, session_bounds
from .models import (DIP, OPEN_ORDER_STATUSES, ROTATION, Bot, Decision, DipPreset, DipStudy, DipTest, EquitySnapshot, Event,
                     Notification, Order, Signal, WatchItem)
from .schemas import (BotCreate, BotOut, BotUpdate, CapitalAdd, DecisionOut, DipBotCreate, DipBotUpdate, DipPresetIn, DipPresetOut,
                      DipStudyIn, DipStudyOut, DipTestDetailOut, DipTestIn, DipTestOut,
                      EmailAlertsIn, EmailAlertsOut, EventOut, HoldingOut, NotificationOut, NotifyCategoryOut, OrderOut,
                      RotationBotCreate, SignalOut, SnapshotOut, StatusOut, WatchItemOut, WatchSymbols, check_window)
from .trader import (add_capital, benchmark_value, bot_equity, bot_lock, capital, close_position, evaluate, holdings,
                     log_event, open_orders, resting_stop)

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
    if settings.scheduler_enabled:  # the background work: the bots, and the email alerts they queue
        tasks = [asyncio.create_task(scheduler.run_forever()), asyncio.create_task(notify.run_forever())]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Trading Service", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=4096)  # a dip study's grid is a few MB of JSON
app.include_router(auth.router)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DECIDE_AT_LABEL = {"close": "before the close", "open": "after the open", "both": "after the open and before the close"}


def _get_bot(session: Session, bot_id: int) -> Bot:
    bot = session.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(404, "bot not found")
    return bot


def _bot_out(session: Session, bot: Bot) -> BotOut:
    price = bot.last_price or bot.entry_price or 0.0  # a rotation bot's "price": what its universe held equally is worth
    equity = bot_equity(session, bot, price)
    live = next((b["live"] for b in catalog() if b["name"] == bot.broker), False)
    now = utcnow()
    items = dip.watchlist(session, bot) if bot.strategy == DIP else []
    watched = {i.symbol for i in items}
    rows = []
    for h in holdings(session, bot):
        value = round(h.shares * (h.last_price or h.cost_basis / h.shares), 2)
        rows.append(HoldingOut(symbol=h.symbol, shares=h.shares, cost_basis=h.cost_basis, last_price=h.last_price,
                               last_price_at=h.last_price_at, value=value, unrealized_pnl=round(value - h.cost_basis, 2),
                               weight_pct=round(value / equity * 100, 1) if equity else 0.0, opened_at=h.opened_at,
                               entry_price=h.entry_price, reference_price=h.reference_price, target_price=h.target_price,
                               stop_price=h.stop_price, on_watchlist=bot.strategy != DIP or h.symbol in watched))
    held = {r.symbol for r in rows}

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
        "equity": equity,
        "capital": capital(bot),
        "return_pct": round((equity / capital(bot) - 1) * 100, 2),
        "unrealized_pnl": round(sum(r.unrealized_pnl for r in rows) if rows else
                                bot.shares * price - bot.cost_basis if bot.shares else 0.0, 2),
        "buy_hold_return_pct": round((bh / capital(bot) - 1) * 100, 2) if (bh := benchmark_value(bot, price)) else None,
        "pending_order": bool(open_orders(session, bot)),
        "stop_at_broker": resting_stop(session, bot) is not None,
        "next_decision_at": scheduler.next_decision_at(bot, now),
        "holdings": rows,
        "rebalancing": bot.rotation_plan is not None,
        "watchlist": watchlist,
    })


def _symbol_clash(session: Session, broker: str, symbols: list[str], exclude: int | None = None,
                  dip_bot: bool = False) -> tuple[str, Bot] | None:
    """On a real (paper) account, a bot that can't share one of these symbols with the new one: (symbol, that bot).
    Dip bots share symbols with each other (dip_bot=True: the new one is a dip bot): each books its own fills and sells
    only its own shares, the account check adds up all their records (rotation.reconcile), and no order crosses
    another bot's (trader._refusal). The others keep their symbols to themselves: a single-symbol bot's stop-loss rests
    at the broker as an open sell order, and Alpaca rejects any buy of that symbol while it's open (wash-trade
    protection); a rotation bot's rebalance isn't made for sharing. The simulator keeps every bot apart.
    exclude: a bot that may trade them (a dip bot adding symbols to its own watchlist)."""
    if broker not in SHARED_ACCOUNT_BROKERS:
        return None
    for other in session.scalars(select(Bot).where(Bot.broker == broker, Bot.status != "archived", Bot.id != (exclude or 0))):
        if dip_bot and other.strategy == DIP:
            continue  # two dip bots: fine
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
    return (f"bot #{other.id} already trades {symbol} on {broker}. On a real account only dip bots share symbols, with "
            "each other: a single-symbol bot's stop-loss waits at Alpaca as an open sell order (Alpaca rejects buys of "
            "that symbol meanwhile), and a rotation bot keeps its symbols to itself")


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
        late = max(TICK_LATE, timedelta(seconds=10 * settings.tick_seconds))
        if not st["running"]:
            problems.append("the scheduler isn't running: the bots don't check or trade")
        elif since is not None and utcnow() - since > late:
            minutes = int((utcnow() - since).total_seconds() // 60)
            problems.append(f"the scheduler hasn't finished a round for {minutes} minutes: the bots don't check or trade"
                            + (f" (last error: {st['last_error']})" if st["last_error"] else ""))
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
    (rotation.py). Paper accounts only for now."""
    info = next((b for b in catalog() if b["name"] == body.broker), None)
    if info is None:
        raise HTTPException(422, f"unknown broker '{body.broker}'")
    if not info["available"]:
        raise HTTPException(422, f"broker '{body.broker}' unavailable: {info['reason']}")
    if info["live"]:
        raise HTTPException(422, "rotation bots trade on paper accounts only for now: paper-trade it first")
    if clash := _symbol_clash(session, body.broker, body.universe):
        raise HTTPException(409, _clash_text(clash, body.broker))

    # The universe's prices today: they validate the symbols and start the benchmark (the universe held equally)
    now = utcnow()
    try:
        prices = rotation.universe_prices(body.universe, ny_date(now))
    except BrokerError as e:
        raise HTTPException(422, f"can't get prices for the universe: {e}")
    if missing := [s for s in body.universe if s not in prices]:
        raise HTTPException(422, f"no prices for {', '.join(missing)} (unknown symbols?)")

    params = body.model_dump(exclude={"name", "broker", "allocated_cash"})
    bot = Bot(name=body.name or f"Momentum top {body.top_n} of {len(body.universe)}", symbol="ROTATION",
              broker=body.broker, status="active", strategy=ROTATION, allocated_cash=body.allocated_cash,
              cash=body.allocated_cash, peak_equity=body.allocated_cash, benchmark_prices=prices,
              benchmark_price=body.allocated_cash, last_price=body.allocated_cash, last_price_at=now, **params)
    session.add(bot)
    session.flush()
    skip = f", skipping the latest {body.skip_months} month{'s' if body.skip_months > 1 else ''}" if body.skip_months else ""
    log_event(session, bot.id, "created",
              f"Created {bot.name}: momentum rotation on {bot.broker} with ${bot.allocated_cash:,.2f}. Holds the strongest "
              f"{body.top_n} of {', '.join(body.universe)} by {body.lookback_months}-month momentum{skip}"
              f"{', only those that rose' if body.abs_filter else ''}; rebalances at each month's last close"
              f"{'; buys fractions of a share' if body.fractional else '; whole shares only'}")
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


@app.post("/bots/dip", response_model=BotOut, status_code=201)
def create_dip_bot(body: DipBotCreate, session: Session = Depends(get_session)):
    """A dip buyer: watches `symbols` and buys any that fell drop_pct during the last `lookback` days (or hours),
    selling it back up (dip.py). A real-money broker needs confirm_live. The watchlist can change while it runs."""
    live = _usable_broker_or_422(body.broker, body.confirm_live)
    if clash := _symbol_clash(session, body.broker, body.symbols, dip_bot=True):
        raise HTTPException(409, _clash_text(clash, body.broker))
    prices = _prices_or_422(body.symbols)  # also the benchmark: the starting watchlist held in equal parts
    now = utcnow()
    params = body.model_dump(exclude={"name", "broker", "allocated_cash", "symbols", "confirm_live"})
    bot = Bot(name=body.name or f"Dip buyer on {len(body.symbols)} symbol{'s' if len(body.symbols) > 1 else ''}",
              symbol="DIP", broker=body.broker, status="active", strategy=DIP, allocated_cash=body.allocated_cash,
              cash=body.allocated_cash, peak_equity=body.allocated_cash, universe=body.symbols, benchmark_prices=prices,
              benchmark_price=body.allocated_cash, last_price=body.allocated_cash, last_price_at=now, **params)
    session.add(bot)
    session.flush()
    for s in body.symbols:
        session.add(WatchItem(bot_id=bot.id, symbol=s, status="watching", added_at=now))
    log_event(session, bot.id, "created", f"Created {bot.name}: dip buyer on {bot.broker} with ${bot.allocated_cash:,.2f}"
              f"{' (REAL MONEY)' if live else ''}, watching {', '.join(body.symbols)}. {_dip_rules_text(bot)}",
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
        merged = {k: fields.get(k, getattr(bot, k)) for k in ("interval", "lookback", "lookback_unit")}
        try:
            check_window(merged["interval"], merged["lookback"], merged["lookback_unit"])
        except ValueError as e:
            raise HTTPException(422, str(e))
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
        if clash := _symbol_clash(session, bot.broker, new, exclude=bot.id, dip_bot=True):
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
        live = next((b["live"] for b in catalog() if b["name"] == bot.broker), False)
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


@app.get("/dip/presets", response_model=list[DipPresetOut])
def list_dip_presets(session: Session = Depends(get_session)):
    """The dip buyer setups you saved, by name."""
    return list(session.scalars(select(DipPreset).order_by(func.lower(DipPreset.name))))


@app.post("/dip/presets", response_model=DipPresetOut)
def save_dip_preset(body: DipPresetIn, session: Session = Depends(get_session)):
    """Save a dip buyer setup under a name. Saving under a name that exists (ignoring case) replaces that setup."""
    config = body.config.model_dump(mode="json")
    preset = session.scalar(select(DipPreset).where(func.lower(DipPreset.name) == body.name.lower()))
    if preset is None:
        preset = DipPreset(name=body.name, config=config)
        session.add(preset)
    else:
        preset.name, preset.config, preset.updated_at = body.name, config, utcnow()
    session.commit()
    return preset


@app.delete("/dip/presets/{preset_id}", status_code=204)
def delete_dip_preset(preset_id: int, session: Session = Depends(get_session)):
    preset = session.get(DipPreset, preset_id)
    if preset is None:
        raise HTTPException(404, "saved setup not found")
    session.delete(preset)
    session.commit()


def _study_out(session: Session, study: DipStudy) -> DipStudyOut:
    n = session.scalar(select(func.count()).select_from(DipTest).where(DipTest.study_id == study.id))
    return DipStudyOut.model_validate(study).model_copy(update={"tests": n or 0})


@app.get("/dip/studies", response_model=list[DipStudyOut])
def list_dip_studies(session: Session = Depends(get_session)):
    """The dip buyer test batches (parameter sweeps), newest first, each with how many setups it tested."""
    return [_study_out(session, s) for s in session.scalars(select(DipStudy).order_by(DipStudy.id.desc()))]


@app.put("/dip/studies", response_model=DipStudyOut)
def save_dip_study(body: DipStudyIn, session: Session = Depends(get_session)):
    """Create a study, or replace the one with that name (ignoring case): its tests are dropped, add them again."""
    periods = {k: [v[0].isoformat(), v[1].isoformat()] for k, v in body.periods.items()}
    study = session.scalar(select(DipStudy).where(func.lower(DipStudy.name) == body.name.lower()))
    if study is None:
        study = DipStudy(name=body.name, description=body.description, periods=periods)
        session.add(study)
    else:
        session.execute(delete(DipTest).where(DipTest.study_id == study.id))
        study.name, study.description, study.periods, study.created_at = body.name, body.description, periods, utcnow()
    session.commit()
    return _study_out(session, study)


def _get_study(session: Session, study_id: int) -> DipStudy:
    study = session.get(DipStudy, study_id)
    if study is None:
        raise HTTPException(404, "test batch not found")
    return study


@app.post("/dip/studies/{study_id}/tests", response_model=DipStudyOut)
def add_dip_tests(study_id: int, body: list[DipTestIn], session: Session = Depends(get_session)):
    """Add tested setups to a study (a code it already has is replaced)."""
    study = _get_study(session, study_id)
    codes = [t.code for t in body]
    if len(set(codes)) != len(codes):
        raise HTTPException(422, "each test needs its own code")
    session.execute(delete(DipTest).where(DipTest.study_id == study_id, DipTest.code.in_(codes)))
    session.add_all(DipTest(study_id=study_id, **t.model_dump(mode="json")) for t in body)
    session.commit()
    return _study_out(session, study)


@app.get("/dip/studies/{study_id}/tests", response_model=list[DipTestOut])
def list_dip_tests(study_id: int, session: Session = Depends(get_session)):
    """A study's tested setups, best score first (without their curves: GET /dip/tests/{id})."""
    _get_study(session, study_id)
    q = (select(DipTest).where(DipTest.study_id == study_id).options(defer(DipTest.detail))  # the curves: MBs, not shown here
         .order_by(DipTest.score.desc().nulls_last(), DipTest.id))
    return list(session.scalars(q))


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


@app.get("/email-alerts/history", response_model=list[NotificationOut])
def email_alert_history(limit: int = Query(50, le=500), session: Session = Depends(get_session)):
    """The latest email alerts, newest first: sent, waiting (a retry), failed or skipped."""
    return list(session.scalars(select(Notification).order_by(Notification.id.desc()).limit(limit)))


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
