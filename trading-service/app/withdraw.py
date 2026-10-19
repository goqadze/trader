"""Taking money out of a running bot (POST /bots/{id}/withdraw): the opposite of Add money (main.add_bot_capital).

Where the money comes from:
  - The bot's cash first: money it hasn't spent, realized profit included (a sale's money is cash once it has filled).
  - Only what the cash doesn't cover comes from its holdings: the same share of each one, sold at market right away (so
    it needs regular hours), and the bot keeps its mix. A dip or rotation bot with `fractional` sells fractions of a
    share (to a millionth, $1 at least) where the broker can split the symbol; elsewhere whole shares: each holding's
    share is rounded down, and what that leaves short is made up one share at a time from the holdings nearest to their
    next whole share (a 1-share holding is sold whole or kept). What a whole share raises beyond the amount stays in
    the bot's cash. The sales are sized on fresh quotes less the bot's own slippage and fee estimates.
  - A bot keeps at least MIN_LEFT, the least a bot can start with. To take everything out, close its positions and
    archive it: an archived bot's cash belongs to no bot (the account's free cash, GET /accounts).
What changes at once (take_out):
  - Its cash goes down by the amount (under zero for the seconds its sales take to fill), and "taken out"
    (Bot.withdrawn_cash) up by it.
  - What you put in stays the same, and the return counts the money taken out: (equity + taken out) / put in - 1
    (trader.return_pct). Taking money out never moves the return, and taking profit out never turns it into a loss
    (lowering "put in" instead would, and would reach zero once more than that came out). Buy & hold sells its basket
    for the same money at today's price (fewer units, benchmark_added) and is measured the same way, so the comparison
    stays fair.
  - The drawdown breaker's peak shrinks in the same proportion as the equity: it sees the same drawdown in %, so a
    withdrawal neither trips it nor resets it.
  - A dip bot's slots (1/max_positions of the equity) are smaller from its next buy; its holdings keep their stop,
    target and days held (selling part doesn't move the average buy). A holding sold whole isn't bought again before
    tomorrow, like a manual sale; a top-up still waiting from Add money is dropped (it would buy back what was just
    sold). A rotation bot's next rebalance sizes each pick at 1/top_n of the smaller equity. A single-symbol bot's stop
    order at the broker is canceled before the sale and placed again for the shares left (trader.protect).
Settling (settle): once the sales have all finished, if they raised less than estimated (a fill under the quote, or an
order that didn't fill), the bot's cash is under zero by that much. That part stays in the bot: less comes out, and the
cash never stays under zero. Sales still working when the request ends are settled by the scheduler's next pass.
Refused before anything changes (`problem`): an archived bot, an order of the bot still working (its cash and holdings
are in flux), a rebalance in progress, more than `most`, sales needed while the market is closed, no fresh quote. A
real-money bot needs confirm_live (main.py), like Add money.
On Alpaca no money leaves the account: the bot's share of it gets smaller and the rest is the account's free cash, to
withdraw at Alpaca or give to another bot.

Every function takes `now`, like trader.py, so tests replay any moment.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .brokers import SHARED_ACCOUNT_BROKERS, Broker, BrokerError
from .dip import end_fall
from .market import is_open, ny_date
from .models import DIP, HOLDINGS_STRATEGIES, ROTATION, Bot, Order, WatchItem
from .rotation import _quote
from .shares import MIN_FRACTIONAL_ORDER, QTY_DECIMALS, fmt_qty, same_qty, tidy
from .trader import (_describe, bot_equity, capital, holdings, log_event, mark_to_market, open_orders, protect,
                     release_stop, submit_order)

MIN_LEFT = 100.0  # a bot keeps at least this much: the least a bot can start with (schemas: allocated_cash >= 100)
REASON = "withdrawal"  # its sales' Order.reason
CENT = 0.005  # what counts as nothing left to raise (amounts are in cents)


class Refused(Exception):
    """Why a withdrawal can't happen now; nothing has changed. `status`: what the API answers with."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass
class Sale:
    """One holding's part of a withdrawal's sales."""

    symbol: str
    held: float  # shares the bot holds
    price: float  # the quote it is sized on
    whole: bool  # whole shares only (else fractions of one)
    want: float = 0.0  # its exact share of the sales, in shares
    qty: float = 0.0  # what it sells

    def raises(self, keep: float) -> float:
        return self.qty * self.price * keep


