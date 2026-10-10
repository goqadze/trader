"""What a bot actually does: turn a signal into orders, book fills, enforce stop-loss/target, protect capital.

The trading rules deliberately mirror the backtest engine (backtest-service/app/engines/simple.py) so
the live bot behaves like the backtest that convinced you:
  - BUY  signal, confidence >= min_confidence, currently flat  -> buy with position_pct of the bot's cash
  - SELL signal, confidence >= min_confidence, currently long  -> sell the whole position
  - stop-loss / target from the entry fill                      -> sell the whole position
  - long-only, whole shares, never more than the bot's own cash
Two things are stricter than the backtest because real money is involved:
  - stops/targets are checked every few minutes during the day, not only at the close
  - a quote older than MAX_QUOTE_AGE_MINUTES blocks trading (no acting on a frozen feed)
On brokers that can hold orders for us (Alpaca) the stop-loss also rests AT THE BROKER as a real stop
order (see protect), so it still fires while this service -- or the machine it runs on -- is down.
Several dip bots may trade one symbol on one real account (main._symbol_clash): each books only its own fills and sells
only its own shares; the account check adds up all their records (others_hold), and an order that would sell another bot's
shares or cross another bot's working order isn't sent (_refusal).

Every function takes `now` explicitly instead of reading the clock, so tests can replay any moment.
"""

import logging
import math
import threading
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import notify
from .brokers import SHARED_ACCOUNT_BROKERS, Broker, BrokerError, BrokerOrder, Quote
from .config import settings
from .decision_client import SignalError, get_signal
from .market import NY, ny_date
from .models import DIP, HOLDINGS_STRATEGIES, OPEN_ORDER_STATUSES, ROTATION, Bot, Decision, EquitySnapshot, Event, Holding, Order
from .shares import fmt_qty, same_qty, tidy

logger = logging.getLogger("trading-service")

