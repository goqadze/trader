"""Dip buyer bots: one bot watching a list of symbols, buying the ones that just fell and selling them once they're back.

The rules are the backtest's (backtest-service/app/engines/dip.py), so the bot trades what was tested. At every check --
the end of each 5/15/30/60-minute bar from the open (9:45, 10:00 ... 15:45 for 15 minutes), or once a day in the last
30 minutes before the close -- it goes through all its symbols:
  1. Exits, on fresh quotes: at or under a holding's stop (stop_pct below the buy) -> sell, and blacklist the symbol:
     no more buys until you re-enable it. At or over its target (the price the fall started from, or rise_pct above
     the buy) -> sell. Held max_hold_days trading days (when set) -> sell.
  2. Where each symbol stands: its reference -- the highest close of the last `lookback` days (or hours) of bars, or
     the close at that window's start -- and how far under it the price is. Falling into the buy zone (drop_pct or
     more under) is a "down" signal; an unheld dip that is back at its reference is an "up" signal.
  3. Buys (active bots only): symbols in the buy zone that aren't held, blacklisted, sold today or blocked by news
     today, deepest fall first, while slots are free. A slot = 1/max_positions of the equity, in whole shares (with
     `fractional`, fractions of one where the broker can split the symbol, $1 or more), never more than the cash.
     With `news` on, bearish news blocks the buy for the rest of the day; with `trend_filter`, only symbols whose
     50-day average is above their 200-day one are bought.
     With `rebound` (wait for the turn) a fall into the buy zone isn't bought yet: the bot follows it -- the reference
     it fell from, its lowest price since -- and buys once the price is rebound_pct above that low (bearish turned
     bullish, a "rebound" signal) while still under the reference. Back at the reference first = that dip is over.
Every signal is saved (Signal) for your information, whatever the bot did about it: the dashboard can notify you.
Where it differs from the backtest: real quotes (Yahoo's bars for the window, the broker's quote before each trade),
real fills, and a check runs at the first scheduler tick after its moment (within ~30 s). The stop is checked at
each check, not held at the broker: a fast fall between checks fills lower. A paused bot still checks its holdings'
exits at each interval (no buys), like the other bots' stop-loss. Paper accounts only for now: main.py refuses real
money.

Like trader.py and rotation.py, every function takes `now`, and the price download (`bars_fn`), the news (`news_fn`)
and the trend filter (`trend_fn`) can be swapped, so tests replay any moment without the network.
"""

import logging
import math
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import notify
from .brokers import Broker, BrokerError
from .config import settings
from .decision_client import get_news
from .market import NY, ny_date, session_bounds, sessions_since, slot_window
from .models import Bot, Decision, Holding, Order, Signal, WatchItem
from .rotation import _quote, yahoo_closes
from .shares import affordable, fmt_qty
from .trader import _describe, bot_equity, holdings, log_event, mark_to_market, open_orders, submit_order

logger = logging.getLogger("trading-service")

INTERVAL_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 390}
YAHOO_INTERVALS = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d"}
YAHOO_INTRADAY_DAYS = 59  # Yahoo keeps 5-, 15- and 30-minute bars for 60 days (hourly ones for 730)
TREND_HISTORY_DAYS = 320  # calendar days of daily closes for the trend filter's 200-day average
UNIT = {"days": "day", "hours": "hour"}


# ---------------------------------------------------------------------------
# The window and the reference price
# ---------------------------------------------------------------------------

def window_bars(bot: Bot) -> int:
    """The window in bars of the check interval (a session has 390 minutes). The backtest's DipConfig.window_bars."""
    if bot.interval == "1d":
        return bot.lookback
    per_day = math.ceil(390 / INTERVAL_MINUTES[bot.interval])
    if bot.lookback_unit == "days":
        return bot.lookback * per_day
    return max(1, math.ceil(bot.lookback * 60 / INTERVAL_MINUTES[bot.interval]))


def window_label(bot: Bot) -> str:
    """'5-day high', '3-hour start' ... for the signals' messages."""
    return f"{bot.lookback}-{UNIT[bot.lookback_unit]} {'high' if bot.drop_from == 'high' else 'start'}"