@dataclass
class Plan:
    """What taking `amount` out does: from_cash out of the cash, the rest from `sales`."""

    amount: float
    most: float  # the most the bot can give now
    equity: float
    cash: float  # its cash before
    from_cash: float
    part: float  # the share of the holdings' worth the sales must raise (0 = none)
    keep: float  # what a dollar of sales leaves after the bot's slippage and fee estimates
    held: int = 0  # how many positions it holds
    sales: list[Sale] = field(default_factory=list)

    @property
    def raises(self) -> float:
        return round(sum(s.raises(self.keep) for s in self.sales), 2)


def _keep(bot: Bot) -> float:
    return (1 - bot.slippage_pct) * (1 - bot.fee_pct)


def _ceil(qty: float) -> float:
    """Up to the next millionth of a share (float noise doesn't count)."""
    return math.ceil(qty * 10**QTY_DECIMALS - 1e-6) / 10**QTY_DECIMALS


def positions(session: Session, bot: Bot) -> list[tuple[str, float, float]]:
    """(symbol, shares, price) of what the bot holds, at the prices it has now."""
    if bot.strategy in HOLDINGS_STRATEGIES:
        return [(h.symbol, h.shares, h.last_price or h.cost_basis / h.shares) for h in holdings(session, bot) if h.shares > 0]
    return [(bot.symbol, bot.shares, bot.last_price or bot.entry_price or 0.0)] if bot.shares > 0 else []


def size(bot: Bot, held: list[tuple[str, float, float]], need: float, split: set[str]) -> tuple[float, list[Sale]]:
    """Sell the same share of every holding to raise `need` dollars after the bot's slippage and fee estimates: that
    share, and the sales. Fractions of a share for the symbols in `split`, whole shares elsewhere (the module docstring
    has the rounding). Never more than a holding."""
    keep = _keep(bot)
    sales = [Sale(s, n, p, s not in split) for s, n, p in held if n > 0 and p > 0]
    worth = sum(x.held * x.price for x in sales) * keep
    if need <= CENT or worth <= 0:
        return 0.0, []
    part = min(1.0, need / worth)
    for x in sales:
        x.want = x.held * part
        if x.whole:
            x.qty = min(x.held, math.floor(x.want + 1e-9))
        else:
            x.qty = min(x.held, _ceil(x.want))
            if 0 < x.qty * x.price < MIN_FRACTIONAL_ORDER:  # under the broker's smallest fractional order
                x.qty = min(x.held, _ceil(MIN_FRACTIONAL_ORDER / x.price))
    short = need - sum(x.raises(keep) for x in sales)
    for x in sorted((x for x in sales if x.whole), key=lambda x: x.qty - x.want):  # nearest to its next share first
        if short <= CENT:
            break
        if x.want > x.qty + 1e-9 and x.qty + 1 <= x.held + 1e-9:
            x.qty += 1
            short -= x.price * keep
    for x in sales:  # still short (a fraction of a share held in a symbol sold whole): the rest of a holding
        if short <= CENT:
            break
        if x.qty < x.held:
            short -= (x.held - x.qty) * x.price * keep
            x.qty = x.held
    for x in sales:
        x.qty = tidy(x.qty)
    return part, [x for x in sales if x.qty > 0]


def most(equity: float) -> float:
    """The most a bot worth `equity` can give: it keeps MIN_LEFT."""
    return max(0.0, round(equity - MIN_LEFT, 2))


def plan(session: Session, bot: Bot, amount: float, split: set[str] | None = None) -> Plan:
    """What taking `amount` out would do at the prices the bot has now: its holdings' last prices (withdraw puts fresh
    quotes there first). split: the symbols sold in fractions; None = all of a `fractional` dip or rotation bot's (a
    preview: the broker isn't asked; a symbol it can't split is sold in whole shares then)."""
    held = positions(session, bot)
    if split is None:
        split = {s for s, _, _ in held} if bot.fractional and bot.strategy in HOLDINGS_STRATEGIES else set()
    equity = bot_equity(session, bot, bot.last_price)
    from_cash = round(min(amount, max(bot.cash, 0.0)), 2)
    part, sales = size(bot, held, round(amount - from_cash, 2), split)
    return Plan(amount=amount, most=most(equity), equity=equity, cash=bot.cash, from_cash=from_cash, part=part,
                keep=_keep(bot), held=len(held), sales=sales)


