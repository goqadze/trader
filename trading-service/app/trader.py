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

Every function takes `now` explicitly instead of reading the clock, so tests can replay any moment.
"""

import logging
import math
import threading
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .brokers import Broker, BrokerError, BrokerOrder, Quote
from .config import settings
from .decision_client import SignalError, get_signal
from .market import ny_date
from .models import OPEN_ORDER_STATUSES, Bot, Decision, EquitySnapshot, Event, Order

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


def open_orders(session: Session, bot: Bot) -> list[Order]:
    return list(session.scalars(
        select(Order).where(Order.bot_id == bot.id, Order.status.in_(OPEN_ORDER_STATUSES))
    ))


def fresh_quote(broker: Broker, bot: Bot, now: datetime) -> Quote:
    """A quote we're willing to trade on. Also refreshes the bot's last known price."""
    q = broker.quote(bot.symbol)
    age = now - q.at
    if age > timedelta(minutes=settings.max_quote_age_minutes):
        raise BrokerError(f"quote for {bot.symbol} is {int(age.total_seconds() // 60)} min old; not trading on stale data")
    bot.last_price, bot.last_price_at = q.price, q.at
    return q


# ---------------------------------------------------------------------------
# Orders and fills
# ---------------------------------------------------------------------------

def submit_order(session: Session, bot: Bot, broker: Broker, side: str, qty: int, reason: str,
                 now: datetime, decision: Decision | None = None) -> Order:
    """Write-ahead order submission:
    1. save the order as "new" and COMMIT, before the broker hears about it;
    2. send it with our client_order_id;
    3. book whatever the broker answered.
    If we crash or the network drops between 1 and 3, the scheduler finds the "new" order and asks
    the broker what happened to it -- so an order can be neither lost nor sent twice."""
    order = Order(bot_id=bot.id, decision_id=decision.id if decision else None, side=side, qty=qty,
                  reason=reason, status="new", client_order_id=f"bot{bot.id}-{uuid.uuid4().hex[:16]}",
                  created_at=now, updated_at=now)
    session.add(order)
    session.commit()
    try:
        result = broker.submit(bot.symbol, side, qty, order.client_order_id)
    except BrokerError as e:
        order.error = str(e)
        log_event(session, bot.id, "order", f"{side} {qty} {bot.symbol}: submit uncertain ({e}); will reconcile",
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
        log_event(session, bot.id, "order", f"{order.side} {order.qty} {bot.symbol} {order.status}: {bo.error or ''}",
                  "warning", now)
        return
    order.status = bo.status  # filled | partially_filled (finished with a partial fill)
    order.filled_qty = bo.filled_qty
    order.avg_price = bo.avg_price
    order.fee = bo.fee
    _book_fill(bot, order)
    bot.last_price, bot.last_price_at = bo.avg_price, now  # a fill is the freshest price we have
    mark_to_market(session, bot, bo.avg_price, now)
    log_event(session, bot.id, "order",
              f"{order.side} {order.filled_qty} {bot.symbol} @ ${order.avg_price:.2f} ({order.reason})"
              + (f", P&L ${order.pnl:+.2f}" if order.pnl is not None else ""), now=now)


def _book_fill(bot: Bot, order: Order) -> None:
    """Update the bot's cash and position from a finished order (same accounting as the backtest)."""
    qty, price, fee = order.filled_qty, order.avg_price, order.fee
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


def sync_pending(session: Session, bot: Bot, broker: Broker, now: datetime) -> None:
    """Resolve orders we sent but haven't seen finish (slow fills, or a crash mid-submit)."""
    for order in open_orders(session, bot):
        try:
            bo = broker.lookup(order.client_order_id)
        except BrokerError as e:
            logger.warning("bot=%s lookup %s failed: %s", bot.id, order.client_order_id, e)
            continue  # broker unreachable: try again next tick
        if bo is None:
            # The broker never got it. Give a grace period first: a just-sent order can take a moment to appear.
            if now - order.created_at > timedelta(minutes=2):
                order.status = "failed"
                log_event(session, bot.id, "order", f"{order.side} {order.qty} {bot.symbol} never reached the broker; marked failed",
                          "warning", now)
            continue
        apply_broker_state(session, bot, order, bo, now)
    session.commit()