def history_start(bot: Bot, today: date) -> date:
    """The first day the price download needs for a full window (Yahoo's 60-day limit on 5- to 30-minute bars)."""
    days = bot.lookback if bot.lookback_unit == "days" else math.ceil(bot.lookback / 6.5)
    start = today - timedelta(days=int(days * 7 / 5) + 10)
    if bot.interval in ("5m", "15m", "30m"):
        start = max(start, today - timedelta(days=YAHOO_INTRADAY_DAYS))
    return start


def yahoo_bars(symbols: list[str], interval: str, start: date) -> pd.DataFrame:
    """Closes from `start` to now, one column per symbol. Intraday intervals: regular-hours bars indexed by the moment
    each CLOSED (New York time; the last one still forming); "1d": daily closes indexed by day (today's = the latest
    price during market hours). A symbol Yahoo doesn't know comes back as an empty column."""
    yahoo = {s: s.replace(".", "-") for s in symbols}  # Yahoo writes share classes with a dash (BRK-B)
    try:
        df = yf.download(list(yahoo.values()), start=start.isoformat(), interval=YAHOO_INTERVALS[interval], progress=False,
                         auto_adjust=True, prepost=False)
    except Exception as e:  # yfinance raises many different things on network trouble
        raise BrokerError(f"prices failed: {e}") from e
    if df is None or df.empty:
        raise BrokerError("no prices from Yahoo (unknown symbols or data feed down)")
    close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": yahoo[symbols[0]]})
    close = close.rename(columns={y: s for s, y in yahoo.items()}).reindex(columns=symbols)
    if interval == "1d":
        close.index = pd.DatetimeIndex([pd.Timestamp(d.date()) for d in close.index])
    else:
        idx = close.index if close.index.tz is not None else close.index.tz_localize("UTC")
        close.index = idx.tz_convert(NY) + pd.Timedelta(minutes=INTERVAL_MINUTES[interval])
    return close.sort_index()


def stand(series: pd.Series | None, bot: Bot, moment: datetime, today: date) -> tuple[float | None, float | None]:
    """(reference, latest price) of one symbol at a check. The reference uses the bars that had closed before the
    current one, like the backtest's `references`: the highest of the last window_bars closes, or the first of them.
    None while there aren't that many (a new listing, or a feed gap)."""
    if series is None:
        return None, None
    s = series.dropna()
    if s.empty:
        return None, None
    price = float(s.iloc[-1])
    if bot.interval == "1d":
        hist = s[[d < today for d in s.index.date]]
    else:  # the bar that just closed is "now"; the window is the ones before it
        hist = s[s.index <= moment - timedelta(minutes=INTERVAL_MINUTES[bot.interval])]
    if len(hist) and hist.index[-1] == s.index[-1]:
        hist = hist.iloc[:-1]  # out of hours (a preview) the latest bar IS the price: it can't be its own reference
    window = hist.tail(window_bars(bot))
    if len(window) < window_bars(bot):
        return None, price
    return float(window.max() if bot.drop_from == "high" else window.iloc[0]), price


_trend_cache: dict[tuple[str, date], bool] = {}


def uptrend(symbols: list[str], today: date) -> dict[str, bool]:
    """Was each symbol in a long-term uptrend at yesterday's close: its 50-day average above its 200-day one? (Fewer
    than 200 days of history: no.) The backtest's `uptrend`. Daily closes from Yahoo, once per symbol and day."""
    need = [s for s in symbols if (s, today) not in _trend_cache]
    if need:
        closes = yahoo_closes(need, today - timedelta(days=TREND_HISTORY_DAYS))
        closes = closes[[d < today for d in closes.index.date]]
        if len(_trend_cache) > 5000:
            _trend_cache.clear()
        for s in need:
            c = closes[s].dropna() if s in closes else pd.Series(dtype=float)
            _trend_cache[(s, today)] = bool(len(c) >= 200 and c.tail(50).mean() > c.tail(200).mean())
    return {s: _trend_cache[(s, today)] for s in symbols}


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------