def problem(session: Session, bot: Bot, p: Plan, now: datetime) -> tuple[str, int] | None:
    """Why the withdrawal of plan `p` can't happen now, and the HTTP status for it; None = it can."""
    if bot.status == "archived":
        return "bot is archived", 409
    if bot.rotation_plan:
        return "a rebalance is still in progress: try again once it's done", 409
    if open_orders(session, bot):
        return "an order of this bot is still working at the broker: try again once it has finished", 409
    if p.amount > p.most + CENT:
        has = f"it has ${p.equity:,.2f} (${p.cash:,.2f} of it cash) and keeps at least ${MIN_LEFT:,.0f} to trade with"
        return ((f"at most ${p.most:,.2f}: {has}" if p.most > 0 else f"nothing can come out: {has}")
                + ". To take everything out, close its positions and archive it"), 422
    if p.amount > p.from_cash + CENT and not p.sales:
        return f"its cash covers ${p.from_cash:,.2f} and it holds nothing to sell for the rest", 422
    if p.sales and not is_open(now):
        return (f"the market is closed: only its cash (${p.from_cash:,.2f}) can come out now. Selling part of its holdings "
                "for the rest needs regular hours"), 409
    return None


def _refresh(session: Session, bot: Bot, broker: Broker, now: datetime) -> set[str]:
    """Fresh quotes for what the bot holds (the sales are sized on them), and the symbols the broker sells in fractions
    on a `fractional` dip or rotation bot. Refused when a quote is missing or stale: nothing is sold on old prices."""
    try:
        if bot.strategy not in HOLDINGS_STRATEGIES:
            q = _quote(broker, bot.symbol, now)
            bot.last_price, bot.last_price_at = q.price, q.at
            return set()
        split = set()
        for h in holdings(session, bot):
            q = _quote(broker, h.symbol, now)
            h.last_price, h.last_price_at = q.price, q.at
            if bot.fractional:
                try:
                    if broker.fractionable(h.symbol):
                        split.add(h.symbol)
                except BrokerError:
                    pass  # unknown: whole shares
        return split
    except BrokerError as e:
        raise Refused(f"no fresh quote to size the sales ({e}): try again in a moment")


def take_out(session: Session, bot: Bot, amount: float, now: datetime) -> None:
    """Book `amount` out of the bot (back in when negative: what a withdrawal's sales didn't raise): its cash, the money
    taken out, buy & hold's units at today's price, the drawdown peak in proportion to the equity (module docstring)."""
    equity = bot_equity(session, bot, bot.last_price)
    price = bot.last_price or (bot.allocated_cash if bot.strategy in HOLDINGS_STRATEGIES else None)
    if price and bot.benchmark_price:
        bot.benchmark_added = (bot.benchmark_added or 0.0) - amount / price
    if equity > 0 and equity - amount > 0:
        bot.peak_equity = round(bot.peak_equity * (equity - amount) / equity, 2)
    bot.cash = round(bot.cash - amount, 2)
    bot.withdrawn_cash = round((bot.withdrawn_cash or 0.0) + amount, 2) or None
    if price:
        mark_to_market(session, bot, price, now)


def settle(session: Session, bot: Bot, now: datetime, quiet: bool = False) -> float | None:
    """Once a withdrawal's sales have all finished: what they raised less than booked (0 = all of it) stays in the bot,
    so its cash isn't left under zero. Returns that shortfall, or None when nothing was settled (no withdrawal waiting,
    or a sale still working). Called right after the sales (quiet: the withdrawal's own event says it) and by every
    scheduler pass after its order sync (scheduler.process_bot), which logs it."""
    if bot.withdrawing is None or any(o.reason == REASON for o in open_orders(session, bot)):
        return None
    asked, bot.withdrawing = bot.withdrawing, None
    short = round(min(max(-bot.cash, 0.0), asked), 2)
    if short > 0:
        take_out(session, bot, -short, now)
    if not quiet:
        if short > 0:
            log_event(session, bot.id, REASON, f"Withdrawal of ${asked:,.2f} settled: its sales raised ${short:,.2f} less "
                      f"than estimated (a fill under the quote, or one that didn't fill: see Orders), which stays in the "
                      f"bot. ${asked - short:,.2f} came out; cash ${bot.cash:,.2f}.", "warning", now)
        else:
            log_event(session, bot.id, REASON, f"Withdrawal of ${asked:,.2f} settled: its sales have filled; cash "
                      f"${bot.cash:,.2f}.", now=now)
    return short


