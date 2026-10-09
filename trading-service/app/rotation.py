"""Momentum rotation bots: one bot, a universe of symbols, holding the strongest few in equal parts.

The rules are the backtest's (backtest-service/app/engines/rotation.py), so the bot trades what was tested:
  - On the bot's first decision time and then on each month's last trading day, 30 min before the close: rank the
    universe by momentum (the return from lookback+skip months ago to skip months ago, on Yahoo's daily closes like
    the backtest) and pick the top `top_n`; with abs_filter only symbols that rose (an empty slot stays in cash).
  - Hold each pick at 1/top_n of equity: sell what dropped out, trim or top up what stays, buy what came in, at
    market. Sells go first; the buys wait until they've filled, so their cash is there.
  - Whole shares, or with `fractional` fractions of one where the broker can split the symbol ($1 or more). A pick
    that stays is only trimmed or topped up when the change is worth at least MIN_TRADE_OF_SLOT of its slot ($1 at
    least): a smaller trade would mostly pay slippage. The backtest sizes the same way.
Where it has to differ from the backtest:
  - The backtest assumes every symbol can be split; the bot buys whole shares of one its broker can't split.
  - A month end missed while the service was down is made up at the next decision time.
  - No stop-loss or take-profit (the backtest has none either). The drawdown breaker pauses the bot: no more
    rebalances, it keeps what it holds until you resume it or close the positions.
Paper accounts only for now (the built-in simulator or Alpaca paper): main.py refuses real money.

Like trader.py, every function takes `now` explicitly, and `closes_fn` (the daily price download) can be swapped,
so tests replay any moment without the network.
"""

import logging
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf
from sqlalchemy.orm import Session

from .brokers import Broker, BrokerError, Quote
from .config import settings
from .market import NY, month_end_after, ny_date, slot_window
from .models import Bot, Decision, Order
from .shares import MIN_FRACTIONAL_ORDER, affordable, fmt_qty, same_qty, tidy
from .trader import holdings, log_event, mark_to_market, open_orders, submit_order

logger = logging.getLogger("trading-service")

MIN_TRADE_OF_SLOT = 0.02
MAX_ROUNDS = 6  # rounds of orders one rebalance may send (sells, buys, retries of refused ones) before it gives up


# ---------------------------------------------------------------------------
# Prices and the ranking
# ---------------------------------------------------------------------------

def yahoo_closes(symbols: list[str], start: date) -> pd.DataFrame:
    """Daily closes from `start` to now, one column per symbol, like the backtest's download. During market hours
    the last row is today's latest price. A symbol Yahoo doesn't know comes back as an empty column."""
    yahoo = {s: s.replace(".", "-") for s in symbols}  # Yahoo writes share classes with a dash (BRK-B)
    try:
        df = yf.download(list(yahoo.values()), start=start.isoformat(), progress=False, auto_adjust=True)
    except Exception as e:  # yfinance raises many different things on network trouble
        raise BrokerError(f"daily prices failed: {e}") from e
    if df is None or df.empty:
        raise BrokerError("no daily prices from Yahoo (unknown symbols or data feed down)")
    close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": yahoo[symbols[0]]})
    close = close.rename(columns={y: s for s, y in yahoo.items()}).reindex(columns=symbols)
    close.index = pd.DatetimeIndex([pd.Timestamp(d.date()) for d in close.index])
    return close


def momentum(closes: pd.DataFrame, day: date, lookback_months: int, skip_months: int) -> pd.Series:
    """Each symbol's return from (lookback + skip) months before `day` to `skip` months before it, using only prices
    up to then (NaN without prices at both ends, or none on `day` itself). `closes` is forward-filled.
    The backtest's formula exactly (backtest-service/app/engines/rotation.py): keep the two the same."""
    t = pd.Timestamp(day)
    upto = closes.loc[:t]
    if upto.empty:
        return pd.Series(float("nan"), index=closes.columns)
    recent = upto.loc[:t - pd.DateOffset(months=skip_months)]
    past = upto.loc[:t - pd.DateOffset(months=lookback_months + skip_months)]
    if recent.empty or past.empty:
        return pd.Series(float("nan"), index=closes.columns)
    mom = recent.iloc[-1] / past.iloc[-1] - 1
    return mom.where(upto.iloc[-1].notna())