def close_position(session: Session, bot: Bot, broker: Broker, reason: str, now: datetime,
                   decision: Decision | None = None) -> Order | None:
    if bot.shares <= 0:
        return None
    return submit_order(session, bot, broker, "SELL", bot.shares, reason, now, decision)


# ---------------------------------------------------------------------------
# Monitoring: prices, equity, stops, circuit breakers
# ---------------------------------------------------------------------------

def mark_to_market(session: Session, bot: Bot, price: float, now: datetime) -> None:
    """Record today's equity point and trip the drawdown breaker if the bot lost too much from its peak."""
    equity = round(bot.cash + bot.shares * price, 2)
    day = ny_date(now)
    snap = session.scalar(select(EquitySnapshot).where(EquitySnapshot.bot_id == bot.id, EquitySnapshot.day == day))
    if snap is None:
        snap = EquitySnapshot(bot_id=bot.id, day=day, equity=equity, cash=bot.cash, shares=bot.shares, price=price)
        session.add(snap)
    snap.equity, snap.cash, snap.shares, snap.price, snap.updated_at = equity, bot.cash, bot.shares, price, now

    bot.peak_equity = max(bot.peak_equity, equity)
    floor = bot.peak_equity * (1 - bot.max_drawdown_pct)
    if bot.status == "active" and bot.max_drawdown_pct > 0 and equity < floor:
        # Pause, don't panic-sell: the position keeps its stop-loss; you decide what happens next.
        bot.status = "paused"
        log_event(session, bot.id, "risk",
                  f"Drawdown breaker: equity ${equity:,.2f} is more than {bot.max_drawdown_pct:.0%} below its peak "
                  f"${bot.peak_equity:,.2f}. Bot paused; stop-loss still active.", "error", now)


def watch(session: Session, bot: Bot, broker: Broker, now: datetime) -> Order | None:
    """Periodic check during market hours: refresh the price, record equity, and exit on stop-loss/target."""
    q = fresh_quote(broker, bot, now)
    mark_to_market(session, bot, q.price, now)
    if bot.shares <= 0 or open_orders(session, bot):
        return None
    if bot.stop_price is not None and q.price <= bot.stop_price:
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
    if qty is None or qty == bot.shares:
        return True
    if bot.status == "active":
        bot.status = "paused"
        log_event(session, bot.id, "reconcile",
                  f"Broker holds {qty} {bot.symbol} but the bot's records say {bot.shares}. Paused: check the "
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
    Marks today as decided only when the signal call succeeded, so a failed call gets retried."""
    today = ny_date(now)
    decision = Decision(bot_id=bot.id, session_date=today, kind=kind, action="HOLD", created_at=now)
    session.add(decision)

    try:
        decision.price = broker.quote(bot.symbol).price  # informational; trades re-check freshness
    except BrokerError:
        decision.price = bot.last_price

    try:
        sig = signal_fn(bot, today)
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

    bot.last_decision_date = today
    decision.outcome = _act(session, bot, broker, decision, now)
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
        return _describe(close_position(session, bot, broker, "signal", now, d))
    return "HOLD: no trade."


def _describe(order: Order | None) -> str:
    if order is None:
        return "No order."
    if order.status in ("filled", "partially_filled"):
        return f"{order.side} {order.filled_qty} sh {order.status} @ ${order.avg_price:.2f}"
    if order.status in ("new", "submitted"):
        return f"{order.side} {order.qty} sh sent; waiting for the fill."
    return f"{order.side} {order.qty} sh {order.status}: {order.error or ''}".strip()