def _moments(bot: Bot, day: date) -> list[datetime]:
    """The day's check moments (UTC), in order. Intraday intervals: the end of each bar from the open, before the close
    (an order can't go out at 16:00). "1d": the start of the close window (15:30). Empty on a non-trading day."""
    if bot.interval == "1d":
        w = slot_window(day, "close", settings.decision_minutes_after_open, settings.decision_minutes_before_close)
        return [] if w is None else [w[0]]
    bounds = session_bounds(day)
    if bounds is None:
        return []
    open_, close = bounds
    step = timedelta(minutes=INTERVAL_MINUTES[bot.interval])
    out, t = [], open_ + step
    while t < close:
        out.append(t)
        t += step
    return out


def check_moment(bot: Bot, now: datetime) -> datetime | None:
    """The check this moment belongs to: the latest of today's moments at or before `now`, while the session (or the
    close window) lasts. None before the first one, after the close, and on holidays."""
    day = ny_date(now)
    past = [m for m in _moments(bot, day) if m <= now]
    if not past:
        return None
    bounds = session_bounds(day)
    return past[-1] if bounds and now < bounds[1] else None


def due(bot: Bot, now: datetime) -> bool:
    """Has a check moment passed that this bot hasn't checked since? Restart-safe: from Bot.last_decision_at. After a
    long outage it checks once, at the latest moment, not once per missed one."""
    moment = check_moment(bot, now)
    return moment is not None and (bot.last_decision_at is None or bot.last_decision_at < moment)


def next_check_at(bot: Bot, now: datetime) -> datetime | None:
    """When the scheduler next checks this bot (the dashboard shows it). None unless active: a paused bot still checks
    its holdings' exits, but buys nothing."""
    if bot.status != "active":
        return None
    if due(bot, now):
        return now
    day = ny_date(now)
    for _ in range(15):  # at most ~2 weeks of holidays in a row
        for m in _moments(bot, day):
            if m > now:
                return m
        day += timedelta(days=1)
    return None


# ---------------------------------------------------------------------------
# The watchlist
# ---------------------------------------------------------------------------

def watchlist(session: Session, bot: Bot) -> list[WatchItem]:
    return list(session.scalars(select(WatchItem).where(WatchItem.bot_id == bot.id).order_by(WatchItem.symbol)))


def end_fall(item: WatchItem) -> None:
    """Stop following a symbol's fall: bought, back up, sold or blacklisted."""
    item.dip_reference = item.armed_at = item.trough_price = item.trough_at = item.turned_at = None


def blacklist(session: Session, bot: Bot, item: WatchItem, now: datetime, reason: str) -> None:
    item.status, item.blacklisted_at, item.blacklist_reason = "blacklisted", now, reason
    item.in_zone = False
    end_fall(item)
    log_event(session, bot.id, "risk", f"{item.symbol} blacklisted: {reason}. No more buys until you re-enable it.", "warning", now)


def signal(session: Session, bot: Bot, symbol: str, kind: str, now: datetime, message: str, outcome: str = "",
           price: float | None = None, reference: float | None = None, change: float | None = None) -> Signal:
    """Save a recommendation (see models.SIGNAL_KINDS). The dashboard lists them and can notify you."""
    s = Signal(bot_id=bot.id, symbol=symbol, kind=kind, created_at=now, message=message, outcome=outcome, price=price,
               reference_price=reference, change_pct=None if change is None else round(change * 100, 2))
    session.add(s)
    notify.dip_signal(session, bot, s)  # by email too, for those who chose the signals
    return s


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