def rank(bot: Bot, today: date, closes_fn=None) -> tuple[list[str], list[tuple[str, float]], list[str]]:
    """The universe ranked by momentum on `today`: (the picks, [(symbol, momentum)] strongest first, the symbols
    without a momentum)."""
    closes_fn = closes_fn or yahoo_closes
    months = bot.lookback_months + bot.skip_months
    start = (pd.Timestamp(today) - pd.DateOffset(months=months) - pd.Timedelta(days=10)).date()
    closes = closes_fn(list(bot.universe), start).sort_index().ffill()
    mom = momentum(closes, today, bot.lookback_months, bot.skip_months)
    missing = [s for s in bot.universe if pd.isna(mom.get(s))]
    mom = mom.dropna().sort_values(ascending=False)
    picks = [s for s in mom.index if not bot.abs_filter or mom[s] > 0][:bot.top_n]
    return picks, [(s, float(mom[s])) for s in mom.index], missing


def universe_prices(universe: list[str], today: date, closes_fn=None) -> dict[str, float]:
    """Each symbol's latest daily close (today's latest price during market hours)."""
    closes = (closes_fn or yahoo_closes)(list(universe), today - timedelta(days=10)).sort_index().ffill()
    if closes.empty:
        return {}
    last = closes.iloc[-1]
    return {s: float(last[s]) for s in universe if pd.notna(last.get(s))}


def basket_value(bot: Bot, prices: dict[str, float]) -> float | None:
    """What the universe bought in equal parts when the bot started is worth now (the backtest's benchmark):
    allocated_cash x the average of each symbol's price / its start price. None while a price is missing."""
    start = bot.benchmark_prices or {}
    ratios = [prices[s] / p0 for s, p0 in start.items() if s in prices and p0]
    if not start or len(ratios) < len(start):
        return None
    return round(bot.allocated_cash * sum(ratios) / len(ratios), 2)


def _quote(broker: Broker, symbol: str, now: datetime, fresh: bool = True) -> Quote:
    q = broker.quote(symbol)
    if fresh and now - q.at > timedelta(minutes=settings.max_quote_age_minutes):
        raise BrokerError(f"quote for {symbol} is stale (last trade {q.at.astimezone(NY):%Y-%m-%d %H:%M} New York, "
                          f"over {settings.max_quote_age_minutes} min ago); not trading on stale data")
    return q


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------

def _close_window(day: date) -> tuple[datetime, datetime] | None:
    return slot_window(day, "close", settings.decision_minutes_after_open, settings.decision_minutes_before_close)


def _owed_from(bot: Bot) -> date | None:
    """The trading day from which the next rebalance is owed: the first month end after the last one. None = it
    never rebalanced: owed at once. Restart-safe: it comes from the database, not from memory."""
    return None if bot.last_decision_at is None else month_end_after(ny_date(bot.last_decision_at))


def due(bot: Bot, now: datetime) -> bool:
    """Should this bot rebalance right now? In the close window (15:30-16:00 New York) of its first day, of each
    month's last trading day, or of the next trading day after a missed one."""
    today = ny_date(now)
    window = _close_window(today)
    if window is None or not window[0] <= now < window[1]:
        return False
    owed = _owed_from(bot)
    return owed is None or owed <= today


def next_rebalance_at(bot: Bot, now: datetime) -> datetime | None:
    """When the scheduler will next rebalance this bot (the dashboard shows it). None unless active."""
    if bot.status != "active":
        return None
    owed = _owed_from(bot)
    day = ny_date(now) if owed is None else max(ny_date(now), owed)
    for _ in range(15):  # at most ~2 weeks of holidays in a row
        window = _close_window(day)
        if window is not None and window[1] > now:
            return max(window[0], now)
        day += timedelta(days=1)
    return None


# ---------------------------------------------------------------------------
# The rebalance
# ---------------------------------------------------------------------------