def withdraw(session: Session, bot: Bot, broker: Broker, amount: float, now: datetime, live: bool = False) -> str:
    """Take `amount` out of the bot now (module docstring). Returns what happened, for the audit log; raises Refused
    before anything has changed."""
    settle(session, bot, now)  # one the scheduler hasn't settled yet: first, so this one starts from the real cash
    p = plan(session, bot, amount)
    if why := problem(session, bot, p, now):
        raise Refused(*why)
    if p.sales:
        p = plan(session, bot, amount, _refresh(session, bot, broker, now))
        if why := problem(session, bot, p, now):  # the fresh prices may say it's worth less now
            raise Refused(*why)
        if bot.strategy not in HOLDINGS_STRATEGIES:
            # The stop order at the broker holds the shares: cancel it first (placed again for what's left, below)
            if not release_stop(session, bot, broker, now):
                raise Refused("the broker hasn't confirmed canceling the stop-loss order yet (it still protects the "
                              "position): try again in a moment")
            p = plan(session, bot, amount, set())  # the stop may have filled first: then the cash is bigger
            if why := problem(session, bot, p, now):
                raise Refused(*why)

    take_out(session, bot, amount, now)
    dropped = False
    if p.sales:
        bot.withdrawing = amount
        dropped, bot.top_up = bool(bot.top_up), None
    sent: list[tuple[Sale, Order]] = []
    whole: list[str] = []
    for x in p.sales:
        if bot.strategy == DIP and same_qty(x.qty, x.held):
            whole.append(x.symbol)
            item = session.scalar(select(WatchItem).where(WatchItem.bot_id == bot.id, WatchItem.symbol == x.symbol))
            if item is not None:
                item.sold_on = ny_date(now)  # like a manual sale: not bought again before tomorrow
                end_fall(item)
        symbol = x.symbol if bot.strategy in HOLDINGS_STRATEGIES else None
        sent.append((x, submit_order(session, bot, broker, "SELL", x.qty, REASON, now, symbol=symbol)))
    short = settle(session, bot, now, quiet=True) if p.sales else 0.0
    if bot.strategy not in HOLDINGS_STRATEGIES:
        protect(session, bot, broker, now)  # the shares left get their stop order at the broker again
    return _story(session, bot, p, sent, short, whole, dropped, live)


def _sale(x: Sale, o: Order) -> str:
    if o.status in ("filled", "partially_filled"):
        return (f"{fmt_qty(o.filled_qty)} {x.symbol} @ ${o.avg_price:,.2f}" + (f" (of {fmt_qty(o.qty)})" if o.status ==
                "partially_filled" else ""))
    return f"{x.symbol} {_describe(o).rstrip('.')}"


def _story(session: Session, bot: Bot, p: Plan, sent: list[tuple[Sale, Order]], short: float | None, whole: list[str],
           dropped: bool, live: bool) -> str:
    """The withdrawal in words, for the audit log (and the email)."""
    head = f"Took out ${p.amount:,.2f}{' of REAL MONEY' if live else ''}"
    if not sent:
        parts = [f"{head} from its cash (${p.cash:,.2f})."]
    else:
        each = ("its position" if bot.strategy not in HOLDINGS_STRATEGIES else "each holding" if p.held > 1
                else "its holding")
        parts = [f"{head}: ${p.from_cash:,.2f} from its cash, ${p.amount - p.from_cash:,.2f} from selling about "
                 f"{p.part:.1%} of {each}.", "Sales: " + "; ".join(_sale(x, o) for x, o in sent) + "."]
        if short is None:
            working = sorted({x.symbol for x, o in sent if o.status in ("new", "submitted")})
            parts.append(f"The sale of {', '.join(working)} is still working at the broker: the withdrawal is settled once "
                         "it has finished (if the sales raise less than estimated, less comes out).")
        elif short > 0:
            parts.append(f"The sales raised ${short:,.2f} less than estimated, which stays in the bot: "
                         f"${p.amount - short:,.2f} came out.")
    equity = bot_equity(session, bot, bot.last_price)
    parts.append(f"Now ${equity:,.2f} (cash ${bot.cash:,.2f}), from ${capital(bot):,.2f} put in and "
                 f"${bot.withdrawn_cash or 0.0:,.2f} taken out in all: the return counts what was taken out.")
    if bot.strategy == DIP:
        parts.append(f"New buys get slots of about ${equity / bot.max_positions:,.2f} (1/{bot.max_positions} of the "
                     "equity); what it holds keeps its stop and target.")
        if whole:
            parts.append(f"Sold whole: {', '.join(whole)}, not bought again before tomorrow.")
        if dropped:
            parts.append("The top-up still waiting from Add money is dropped.")
    elif bot.strategy == ROTATION:
        parts.append(f"Its next rebalance sizes each pick at 1/{bot.top_n} of the equity.")
    else:
        parts.append(f"Its next buy uses {bot.position_pct:.0%} of its cash"
                     + ("; the shares left keep their stop and target." if bot.shares else "."))
    if bot.broker in SHARED_ACCOUNT_BROKERS:
        parts.append(f"No money leaves the {bot.broker} account: it is the account's free cash now, to withdraw at Alpaca "
                     "or give to another bot.")
    return " ".join(parts)