def check(session: Session, bot: Bot, broker: Broker, now: datetime, kind: str, bars_fn=None, news_fn=None,
          trend_fn=None) -> Decision | None:
    """One check of every symbol (see the module docstring): exits, then where each stands, then buys.

    kind: 'scheduled' (the interval), 'manual' (Run now while the market is open and the bot active), 'preview' (Run now
    otherwise: shows what it WOULD do; no order, no signal). A scheduled check that changed nothing leaves no Decision
    behind (a 15-minute bot checks 25 times a day): it returns None. Marks the check as done (last_decision_at) only
    once the prices came in, so a failed download is retried."""
    bars_fn, news_fn, trend_fn = bars_fn or yahoo_bars, news_fn or get_news, trend_fn or uptrend
    today = ny_date(now)
    trade = kind != "preview"
    buying = trade and bot.status == "active"
    items = watchlist(session, bot)
    by_symbol = {i.symbol: i for i in items}
    held = {h.symbol: h for h in holdings(session, bot)}
    symbols = list(dict.fromkeys([*by_symbol, *held]))
    moment = check_moment(bot, now) or now

    decision: Decision | None = None

    def decide() -> Decision:  # created on first need: orders point at it
        nonlocal decision
        if decision is None:
            decision = Decision(bot_id=bot.id, session_date=today, kind=kind, action="HOLD", confidence=1.0, created_at=now)
            session.add(decision)
            session.flush()
        return decision

    try:
        closes = bars_fn(symbols, bot.interval, history_start(bot, today)) if symbols else pd.DataFrame()
    except Exception as e:  # BrokerError, or a malformed download: never trade on prices we couldn't read
        msg = f"Prices failed, no check: {e}"
        if kind == "scheduled":
            log_event(session, bot.id, "error", msg, "error", now)
            session.commit()
            return None
        d = decide()
        d.outcome = msg
        session.commit()
        return d

    levels = {s: stand(closes[s] if s in closes else None, bot, moment, today) for s in symbols}
    pending = {o.symbol for o in open_orders(session, bot)}
    lines: list[str] = []
    bought: list[str] = []
    sold: list[str] = []
    events = 0  # signals saved: a scheduled check that saved one is worth a Decision row

    # 1. Exits, on fresh quotes
    for s, h in held.items():
        if s in pending:
            lines.append(f"{s}: an order is still pending at the broker")
            continue
        try:
            q = _quote(broker, s, now, fresh=trade)
        except BrokerError as e:
            lines.append(f"{s}: held, no quote to check its exits ({e})")
            continue
        h.last_price, h.last_price_at = q.price, q.at
        change = q.price / h.entry_price - 1 if h.entry_price else 0.0
        if h.stop_price is not None and q.price <= h.stop_price:
            reason = "stop-loss"
        elif h.target_price is not None and q.price >= h.target_price:
            reason = "target"
        elif bot.max_hold_days and sessions_since(ny_date(h.opened_at), today) >= bot.max_hold_days:
            reason = "time"
        else:
            lines.append(f"{s}: held {fmt_qty(h.shares)} sh, ${q.price:.2f} ({_pct(change)} from the buy); target "
                         f"${h.target_price or 0:.2f}, stop ${h.stop_price or 0:.2f}")
            continue
        if not trade:
            lines.append(f"{s}: would sell ({reason}) at ${q.price:.2f}, {_pct(change)} from the buy")
            continue
        order = submit_order(session, bot, broker, "SELL", h.shares, reason, now, decide(), symbol=s)
        sold.append(s)
        done = _describe(order)
        lines.append(f"{s}: {reason} at ${q.price:.2f} ({_pct(change)}): {done}")
        item = by_symbol.get(s)
        if item is not None:
            item.sold_on = today
            end_fall(item)
        events += 1
        if reason == "stop-loss":
            signal(session, bot, s, "stop", now, f"{s} fell to ${q.price:.2f}, under its stop ${h.stop_price:.2f} "
                   f"({_pct(change)} from the buy): sell", f"{done}. Blacklisted until you re-enable it." if item else done,
                   q.price, h.reference_price, change)
            if item is not None:
                blacklist(session, bot, item, now, f"stop-loss at ${q.price:.2f} (stop ${h.stop_price:.2f})")
        elif reason == "target":
            signal(session, bot, s, "up", now, f"{s} is back at ${q.price:.2f}, its target ${h.target_price:.2f} "
                   f"({_pct(change)} from the buy): sell", done, q.price, h.reference_price, change)
        else:
            signal(session, bot, s, "time", now, f"{s} held {bot.max_hold_days} trading days without getting back "
                   f"({_pct(change)} from the buy): sell", done, q.price, h.reference_price, change)

    # 2. Where each watched symbol stands; falls that start or end
    rebound = bool(bot.rebound)  # NULL on bots from before the option: buy at once, as they always did
    up = 1 + (bot.rebound_pct or 0.01)
    zone: dict[str, bool] = {}
    entered: list[str] = []
    turned: dict[str, tuple[bool, float]] = {}  # waiting for the turn: symbol -> (first turn of its fall?, its low)
    for item in items:
        s = item.symbol
        ref, price = levels[s]
        item.checked_at, item.last_price, item.reference_price = now, price, ref
        item.drop = round(1 - price / ref, 6) if ref and price else None
        zone[s] = item.drop is not None and item.drop >= bot.drop_pct - 1e-12
        following = item.status == "watching" and not (s in held and s not in sold) and price is not None
        if trade:  # a preview changes no state: the next real check still sees a new fall as new
            if following and item.dip_reference:
                if item.trough_price is None or price < item.trough_price:
                    item.trough_price, item.trough_at = price, now
                if price >= item.dip_reference:  # back where the fall started before it was bought: that dip is over
                    events += 1
                    signal(session, bot, s, "up", now, f"{s} is back at ${price:.2f}, where its fall started "
                           f"(${item.dip_reference:.2f}): the dip is over",
                           f"Not bought: it never turned up {bot.rebound_pct:.1%} from its low first." if rebound
                           else "Not held: nothing to sell.", price, item.dip_reference, price / item.dip_reference - 1)
                    end_fall(item)
            if following and zone[s] and item.dip_reference is None:  # a new fall: follow it
                entered.append(s)
                item.dip_reference, item.armed_at, item.turned_at = ref, now, None
                item.trough_price, item.trough_at = price, now
            item.in_zone = zone[s]
        if rebound and following and item.dip_reference and item.trough_price:
            low = min(item.trough_price, price)
            if price >= low * up - 1e-9 and price < item.dip_reference:
                turned[s] = (item.turned_at is None, low)
                if trade and item.turned_at is None:
                    item.turned_at = now

    # 3. Buys, deepest fall first: falls in the buy zone, or with `rebound` the ones that just turned up
    def buy_reference(i: WatchItem) -> float | None:
        return i.dip_reference if rebound else i.reference_price

    def fall(i: WatchItem) -> float:
        ref = buy_reference(i)
        return 1 - i.last_price / ref if ref and i.last_price else 0.0

    notes: dict[str, str] = {}
    candidates = sorted((i for i in items if i.status == "watching" and i.symbol not in held and i.symbol not in pending
                         and (i.symbol in turned if rebound else zone[i.symbol])), key=lambda i: -fall(i))
    in_use = len([s for s in held if s not in sold]) + len(pending - set(held))
    free = bot.max_positions - in_use
    budget = bot.cash
    if candidates and buying:
        try:
            bp = broker.buying_power()  # a real account: never more than it can pay
        except BrokerError as e:
            bp = 0.0
            lines.append(f"Buying power unknown ({e}): no buys this check")
        if bp is not None:
            budget = min(budget, bp)
    slot = bot_equity(session, bot, None) / bot.max_positions
    trend = None
    for item in candidates:
        s, ref = item.symbol, buy_reference(item)
        if not buying:
            notes[s] = "the bot is paused" if trade else f"would buy (a {fall(item):.1%} fall)" if free > 0 else "all slots in use"
            free -= 1
            continue
        if item.sold_on == today:
            notes[s] = "sold today: not bought again before tomorrow"
            continue
        if item.news_blocked_on == today:
            notes[s] = f"the news was {item.news_sentiment or 'bearish'} today"
            continue
        if free <= 0:
            notes[s] = f"all {bot.max_positions} slots in use"
            continue
        if bot.trend_filter:
            if trend is None:
                try:
                    trend = trend_fn([c.symbol for c in candidates], today)
                except Exception as e:  # can't tell the trend: don't buy blind
                    trend = {}
                    lines.append(f"Trend filter failed ({e}): no buys this check")
            item.trend_ok = trend.get(s)
            if not item.trend_ok:
                notes[s] = "not in an uptrend (50-day average under the 200-day)"
                continue
        try:
            q = _quote(broker, s, now)
        except BrokerError as e:
            notes[s] = f"no fresh quote ({e})"
            continue
        if rebound:
            low = turned[s][1]
            if q.price < low * up - 1e-9 or q.price >= ref:
                notes[s] = f"${q.price:.2f} at the quote: not {bot.rebound_pct:.1%} up from its low ${low:.2f} under ${ref:.2f}"
                continue
        elif 1 - q.price / ref < bot.drop_pct - 1e-12:
            notes[s] = f"back up to ${q.price:.2f} at the quote: no longer a {bot.drop_pct:.0%} fall"
            continue
        if bot.news:
            verdict = news_fn(s, today, now)
            item.news_sentiment, item.news_at = str(verdict.get("sentiment") or "unavailable")[:16], now
            if item.news_sentiment == "bearish":
                item.news_blocked_on = today
                events += 1
                why = next((x for x in verdict.get("steps") or [] if x.startswith("News (")), "")
                signal(session, bot, s, "news", now, f"{s} fell {1 - q.price / ref:.1%}, but the news is bearish: the fall may "
                       f"have a reason. {why}".strip(), "Not bought today.", q.price, ref, q.price / ref - 1)
                notes[s] = "bearish news: not bought today"
                continue
        unit = q.price * (1 + bot.slippage_pct) * (1 + bot.fee_pct)
        split, whole_why = bool(bot.fractional), ""
        if split:
            try:
                split = broker.fractionable(s)
                whole_why = "" if split else f"; {s} can't be bought in fractions at {broker.name}"
            except BrokerError as e:
                split, whole_why = False, f"; fractions unknown ({e})"
        shares = affordable(min(slot, budget), unit, split)
        if shares <= 0:
            notes[s] = (f"a slot (${min(slot, budget):,.2f}) is under the $1 smallest fractional order" if split
                        else f"a slot (${min(slot, budget):,.0f}) can't buy one share at ${q.price:.2f}{whole_why}")
            continue
        order = submit_order(session, bot, broker, "BUY", shares, "dip", now, decide(), symbol=s, reference_price=ref)
        budget -= shares * unit
        free -= 1
        bought.append(s)
        news_note = f"; news {item.news_sentiment}" if bot.news else ""
        notes[s] = f"bought: {_describe(order)}{news_note}{whole_why}"

    # The signals: a fall that just started ("down"); with `rebound` its turn up ("rebound": the first one, or the one it
    # was bought on); without, a fall still on that was bought once a slot freed up
    if trade:
        for s in entered:
            item = by_symbol[s]
            message = (f"{s} is {item.drop:.1%} under its {window_label(bot)} ${item.reference_price:.2f} at ${item.last_price:.2f}: "
                       + ("fell into the buy zone" if rebound else "buy zone"))
            outcome = (f"Waiting for it to turn up {bot.rebound_pct:.1%} from its low (${item.last_price * up:.2f} or more if it "
                       "falls no further)." if rebound else _sentence(notes.get(s, "not bought")))
            events += 1
            signal(session, bot, s, "down", now, message, outcome, item.last_price, item.reference_price, -item.drop)
        for item in candidates:
            s = item.symbol
            if rebound and (turned[s][0] or s in bought):
                low, ref = turned[s][1], buy_reference(item)
                later = "" if turned[s][0] else " (still up from its low)"
                events += 1
                signal(session, bot, s, "rebound", now, f"{s} turned up {item.last_price / low - 1:+.1%} from its low ${low:.2f} "
                       f"to ${item.last_price:.2f}{later}: bearish turned bullish, buy", _sentence(notes.get(s, "not bought")),
                       item.last_price, ref, item.last_price / ref - 1)
            elif not rebound and s in bought and s not in entered:
                events += 1
                signal(session, bot, s, "down", now, f"{s} is {item.drop:.1%} under its {window_label(bot)} "
                       f"${item.reference_price:.2f} at ${item.last_price:.2f} (still in the buy zone): buy zone",
                       _sentence(notes.get(s, "not bought")), item.last_price, item.reference_price, -item.drop)
        for s in bought:
            end_fall(by_symbol[s])
    for item in items:
        if item.symbol in held and item.symbol not in sold:
            continue
        if item.status == "blacklisted":
            lines.append(f"{item.symbol}: blacklisted ({item.blacklist_reason or 'by you'})")
        elif item.drop is None:
            lines.append(f"{item.symbol}: not enough prices yet for a {window_label(bot)}")
        elif rebound and item.dip_reference and item.symbol not in notes:
            lines.append(f"{item.symbol}: ${item.last_price:.2f}, fell from ${item.dip_reference:.2f}; waiting for the turn: "
                         f"low ${item.trough_price or item.last_price:.2f}, buys at ${(item.trough_price or item.last_price) * up:.2f} or more")
        else:
            state = notes.get(item.symbol) or ("in the buy zone" if zone[item.symbol] else "watching")
            lines.append(f"{item.symbol}: ${item.last_price:.2f}, {-item.drop:+.1%} vs its {window_label(bot)} "
                         f"${item.reference_price:.2f} (buy at ${item.reference_price * (1 - bot.drop_pct):.2f} or under): {state}")

    if trade:
        bot.last_decision_date, bot.last_decision_at = today, now
        mark_to_market(session, bot, bot.last_price or bot.allocated_cash, now)
    if kind == "scheduled" and decision is None and not events:
        session.commit()
        return None
    d = decide()
    d.action = "TRADE" if bought and sold else "BUY" if bought else "SELL" if sold else "HOLD"
    d.steps = [f"Buy a {bot.drop_pct:.0%} fall under the {window_label(bot)}"
               + (f" once it turns up {bot.rebound_pct:.1%} from its low" if rebound else "") + "; sell at "
               f"{'the reference' if bot.target_mode == 'reference' else f'+{bot.rise_pct:.0%}'}, stop {bot.stop_pct:.0%}"
               + ("; fractions of a share" if bot.fractional else "")] + lines
    in_zone = [i.symbol for i in candidates]
    if bought or sold:
        d.reasoning = "; ".join(filter(None, [f"Bought {', '.join(bought)}" if bought else "",
                                              f"sold {', '.join(sold)}" if sold else ""])) + "."
    else:
        waiting = [i.symbol for i in items if rebound and i.dip_reference and i.symbol not in turned]
        why = "; ".join(f"{s}: {notes[s]}" for s in in_zone if s in notes)
        d.reasoning = (f"{'Turned up' if rebound else 'In the buy zone'}: {', '.join(in_zone)}, not bought ({why})." if in_zone
                       else f"Waiting for the turn: {', '.join(waiting)}." if waiting
                       else "Nothing in the buy zone, nothing to sell.")
    d.outcome = ("Preview only (market closed or bot paused): no order sent. " if kind == "preview" else "") + d.reasoning
    session.commit()
    return d


def sell_one(session: Session, bot: Bot, broker: Broker, symbol: str, now: datetime) -> Order:
    """Sell one holding at market now (manual). The symbol stays on the watchlist, not blacklisted: it can be bought
    again from tomorrow."""
    h = session.scalar(select(Holding).where(Holding.bot_id == bot.id, Holding.symbol == symbol))
    if h is None or h.shares <= 0:
        raise ValueError(f"not holding {symbol}")
    item = session.scalar(select(WatchItem).where(WatchItem.bot_id == bot.id, WatchItem.symbol == symbol))
    if item is not None:
        item.sold_on = ny_date(now)
        end_fall(item)
    return submit_order(session, bot, broker, "SELL", h.shares, "manual", now, symbol=symbol)