def _targets(bot: Bot, held: dict[str, float], picks: list[str], prices: dict[str, float], split: set[str]) -> list[list]:
    """[[symbol, shares], ...] for the picks, strongest first: 1/top_n of equity each, in shares that fit with
    slippage and fees (fractions of one for the symbols in `split`). Anything held that isn't a pick has a target
    of 0. The backtest's sizing (backtest-service/app/engines/rotation.py): keep the two the same."""
    equity = bot.cash + sum(n * prices[s] for s, n in held.items())
    slot = equity / bot.top_n
    out = []
    for s in picks:
        want = affordable(slot, prices[s] * (1 + bot.slippage_pct) * (1 + bot.fee_pct), s in split)
        have = held.get(s, 0)
        if have and abs(want - have) * prices[s] < max(MIN_TRADE_OF_SLOT * slot, MIN_FRACTIONAL_ORDER):
            want = have  # close enough: a tiny trim or top-up would mostly pay slippage
        out.append([s, want])
    return out


def _splits(bot: Bot, broker: Broker, symbols: list[str]) -> tuple[set[str], list[str]]:
    """With `fractional`: the symbols the broker can split into fractions of a share, and notes on the others (bought
    in whole shares). Without: none."""
    if not bot.fractional:
        return set(), []
    split, notes = set(), []
    for s in symbols:
        try:
            if broker.fractionable(s):
                split.add(s)
            else:
                notes.append(f"{s} can't be bought in fractions at {broker.name}: whole shares")
        except BrokerError as e:
            notes.append(f"{s}: fractions unknown ({e}), whole shares")
    return split, notes


def _changes(held: dict[str, float], targets: list[list]) -> list[str]:
    """The trades from `held` to `targets`, in words."""
    want = dict(targets)
    out = [f"sell all {fmt_qty(n)} {s}" if not want.get(s) else f"sell {fmt_qty(n - want[s])} {s} (trim)"
           for s, n in held.items() if n > want.get(s, 0) and not same_qty(n, want.get(s, 0))]
    for s, w in targets:
        have = held.get(s, 0)
        if w > have and not same_qty(w, have):
            out.append(f"buy {fmt_qty(w - have)} {s}" + (" (top up)" if have else ""))
    return out