# ---------------------------------------------------------------------------
# Per-bot lock: the scheduler and the API ("Run now", "Close position") must never
# work on the same bot at the same time, or both could decide to buy.
# ---------------------------------------------------------------------------
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def bot_lock(bot_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(bot_id, threading.Lock())


def log_event(session: Session, bot_id: int | None, kind: str, message: str, level: str = "info",
              now: datetime | None = None) -> None:
    """Append to the audit log (and the service log, so `docker compose logs` shows it too)."""
    ev = Event(bot_id=bot_id, kind=kind, message=message, level=level)
    if now is not None:
        ev.created_at = now
    session.add(ev)
    logger.log(logging.ERROR if level == "error" else logging.WARNING if level == "warning" else logging.INFO,
               "bot=%s %s: %s", bot_id, kind, message)
    notify.from_event(session, bot_id, kind, message, level, now)  # the important ones also go out by email


def open_orders(session: Session, bot: Bot) -> list[Order]:
    """Market orders still working at the broker: the position is in flux until they finish.
    A stop-loss order resting at the broker is NOT one of these (see resting_stop): it only waits."""
    return list(session.scalars(
        select(Order).where(Order.bot_id == bot.id, Order.order_type == "market", Order.status.in_(OPEN_ORDER_STATUSES))
    ))


def resting_stop(session: Session, bot: Bot) -> Order | None:
    """The stop-loss order waiting at the broker for this bot's position, if there is one."""
    return session.scalar(
        select(Order).where(Order.bot_id == bot.id, Order.order_type == "stop", Order.status.in_(OPEN_ORDER_STATUSES))
        .order_by(Order.id.desc()).limit(1)
    )


def holdings(session: Session, bot: Bot) -> list[Holding]:
    """A rotation or dip bot's positions (none for the other bots, whose one position lives on the Bot row)."""
    return list(session.scalars(select(Holding).where(Holding.bot_id == bot.id).order_by(Holding.symbol)))


def bot_equity(session: Session, bot: Bot, price: float | None) -> float:
    """Cash plus what the bot holds: its position at `price`, or a rotation or dip bot's holdings at their last prices."""
    if bot.strategy in HOLDINGS_STRATEGIES:
        return round(bot.cash + sum(h.shares * (h.last_price or h.cost_basis / h.shares) for h in holdings(session, bot)
                                    if h.shares), 2)
    return round(bot.cash + bot.shares * (price or 0.0), 2)


def others_hold(session: Session, bot: Bot, symbol: str) -> tuple[float, dict[int, float]] | None:
    """A real account several bots trade on: the shares of `symbol` the OTHER bots on it hold by their records, in all
    and per bot id. None while one of them has a market order for it working (the account is in flux: compare later).
    The simulator gives every bot its own account: (0, {})."""
    if bot.broker not in SHARED_ACCOUNT_BROKERS:
        return 0.0, {}
    mates = (Bot.broker == bot.broker, Bot.id != bot.id)  # the other bots on this account
    working = select(Order.id).join(Bot, Bot.id == Order.bot_id).where(
        *mates, Order.order_type == "market", Order.status.in_(OPEN_ORDER_STATUSES),
        func.coalesce(Order.symbol, Bot.symbol) == symbol)
    if session.scalar(working.limit(1)) is not None:
        return None
    per: dict[int, float] = {}
    for bot_id, shares in session.execute(select(Holding.bot_id, Holding.shares).join(Bot, Bot.id == Holding.bot_id)
                                          .where(*mates, Holding.symbol == symbol)):
        per[bot_id] = tidy(per.get(bot_id, 0.0) + shares)
    for bot_id, shares in session.execute(select(Bot.id, Bot.shares).where(  # single-symbol bots: their one position
            *mates, Bot.symbol == symbol, Bot.strategy.not_in(HOLDINGS_STRATEGIES), Bot.shares > 0)):
        per[bot_id] = tidy(per.get(bot_id, 0.0) + shares)
    return tidy(sum(per.values())), per


def _crossing(session: Session, bot: Bot, side: str, symbol: str) -> Order | None:
    """Another bot's order on the other side of `symbol` still working on this account: a market order, or (when
    buying) a stop-loss resting at the broker. Alpaca rejects an order while one like that is open (wash-trade
    protection)."""
    opposite = "SELL" if side == "BUY" else "BUY"
    types = ("market", "stop") if side == "BUY" else ("market",)
    return session.scalars(select(Order).join(Bot, Bot.id == Order.bot_id).where(
        Bot.broker == bot.broker, Bot.id != bot.id, Order.side == opposite, Order.order_type.in_(types),
        Order.status.in_(OPEN_ORDER_STATUSES), func.coalesce(Order.symbol, Bot.symbol) == symbol).limit(1)).first()


def _refusal(session: Session, bot: Bot, broker: Broker, side: str, qty: float, symbol: str) -> tuple[str, bool] | None:
    """On a real account several bots share, why an order mustn't go out, and whether that pauses the bot; else None.
    - Another bot's order on the other side of the symbol is still working: the broker would reject this one. Not sent;
      the bot tries again at its next check (its order normally fills within seconds).
    - A sale bigger than what the account holds beyond the other bots' shares (by their records): it would sell
      theirs, or sell short. This bot's records are off: paused, like the account check does."""
    if bot.broker not in SHARED_ACCOUNT_BROKERS:
        return None
    if (other := _crossing(session, bot, side, symbol)) is not None:
        what = "stop-loss order" if other.order_type == "stop" else other.side
        return (f"bot #{other.bot_id}'s {what} of {symbol} is still working at the broker, which rejects an order on "
                "the other side of the same symbol meanwhile; tries again at the next check", False)
    if side != "SELL":
        return None
    try:
        have = broker.position_qty(symbol)
    except BrokerError:
        return None  # can't ask now: the sale goes out (holding on to a position the bot wants out of is worse)
    if have is None:
        return None
    view = others_hold(session, bot, symbol)
    theirs = view[0] if view else 0.0  # another bot's order on it working: their records lag, only the account counts
    if qty <= have - theirs + 1e-6:
        return None
    whose = f", {fmt_qty(theirs)} of them other bots'" if theirs else ""
    return (f"the account holds {fmt_qty(have)} {symbol}{whose}: selling {fmt_qty(qty)} would "
            f"{'sell theirs' if qty <= have + 1e-6 else 'sell short'}", True)


def capital(bot: Bot) -> float:
    """What you put in: the starting capital plus any money added since (the return is measured against it)."""
    return round(bot.allocated_cash + (bot.added_cash or 0.0), 2)


def benchmark_value(bot: Bot, price: float | None) -> float | None:
    """What buy & hold is worth at `price` (the symbol's; a rotation or dip bot's basket value): the starting capital
    bought it at benchmark_price, and each addition bought benchmark_added more of it at that day's price."""
    if not bot.benchmark_price or not price:
        return None
    return round(price * (bot.allocated_cash / bot.benchmark_price + (bot.benchmark_added or 0.0)), 2)


def add_capital(session: Session, bot: Bot, amount: float, now: datetime) -> None:
    """Put `amount` more in the bot: its cash, what you put in, the drawdown peak (the breaker measures losses, not
    deposits) and the buy & hold baseline, which buys its basket with it at today's price. The bot spends it by its
    own rules: bigger slots for a dip bot's next buys, the next rebalance or buy for the others."""
    price = bot.last_price or (bot.allocated_cash if bot.strategy in HOLDINGS_STRATEGIES else None)
    if price and bot.benchmark_price:
        bot.benchmark_added = (bot.benchmark_added or 0.0) + amount / price
    bot.cash = round(bot.cash + amount, 2)
    bot.added_cash = round((bot.added_cash or 0.0) + amount, 2)
    bot.peak_equity = round(bot.peak_equity + amount, 2)
    if price:
        mark_to_market(session, bot, price, now)


def fresh_quote(broker: Broker, bot: Bot, now: datetime) -> Quote:
    """A quote we're willing to trade on. Also refreshes the bot's last known price."""
    q = broker.quote(bot.symbol)
    if now - q.at > timedelta(minutes=settings.max_quote_age_minutes):
        # Named by the last trade's time, not its age: the message stays the same while the quote stays stale,
        # so the audit log and error monitoring count one problem instead of a new one every few minutes
        raise BrokerError(f"quote for {bot.symbol} is stale (last trade {q.at.astimezone(NY):%Y-%m-%d %H:%M} New York, "
                          f"over {settings.max_quote_age_minutes} min ago); not trading on stale data")
    bot.last_price, bot.last_price_at = q.price, q.at
    return q


# ---------------------------------------------------------------------------
# Orders and fills
# ---------------------------------------------------------------------------

def submit_order(session: Session, bot: Bot, broker: Broker, side: str, qty: float, reason: str,
                 now: datetime, decision: Decision | None = None, stop_price: float | None = None,
                 symbol: str | None = None, reference_price: float | None = None) -> Order:
    """Write-ahead order submission (a market order, or a resting SELL stop when stop_price is given), for the
    bot's symbol or, on a rotation or dip bot, `symbol` (a dip buy carries its reference_price: the target to come):
    0. on a real account other bots trade on too, refuse what would cross them (_refusal: saved as rejected);
    1. save the order as "new" and COMMIT, before the broker hears about it;
    2. send it with our client_order_id;
    3. book whatever the broker answered.
    If we crash or the network drops between 1 and 3, the scheduler finds the "new" order and asks
    the broker what happened to it -- so an order can be neither lost nor sent twice."""
    symbol = symbol or bot.symbol
    order = Order(bot_id=bot.id, decision_id=decision.id if decision else None, side=side, symbol=symbol, qty=qty,
                  order_type="market" if stop_price is None else "stop", stop_price=stop_price, reference_price=reference_price,
                  reason=reason, status="new", client_order_id=f"bot{bot.id}-{uuid.uuid4().hex[:16]}",
                  created_at=now, updated_at=now)
    if refused := _refusal(session, bot, broker, side, qty, symbol):  # a real account other bots trade on too
        why, pause = refused
        order.status, order.error = "rejected", why
        session.add(order)
        if pause and bot.status == "active":
            bot.status = "paused"
        log_event(session, bot.id, "reconcile" if pause else "order", f"{_label(order, bot)} not sent: {why}"
                  + (". Paused: check the account before resuming." if pause else ""), "error" if pause else "info", now)
        session.commit()
        return order
    session.add(order)
    session.commit()
    try:
        if stop_price is None:
            result = broker.submit(symbol, side, qty, order.client_order_id)
        else:
            result = broker.submit_stop(symbol, qty, stop_price, order.client_order_id)
    except BrokerError as e:
        order.error = str(e)
        log_event(session, bot.id, "order", f"{_label(order, bot)}: submit uncertain ({e}); will reconcile",
                  "warning", now)
        session.commit()
        return order
    apply_broker_state(session, bot, order, result, now)
    session.commit()
    return order


def apply_broker_state(session: Session, bot: Bot, order: Order, bo: BrokerOrder, now: datetime) -> None:
    """Copy the broker's view onto our order; book the fill into the bot once the order is finished."""
    order.broker_order_id = bo.broker_order_id or order.broker_order_id
    if bo.status == "submitted":
        order.status = "submitted"  # still working at the broker; sync_pending() checks again next tick
        return
    if bo.filled_qty == 0:  # rejected / canceled without any fill: nothing to book
        order.status = bo.status if bo.status in ("rejected", "canceled") else "canceled"
        order.error = bo.error
        # Canceling a stop order is routine (it happens before every other sell); anything else deserves a look
        routine = order.order_type == "stop" and order.status == "canceled"
        log_event(session, bot.id, "order", f"{_label(order, bot)} {order.status}" + (f": {bo.error}" if bo.error else ""),
                  "info" if routine else "warning", now)
        return
    order.status = bo.status  # filled | partially_filled (finished with a partial fill)
    order.filled_qty = bo.filled_qty
    order.avg_price = bo.avg_price
    order.fee = bo.fee
    if bot.strategy in HOLDINGS_STRATEGIES:
        _book_holding_fill(session, bot, order, now)
        mark_to_market(session, bot, bot.last_price or bot.allocated_cash, now)  # its "price": the universe's value
    else:
        _book_fill(bot, order)
        bot.last_price, bot.last_price_at = bo.avg_price, now  # a fill is the freshest price we have
        mark_to_market(session, bot, bo.avg_price, now)
    how = ", stop order executed at the broker" if order.order_type == "stop" else ""
    log_event(session, bot.id, "order",
              f"{order.side} {fmt_qty(order.filled_qty)} {order.symbol or bot.symbol} @ ${order.avg_price:.2f} ({order.reason}{how})"
              + (f", P&L ${order.pnl:+.2f}" if order.pnl is not None else ""), now=now)
    notify.trade(session, bot, order, now)


def _label(order: Order, bot: Bot) -> str:
    """How an order is named in the audit log: 'BUY 10 AAPL' or 'stop SELL 10 AAPL @ $95.20'."""
    text = f"{order.side} {fmt_qty(order.qty)} {order.symbol or bot.symbol}"
    return f"stop {text} @ ${order.stop_price:.2f}" if order.order_type == "stop" else text


def _book_fill(bot: Bot, order: Order) -> None:
    """Update the bot's cash and position from a finished order (same accounting as the backtest)."""
    qty, price, fee = round(order.filled_qty), order.avg_price, order.fee  # these bots trade whole shares only
    if order.side == "BUY":
        cost = qty * price
        bot.cash = round(bot.cash - cost - fee, 2)
        bot.shares += qty
        bot.entry_price = price
        bot.cost_basis = round(cost + fee, 2)  # what it truly cost to get in
        # Stop/target come from the ACTUAL fill, so slippage can't silently widen the risk
        bot.stop_price = round(price * (1 - bot.stop_pct), 2)
        bot.target_price = round(price * (1 + bot.target_pct), 2)
    else:
        net = qty * price - fee
        # The slice of cost basis being sold (all of it for a full exit; a fraction for a partial fill)
        basis = bot.cost_basis * qty / bot.shares if bot.shares else 0.0
        order.pnl = round(net - basis, 2)  # true round-trip P&L: both fees and both slippages included
        bot.cash = round(bot.cash + net, 2)
        bot.realized_pnl = round(bot.realized_pnl + order.pnl, 2)
        bot.shares -= qty
        bot.cost_basis = round(bot.cost_basis - basis, 2)
        if bot.shares == 0:
            bot.entry_price = bot.stop_price = bot.target_price = None
            bot.cost_basis = 0.0


def _book_holding_fill(session: Session, bot: Bot, order: Order, now: datetime) -> None:
    """The same accounting for a rotation or dip bot, on the Holding of the order's symbol."""
    qty, price, fee = order.filled_qty, order.avg_price, order.fee
    h = session.scalar(select(Holding).where(Holding.bot_id == bot.id, Holding.symbol == order.symbol))
    if order.side == "BUY":
        if h is None:
            h = Holding(bot_id=bot.id, symbol=order.symbol, shares=0, cost_basis=0.0, opened_at=now)
            session.add(h)
        bot.cash = round(bot.cash - qty * price - fee, 2)
        if bot.strategy == DIP:
            _set_dip_levels(bot, h, qty, price, order.reference_price)
        h.shares = tidy(h.shares + qty)
        h.cost_basis = round(h.cost_basis + qty * price + fee, 2)
    else:
        if h is None or h.shares <= 0:  # can't happen through the bot itself; book the cash so it isn't lost
            bot.cash = round(bot.cash + qty * price - fee, 2)
            return
        net = qty * price - fee
        basis = h.cost_basis * min(qty, h.shares) / h.shares
        order.pnl = round(net - basis, 2)
        bot.cash = round(bot.cash + net, 2)
        bot.realized_pnl = round(bot.realized_pnl + order.pnl, 2)
        h.shares = tidy(h.shares - qty)
        h.cost_basis = round(h.cost_basis - basis, 2)
    h.last_price, h.last_price_at = price, now  # a fill is the freshest price we have
    if h.shares <= 0:
        session.delete(h)
    session.flush()


def _set_dip_levels(bot: Bot, h: Holding, qty: float, price: float, reference: float | None) -> None:
    """A dip bot's exit levels, from the ACTUAL fill (slippage can't quietly widen the risk), like the backtest: the stop
    stop_pct under the average buy, the target back at the reference the fall started from (or rise_pct above the buy).
    Called before the new shares are added to the holding."""
    entry = (h.shares * (h.entry_price or price) + qty * price) / (h.shares + qty)
    h.entry_price = round(entry, 4)
    if reference is not None:
        h.reference_price = round(reference, 2)
    h.stop_price = round(entry * (1 - bot.stop_pct), 2)
    if bot.target_mode == "percent" or h.reference_price is None:
        h.target_price = round(entry * (1 + (bot.rise_pct or bot.drop_pct or 0.05)), 2)
    else:
        h.target_price = h.reference_price


def sync_pending(session: Session, bot: Bot, broker: Broker, now: datetime) -> None:
    """Resolve orders we sent but haven't seen finish (slow fills, a crash mid-submit) -- and notice when the
    stop order resting at the broker has filled, which books that sale like any other."""
    unfinished = select(Order).where(Order.bot_id == bot.id, Order.status.in_(OPEN_ORDER_STATUSES))
    for order in session.scalars(unfinished).all():
        try:
            bo = broker.lookup(order.client_order_id)
        except BrokerError as e:
            logger.warning("bot=%s lookup %s failed: %s", bot.id, order.client_order_id, e)
            continue  # broker unreachable: try again next tick
        if bo is None:
            # The broker never got it. Give a grace period first: a just-sent order can take a moment to appear.
            if now - order.created_at > timedelta(minutes=2):
                order.status = "failed"
                log_event(session, bot.id, "order", f"{_label(order, bot)} never reached the broker; marked failed",
                          "warning", now)
            continue
        apply_broker_state(session, bot, order, bo, now)
    session.commit()


def close_position(session: Session, bot: Bot, broker: Broker, reason: str, now: datetime,
                   decision: Decision | None = None) -> Order | None:
    """Sell the whole position at market. Returns None when nothing was sent: already flat, or the stop order
    at the broker isn't confirmed canceled yet (it keeps protecting the position; try again later)."""
    if bot.shares <= 0:
        return None
    if not release_stop(session, bot, broker, now) or bot.shares <= 0:
        return None  # stop still resting -- or it filled first and the position is already gone
    return submit_order(session, bot, broker, "SELL", bot.shares, reason, now, decision)


# ---------------------------------------------------------------------------
# The stop-loss held at the broker
# ---------------------------------------------------------------------------

STOP_RETRY = timedelta(minutes=30)  # after the broker refuses a stop order, wait this long before trying again


def protect(session: Session, bot: Bot, broker: Broker, now: datetime) -> None:
    """Keep the stop-loss resting AT THE BROKER as a real stop order, for brokers that can hold one (Alpaca).

    Then the stop still fires if this service -- or the laptop/server it runs on -- is down, asleep or
    offline. Runs after every decision and on every scheduler tick; does nothing when the right stop is
    already resting. The take-profit stays with the service: a missed target costs an opportunity, not money.
    Bots on the built-in paper simulator keep the service's own stop check (see watch)."""
    if not broker.supports_stop_orders:
        return
    stop = resting_stop(session, bot)
    if bot.shares <= 0 or bot.stop_price is None:
        if stop is not None:
            # Flat, yet a sell stop is still out there: if it triggered it would sell shares we don't hold (a short)
            release_stop(session, bot, broker, now)
        return
    if open_orders(session, bot):
        return  # a market order is changing the position; protect the final quantity once it has finished
    if stop is not None:
        if stop.qty == bot.shares and stop.stop_price == bot.stop_price:
            return  # already protected
        # The position changed size (e.g. a partial fill): replace the stop with one for the new quantity
        if not release_stop(session, bot, broker, now) or bot.shares <= 0:
            return

    last = session.scalar(select(Order).where(Order.bot_id == bot.id, Order.order_type == "stop")
                          .order_by(Order.id.desc()).limit(1))
    if last is not None and last.status == "rejected" and now - last.created_at < STOP_RETRY:
        return  # refused recently; watch() checks the stop meanwhile
    try:
        held = broker.position_qty(bot.symbol)
    except BrokerError as e:
        logger.warning("bot=%s can't check the position before placing the stop: %s", bot.id, e)
        return  # try again next tick
    if held is not None and held < bot.shares:
        return  # never rest a sell for shares the account doesn't hold (a short); reconcile() pauses the bot

    order = submit_order(session, bot, broker, "SELL", bot.shares, "stop-loss", now, stop_price=bot.stop_price)
    if order.status == "submitted":
        log_event(session, bot.id, "risk", f"Stop-loss now held at the broker: SELL {fmt_qty(order.qty)} {bot.symbol} if it trades "
                  f"at ${bot.stop_price:.2f} or lower (works even while this service is down)", now=now)
    elif order.status == "rejected":
        log_event(session, bot.id, "risk", f"The broker refused the stop order; the service keeps checking the stop "
                  f"itself every {settings.risk_check_minutes} min and retries in {int(STOP_RETRY.total_seconds() // 60)} min",
                  "warning", now)
    session.commit()


def release_stop(session: Session, bot: Bot, broker: Broker, now: datetime) -> bool:
    """Cancel the resting stop order before any other sell. The broker reserves the shares for it, so another
    sell would be refused -- and if both filled, the position would be sold twice (leaving a short).
    True = no stop is left: go ahead. False = it may still be live: don't sell now."""
    stop = resting_stop(session, bot)
    if stop is None:
        return True
    try:
        bo = broker.cancel(stop.client_order_id)
    except BrokerError as e:
        log_event(session, bot.id, "order", f"Could not cancel the stop-loss order ({e}); sell postponed", "warning", now)
        session.commit()
        return False
    if bo is None:
        stop.status = "failed"  # the broker never had it, so there is nothing to cancel
        session.commit()
        return True
    if bo.status == "submitted":
        log_event(session, bot.id, "order", "The broker hasn't confirmed canceling the stop-loss order yet; sell "
                  "postponed (the stop still protects the position)", "warning", now)
        session.commit()
        return False
    apply_broker_state(session, bot, stop, bo, now)  # canceled -- or filled, if the stop triggered first: a real sale
    session.commit()
    return True


# ---------------------------------------------------------------------------
# Monitoring: prices, equity, stops, circuit breakers
# ---------------------------------------------------------------------------

def mark_to_market(session: Session, bot: Bot, price: float, now: datetime) -> None:
    """Record today's equity point and trip the drawdown breaker if the bot lost too much from its peak.
    `price`: the symbol's price; for a rotation bot the universe's value (its holdings carry their own prices)."""
    equity = bot_equity(session, bot, price)
    day = ny_date(now)
    snap = session.scalar(select(EquitySnapshot).where(EquitySnapshot.bot_id == bot.id, EquitySnapshot.day == day))
    if snap is None:
        snap = EquitySnapshot(bot_id=bot.id, day=day, equity=equity, cash=bot.cash, shares=bot.shares, price=price)
        session.add(snap)
    snap.equity, snap.cash, snap.shares, snap.price, snap.updated_at = equity, bot.cash, bot.shares, price, now
    snap.benchmark = benchmark_value(bot, price)

    bot.peak_equity = max(bot.peak_equity, equity)
    floor = bot.peak_equity * (1 - bot.max_drawdown_pct)
    if bot.status == "active" and bot.max_drawdown_pct > 0 and equity < floor:
        # Pause, don't panic-sell: the position keeps its stop-loss; you decide what happens next.
        bot.status = "paused"
        after = ("no more rebalances; it keeps what it holds" if bot.strategy == ROTATION
                 else "no more buys; its holdings' stop-loss and target are still checked" if bot.strategy == DIP
                 else "stop-loss still active")
        log_event(session, bot.id, "risk",
                  f"Drawdown breaker: equity ${equity:,.2f} is more than {bot.max_drawdown_pct:.0%} below its peak "
                  f"${bot.peak_equity:,.2f}. Bot paused; {after}.", "error", now)


def watch(session: Session, bot: Bot, broker: Broker, now: datetime) -> Order | None:
    """Periodic check during market hours: refresh the price, record equity, and exit on stop-loss/target."""
    if bot.shares <= 0:
        # Flat: nothing to protect and equity is just cash, so the price is only for display and any age will do.
        # (A thinly traded symbol can go an hour without a trade on the free IEX feed; that's no error here.)
        q = broker.quote(bot.symbol)
        bot.last_price, bot.last_price_at = q.price, q.at
        mark_to_market(session, bot, q.price, now)
        return None
    q = fresh_quote(broker, bot, now)
    mark_to_market(session, bot, q.price, now)
    if open_orders(session, bot):
        return None
    # A stop order resting at the broker owns the stop-loss: selling here as well could sell twice
    broker_holds_stop = resting_stop(session, bot) is not None
    if bot.stop_price is not None and q.price <= bot.stop_price and not broker_holds_stop:
        reason, level = "stop-loss", bot.stop_price
    elif bot.target_price is not None and q.price >= bot.target_price:
        reason, level = "target", bot.target_price
    else:
        return None
    log_event(session, bot.id, "risk", f"{bot.symbol} ${q.price:.2f} hit the {reason} (${level:.2f}); selling", now=now)
    return close_position(session, bot, broker, reason, now)


def reconcile(session: Session, bot: Bot, broker: Broker, now: datetime) -> bool:
    """Real accounts only: does the broker hold the shares we think we hold? If someone traded this
    symbol by hand (or a fill was missed), pause instead of trading on a wrong picture of reality."""
    if open_orders(session, bot):
        return True  # position is in flux; compare once the order finishes
    qty = broker.position_qty(bot.symbol)
    if qty is not None and not same_qty(qty, bot.shares) and resting_stop(session, bot) is not None:
        # The broker-held stop may have filled a moment ago: book that sale first, then compare again
        sync_pending(session, bot, broker, now)
        qty = broker.position_qty(bot.symbol)
    if qty is None or same_qty(qty, bot.shares):
        return True
    if bot.status == "active":
        bot.status = "paused"
        log_event(session, bot.id, "reconcile",
                  f"Broker holds {fmt_qty(qty)} {bot.symbol} but the bot's records say {bot.shares}. Paused: check the "
                  "account (manual trades on this symbol?) before resuming.", "error", now)
    return False


# ---------------------------------------------------------------------------
# The decision itself
# ---------------------------------------------------------------------------

def evaluate(session: Session, bot: Bot, broker: Broker, now: datetime, kind: str,
             signal_fn=get_signal) -> Decision:
    """Ask for a signal, save it, and (unless kind == 'preview') act on it.

    kind: 'scheduled' (the daily run), 'manual' (Run now while the market is open),
          'preview'   (Run now while closed or paused: shows what the bot WOULD do, trades nothing).
    Marks the decision as made (last_decision_at) only when the signal call succeeded, so a failed call
    gets retried; the scheduler reads it to know which of today's slots are done."""
    today = ny_date(now)
    decision = Decision(bot_id=bot.id, session_date=today, kind=kind, action="HOLD", created_at=now)
    session.add(decision)

    try:
        decision.price = broker.quote(bot.symbol).price  # informational; trades re-check freshness
    except BrokerError:
        decision.price = bot.last_price

    try:
        sig = signal_fn(bot, today, now)  # `now`: news published after this moment is never used
    except SignalError as e:
        decision.outcome = f"Signal failed, no trade: {e}"
        log_event(session, bot.id, "error", decision.outcome, "error", now)
        session.commit()
        return decision

    # Normalize what came back: Postgres enforces column lengths, and anything unexpected must mean "do nothing"
    action = str(sig.get("action", "HOLD")).upper().strip()
    decision.action = action if action in ("BUY", "SELL", "HOLD") else "HOLD"
    decision.confidence = max(0.0, min(1.0, float(sig.get("confidence") or 0.0)))
    decision.sentiment = str(sig.get("sentiment") or "")[:32]
    decision.reasoning = sig.get("reasoning") or ""
    decision.steps = sig.get("steps") or []
    session.flush()  # gives the decision an id so orders can point at it

    if kind == "preview":
        decision.outcome = "Preview only (market closed or bot paused): no order sent."
        session.commit()
        return decision

    bot.last_decision_date, bot.last_decision_at = today, now
    decision.outcome = _act(session, bot, broker, decision, now)
    protect(session, bot, broker, now)  # a new position gets its broker-held stop-loss at once
    if decision.price:
        mark_to_market(session, bot, bot.last_price or decision.price, now)
    session.commit()
    return decision


def _act(session: Session, bot: Bot, broker: Broker, d: Decision, now: datetime) -> str:
    """Apply the trading rule to one decision. Returns a human-readable outcome for the history table."""
    if open_orders(session, bot):
        return "Skipped: a previous order is still pending at the broker."
    below = d.confidence < bot.min_confidence
    if d.action == "BUY":
        if bot.shares > 0:
            return f"Already holding {bot.shares} sh; BUY ignored (one position at a time)."
        if below:
            return f"Confidence {d.confidence:.2f} below minimum {bot.min_confidence:.2f}; no trade."
        try:
            q = fresh_quote(broker, bot, now)
            budget = bot.cash * bot.position_pct
            bp = broker.buying_power()  # real account: never exceed what the account can actually pay
            if bp is not None:
                budget = min(budget, bp)
        except BrokerError as e:
            log_event(session, bot.id, "error", f"BUY skipped: {e}", "error", now)
            return f"BUY skipped: {e}"
        # Size so shares + slippage + fee fit the budget (same formula as the backtest)
        shares = math.floor(budget / (q.price * (1 + bot.slippage_pct) * (1 + bot.fee_pct)))
        if shares <= 0:
            return f"Budget ${budget:,.2f} can't buy one share at ${q.price:.2f}; no trade."
        return _describe(submit_order(session, bot, broker, "BUY", shares, "signal", now, d))
    if d.action == "SELL":
        if bot.shares == 0:
            return "Flat; nothing to sell (long-only)."
        if below:
            return f"Confidence {d.confidence:.2f} below minimum {bot.min_confidence:.2f}; keeping the position."
        order = close_position(session, bot, broker, "signal", now, d)
        if order is None:
            return ("The stop-loss order at the broker sold the position first." if bot.shares == 0 else
                    "SELL not sent: the broker hasn't confirmed canceling the stop-loss order yet (it still protects "
                    "the position). Use Close position to retry.")
        return _describe(order)
    return "HOLD: no trade."


def _describe(order: Order | None) -> str:
    if order is None:
        return "No order."
    if order.status in ("filled", "partially_filled"):
        return f"{order.side} {fmt_qty(order.filled_qty)} sh {order.status} @ ${order.avg_price:.2f}"
    if order.status in ("new", "submitted"):
        return f"{order.side} {fmt_qty(order.qty)} sh sent; waiting for the fill."
    return f"{order.side} {fmt_qty(order.qty)} sh {order.status}: {order.error or ''}".strip()