def rebalance(session: Session, bot: Bot, broker: Broker, now: datetime, kind: str, closes_fn=None) -> Decision:
    """Rank the universe, choose the holdings and (unless kind == 'preview') start trading towards them.

    kind: 'scheduled' (the month end), 'manual' (Run now while the market is open), 'preview' (Run now while closed
    or paused: shows what it WOULD do, trades nothing). Marks the decision as made only once the ranking and the
    quotes worked, so a failed download is retried."""
    today = ny_date(now)
    d = Decision(bot_id=bot.id, session_date=today, kind=kind, action="HOLD", confidence=1.0, created_at=now)
    session.add(d)
    held = {h.symbol: h for h in holdings(session, bot)}

    try:
        picks, ranking, missing = rank(bot, today, closes_fn)
        if missing and (2 * len(missing) > len(bot.universe) or set(missing) & set(held)):
            raise BrokerError(f"no momentum for {', '.join(missing)}; not rebalancing on incomplete prices")
    except Exception as e:  # BrokerError, or a malformed download: never trade on a ranking we couldn't make
        d.outcome = f"Ranking failed, no trade: {e}"
        log_event(session, bot.id, "error", d.outcome, "error", now)
        session.commit()
        return d

    mom = dict(ranking)
    bought = [s for s in picks if s not in held]
    sold = [s for s in held if s not in picks]
    d.action = "ROTATE" if bought or sold else "HOLD"
    parts = [f"Hold {', '.join(f'{s} {mom[s]:+.1%}' for s in picks)}" if picks else "Hold nothing: no symbol rose"]
    if bought:
        parts.append(f"buy {', '.join(bought)}")
    if sold:
        parts.append(f"sell {', '.join(sold)}")
    if len(picks) < bot.top_n:
        parts.append(f"{bot.top_n - len(picks)} of {bot.top_n} slots stay in cash")
    d.reasoning = "; ".join(parts) + "." + (f" No prices for {', '.join(missing)}: left out." if missing else "")
    d.steps = [f"Momentum = the return from {bot.lookback_months + bot.skip_months} to {bot.skip_months} months ago"] + [
        f"{s} {m:+.1%}" + ("  ← hold" if s in picks else "") for s, m in ranking]
    session.flush()

    try:
        prices = {s: _quote(broker, s, now, fresh=kind != "preview").price for s in dict.fromkeys([*held, *picks])}
    except BrokerError as e:
        d.outcome = (f"Preview: it would hold {', '.join(picks) or 'nothing'} (no quotes to size the trades: {e})."
                     if kind == "preview" else f"No trade: {e}")
        if kind != "preview":
            log_event(session, bot.id, "error", d.outcome, "error", now)
        session.commit()
        return d
    for s, h in held.items():
        h.last_price, h.last_price_at = prices[s], now
    split, split_notes = _splits(bot, broker, picks)
    targets = _targets(bot, {s: h.shares for s, h in held.items()}, picks, prices, split)
    changes = _changes({s: h.shares for s, h in held.items()}, targets)
    if bot.fractional:
        d.steps = [*d.steps, "Fractions of a share" + (f" ({'; '.join(split_notes)})" if split_notes else "")]
    if small := [s for s, w in targets if not w]:
        slot = (bot.cash + sum(h.shares * prices[s] for s, h in held.items())) / bot.top_n
        d.reasoning += (f" A slot (${slot:,.2f}) is less than one share of {', '.join(small)}: that money stays in cash"
                        + ("." if bot.fractional else " (fractional shares would buy it)."))

    if kind == "preview":
        d.outcome = f"Preview only (market closed or bot paused): no order sent. It would {'; '.join(changes) or 'change nothing'}."
        session.commit()
        return d
    if open_orders(session, bot):
        d.outcome = "Skipped: orders from the last rebalance are still pending at the broker."
        session.commit()
        return d

    bot.last_decision_date, bot.last_decision_at = today, now
    bot.rotation_plan = {"decision_id": d.id, "targets": targets, "split": sorted(split), "rounds": 0}
    d.outcome = f"Trades: {'; '.join(changes)}." if changes else "No trades needed."
    try:
        step(session, bot, broker, now)
    except BrokerError as e:  # e.g. a stale quote for a buy: the scheduler keeps trying on its next ticks
        d.outcome += f" Paused for now: {e}"
        log_event(session, bot.id, "order", f"Rebalance waiting: {e}", "warning", now)
    session.commit()
    return d


def _next_orders(session: Session, bot: Bot, broker: Broker, now: datetime, targets: list[list],
                 split: set[str] = frozenset()) -> list[tuple]:
    """The orders still needed to reach the targets, as (side, symbol, shares, reason). All the sells first; the buys
    only once nothing is left to sell, capped by the cash (and a real account's buying power). Fractions of a share
    for the symbols in `split`."""
    want = dict(targets)
    held = {h.symbol: h.shares for h in holdings(session, bot)}
    sells = [("SELL", s, tidy(n - want.get(s, 0)) if want.get(s) else n, "rebalance" if want.get(s) else "rotation")
             for s, n in held.items() if n > want.get(s, 0) and not same_qty(n, want.get(s, 0))]
    if sells:
        return sells
    budget = bot.cash
    bp = broker.buying_power()
    if bp is not None:
        budget = min(budget, bp)
    buys = []
    for s, w in targets:
        need = tidy(w - held.get(s, 0))
        if need <= 0 or same_qty(w, held.get(s, 0)):
            continue
        unit = _quote(broker, s, now).price * (1 + bot.slippage_pct) * (1 + bot.fee_pct)
        qty = min(need, affordable(budget, unit, s in split))
        if qty > 0 and (s not in split or qty * unit >= MIN_FRACTIONAL_ORDER):
            buys.append(("BUY", s, qty, "rebalance" if held.get(s) else "rotation"))
            budget -= qty * unit
    return buys


def step(session: Session, bot: Bot, broker: Broker, now: datetime) -> None:
    """Trade towards the open plan: sells first, buys once they've filled. Runs right after the decision and on every
    scheduler tick while the plan is open (Alpaca fills take a moment); closes the plan once nothing is left to send.
    Raises BrokerError when a quote is missing or stale: the plan stays open and the next tick tries again."""
    plan = bot.rotation_plan
    if not plan:
        return
    decision = session.get(Decision, plan["decision_id"])
    while True:
        if open_orders(session, bot):
            return  # wait for the fills
        orders = _next_orders(session, bot, broker, now, plan["targets"], set(plan.get("split", [])))
        if not orders:
            bot.rotation_plan = None
            now_held = ", ".join(f"{fmt_qty(h.shares)} {h.symbol}" for h in holdings(session, bot)) or "nothing"
            if decision is not None:
                decision.outcome = f"{decision.outcome} Done: holding {now_held}, cash ${bot.cash:,.2f}."
            log_event(session, bot.id, "order", f"Rebalance done: holding {now_held}, cash ${bot.cash:,.2f}", now=now)
            return
        if plan["rounds"] >= MAX_ROUNDS:
            bot.rotation_plan = None
            todo = ", ".join(f"{side} {fmt_qty(qty)} {s}" for side, s, qty, _ in orders)
            log_event(session, bot.id, "error", f"Rebalance stopped after {MAX_ROUNDS} rounds of orders with {todo} still to "
                      "do. Check the orders tab; the next rebalance starts over.", "error", now)
            return
        plan = {**plan, "rounds": plan["rounds"] + 1}  # a new dict: SQLAlchemy only saves a JSON column it sees replaced
        bot.rotation_plan = plan
        for side, symbol, qty, reason in orders:
            submit_order(session, bot, broker, side, qty, reason, now, decision, symbol=symbol)


# ---------------------------------------------------------------------------
# Monitoring and manual control
# ---------------------------------------------------------------------------

def watch(session: Session, bot: Bot, broker: Broker, now: datetime, closes_fn=None) -> None:
    """Every few minutes in market hours: refresh the holdings' prices and the universe's value, record equity and
    let the drawdown breaker look at it."""
    for h in holdings(session, bot):
        q = broker.quote(h.symbol)
        h.last_price, h.last_price_at = q.price, q.at
    try:
        value = basket_value(bot, universe_prices(bot.universe, ny_date(now), closes_fn))
        if value is not None:
            bot.last_price, bot.last_price_at = value, now
    except Exception as e:  # the comparison line can wait; the holdings' own prices are what matter
        logger.warning("bot=%s universe prices failed: %s", bot.id, e)
    mark_to_market(session, bot, bot.last_price or bot.allocated_cash, now)


def reconcile(session: Session, bot: Bot, broker: Broker, now: datetime) -> bool:
    """Real (paper) accounts: does the account hold what the bot thinks, symbol by symbol? If not (a manual trade, a
    missed fill), pause instead of trading on a wrong picture. Always fine on the simulator."""
    if open_orders(session, bot):
        return True  # in flux; compare once the orders finish
    held = {h.symbol: h.shares for h in holdings(session, bot)}
    planned = [s for s, _ in (bot.rotation_plan or {}).get("targets", [])]
    for s in dict.fromkeys([*held, *planned]):
        qty = broker.position_qty(s)
        if qty is None:
            return True  # the simulator: nothing to compare with
        if not same_qty(qty, held.get(s, 0)):
            if bot.status == "active":
                bot.status = "paused"
                log_event(session, bot.id, "reconcile", f"Broker holds {fmt_qty(qty)} {s} but the bot's records say {fmt_qty(held.get(s, 0))}. "
                          "Paused: check the account (manual trades on this symbol?) before resuming.", "error", now)
            return False
    return True


def close_all(session: Session, bot: Bot, broker: Broker, now: datetime) -> list[Order]:
    """Sell every holding at market (manual override; works on paused bots too). The rebalance in progress is dropped,
    but an active bot buys again at its next rebalance: pause it first to stay in cash."""
    bot.rotation_plan = None
    return [submit_order(session, bot, broker, "SELL", h.shares, "manual", now, symbol=h.symbol)
            for h in holdings(session, bot)]
