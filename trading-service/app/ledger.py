"""The money history of a real account: what it started with, every move since, and the sum that checks them all.

The bot table adds up what was put in the bots still running, so it can't say how much of the starting money is left:
taking money out of one bot and adding it to another, or archiving a bot and starting a new one with its cash, moves that
total up and down while not a cent enters the account. Here the account is what counts:

    in the account (the bots' equity + free cash) = start + real deposits - real withdrawals + gains and losses

Gains and losses are the bots' own, the archived ones' too, after the Alpaca fees booked to them (fees.py), less any fee
no bot paid. Moves between bots cancel out: they only pass through free cash. So the history reads as free cash going up
and down, and its last line is what the records say free cash should be. What they can't explain shows as the difference:
a deposit not written down here, a dividend, a fractional fill's rounding, or for a minute around a fill the account's
read (up to 30 seconds old) and the bots' records being apart.

Only real-money accounts: the simulator has no account, and the Alpaca paper account is shared with the Mac's bots.

The start and the real deposits and withdrawals are rows in the database (MoneyMove without a bot), written down on the
dashboard: no bot can know them, the repo is public so no amount goes in the code, they change without a restart, and the
database's backups keep them. They are only records: nothing here reaches the broker.

A bot has its own, smaller history (of_bot), on any broker, as its records know it all:

    its equity = start + added - taken out + its trades' gains and losses

Its moves are the start, each Add money and each withdrawal, and its cash going back to the account when archived."""

import re
from collections import defaultdict
from datetime import date, datetime, time

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .market import NY
from .models import AccountFee, Bot, Event, MoneyMove
from .schemas import BotLedgerOut, BotMove, LedgerMove, LedgerOut
from .trader import capital

ACCOUNT_KINDS = ("start", "deposit", "withdrawal")

# What backfill reads from the events of the adds and withdrawals (main.add_bot_capital, withdraw._story, withdraw.settle)
_DOLLARS = r"\$([\d,]+\.\d{2})"
_ADDED = re.compile(rf"^Added {_DOLLARS}")
_TOOK_OUT = re.compile(rf"^Took out {_DOLLARS}")
_CAME_OUT = re.compile(rf"{_DOLLARS} came out")
_RAISED_LESS = re.compile(rf"^Withdrawal of .* raised {_DOLLARS} less")


def noon(day: date) -> datetime:
    """A day picked on the dashboard as a moment: noon in New York, so it reads as that day there."""
    return datetime.combine(day, time(12), tzinfo=NY)


def _dollars(match: re.Match) -> float:
    return float(match.group(1).replace(",", ""))


def _from_event(event: Event) -> float | None:
    """The move an add's or withdrawal's event tells of, into the bot (negative: out of it); None when it tells of none:
    a withdrawal settled in full. A withdrawal says "$49.95 came out" when its sales raised less than the $50.00 asked."""
    if m := _ADDED.search(event.message):
        return _dollars(m)
    if m := _TOOK_OUT.search(event.message):
        return -_dollars(_CAME_OUT.search(event.message) or m)
    if m := _RAISED_LESS.search(event.message):
        return _dollars(m)
    return None


def backfill(session: Session) -> int:
    """At startup: the rows for adds and withdrawals made before this history was kept, read from each bot's events, so
    they add up to its added_cash and withdrawn_cash. What the events don't explain gets one row at the bot's start, saying
    so. A bot whose rows already add up is skipped, so this does its work once. Returns the rows it wrote."""
    bots = list(session.scalars(select(Bot).where(or_(Bot.added_cash.is_not(None), Bot.withdrawn_cash.is_not(None)))))
    have: dict[tuple[int, str], float] = defaultdict(float)
    for bot_id, kind, amount in session.execute(select(MoneyMove.bot_id, MoneyMove.kind, MoneyMove.amount).where(
            MoneyMove.bot_id.is_not(None))):
        have[bot_id, kind] += amount
    told: dict[tuple[int, str], list[Event]] = defaultdict(list)
    for e in session.scalars(select(Event).where(Event.kind.in_(("capital", "withdrawal")),
                                                 Event.bot_id.in_([b.id for b in bots])).order_by(Event.id)):
        told[e.bot_id, "add" if e.kind == "capital" else "take_out"].append(e)
    written = 0
    for bot in bots:
        for kind, total in (("add", bot.added_cash or 0.0), ("take_out", -(bot.withdrawn_cash or 0.0))):
            if abs(have[bot.id, kind] - total) < 0.005:
                continue
            rest = total - have[bot.id, kind]
            for e in [] if have[bot.id, kind] else told[bot.id, kind]:  # with rows already, the events would count them twice
                if amount := _from_event(e):
                    session.add(MoneyMove(broker=bot.broker, bot_id=bot.id, kind=kind, amount=amount, at=e.created_at))
                    rest -= amount
                    written += 1
            if abs(rest) >= 0.005:
                session.add(MoneyMove(broker=bot.broker, bot_id=bot.id, kind=kind, amount=round(rest, 2), at=bot.created_at,
                                      note="From before this history was kept: the bot's events don't say when."))
                written += 1
    session.commit()
    return written


def _bots_moves(rows: list[MoneyMove]) -> list[dict]:
    """The bots' adds and withdrawals, into the bot (negative: out of it). A withdrawal whose sales raise less than
    asked is two rows at one moment, one withdrawal to the reader."""
    same: dict[tuple, float] = defaultdict(float)
    for m in rows:
        same[m.bot_id, m.kind, m.at, m.note] += m.amount
    out = []
    for (bot_id, kind, at, note), amount in same.items():
        if kind == "take_out" and amount > 0 and not note:
            note = "A withdrawal's sales raised less than estimated: this stayed in the bot."
        out.append(dict(at=at, kind=kind, bot_id=bot_id, amount=amount, note=note))
    return out


def _archived_at(session: Session, bot_ids: list[int]) -> dict[int, datetime]:
    """When each archived bot was archived, by its last "archived" event."""
    return dict(session.execute(select(Event.bot_id, func.max(Event.created_at)).where(
        Event.kind == "archived", Event.bot_id.in_(bot_ids)).group_by(Event.bot_id)).all())


def _moves(session: Session, broker: str, bots: list[Bot]) -> list[LedgerMove]:
    """The history, the earliest first: every move of the account's free cash. A new bot's start and an archived bot's
    cash coming back come from the bots themselves; a fee booked to a bot after it was archived is already in that cash."""
    names = {b.id: b.name for b in bots}
    archived = _archived_at(session, list(names))
    lines: list[dict] = []
    for b in bots:
        lines.append(dict(at=b.created_at, kind="new_bot", bot_id=b.id, amount=-b.allocated_cash))
        if b.status == "archived":
            lines.append(dict(at=archived.get(b.id) or b.created_at, kind="archived", bot_id=b.id, amount=b.cash))
    theirs = []
    for m in session.scalars(select(MoneyMove).where(MoneyMove.broker == broker)):
        if m.bot_id is None:
            lines.append(dict(id=m.id, at=m.at, kind=m.kind, amount=m.amount, note=m.note))
        else:
            theirs.append(m)
    lines += [{**x, "amount": -x["amount"]} for x in _bots_moves(theirs)]  # into a bot is out of free cash
    for f in session.scalars(select(AccountFee).where(AccountFee.broker == broker, AccountFee.bot_id.is_(None))):
        lines.append(dict(at=f.created_at, kind="fee", amount=-f.amount, note=f.description))
    lines.sort(key=lambda x: (x["kind"] != "start", x["at"]))  # the start comes first, whatever day it was given
    out, free = [], 0.0
    for x in lines:
        free = round(free + x["amount"], 2)
        out.append(LedgerMove(id=x.get("id"), at=x["at"], kind=x["kind"], bot_id=x.get("bot_id"), bot_name=names.get(x.get("bot_id")),
                              amount=round(x["amount"], 2), free_after=free, note=x.get("note", "")))
    return out


def account(session: Session, broker: str, label: str, equity_of: dict[int, float], cash: float | None, error: str,
            checked_at: datetime | None) -> LedgerOut:
    """The account's money (module docstring). equity_of: the equity of its bots that aren't archived; cash: the
    account's, as just read (None: it couldn't be)."""
    bots = list(session.scalars(select(Bot).where(Bot.broker == broker).order_by(Bot.created_at, Bot.id)))
    running = [b for b in bots if b.status != "archived"]
    gone = [b for b in bots if b.status == "archived"]
    own = {k: 0.0 for k in ACCOUNT_KINDS}
    started = False
    for kind, amount in session.execute(select(MoneyMove.kind, MoneyMove.amount).where(
            MoneyMove.broker == broker, MoneyMove.bot_id.is_(None))):
        own[kind] += amount
        started = started or kind == "start"
    fees = dict(session.execute(select(AccountFee.bot_id.is_(None), func.sum(AccountFee.amount)).where(
        AccountFee.broker == broker).group_by(AccountFee.bot_id.is_(None))).all())
    fees_no_bot = fees.get(True) or 0.0
    bots_equity = round(sum(equity_of[b.id] for b in running), 2)
    running_result = round(sum(equity_of[b.id] + (b.withdrawn_cash or 0.0) - capital(b) for b in running), 2)
    archived_result = round(sum(b.cash + (b.withdrawn_cash or 0.0) - capital(b) for b in gone), 2)
    result = round(running_result + archived_result - fees_no_bot, 2)
    money = round(own["start"] + own["deposit"] + own["withdrawal"], 2)  # a withdrawal's amount is negative
    expected = round(money + result, 2) if started else None
    free = None if cash is None else round(cash - sum(b.cash for b in running), 2)
    equity = None if free is None else round(bots_equity + free, 2)
    return LedgerOut(
        broker=broker, label=label, start=own["start"] if started else None, deposits=round(own["deposit"], 2),
        withdrawals=round(abs(own["withdrawal"]), 2), capital=money, added=round(sum(b.added_cash or 0.0 for b in bots), 2),
        taken_out=round(sum(b.withdrawn_cash or 0.0 for b in bots), 2), running_result=running_result,
        archived_result=archived_result, fees=round(sum(v or 0.0 for v in fees.values()), 2), fees_no_bot=round(fees_no_bot, 2),
        result=result, result_pct=round(result / money * 100, 2) if started and money > 0 else None, expected=expected,
        bots_equity=bots_equity, free_cash=free, equity=equity,
        difference=None if equity is None or expected is None else round(equity - expected, 2), error=error,
        checked_at=checked_at, first_bot_day=bots[0].created_at.astimezone(NY).date() if bots else None,
        moves=_moves(session, broker, bots))


def of_bot(session: Session, b: Bot, equity: float) -> BotLedgerOut:
    """A bot's money from its start (module docstring), on any broker. equity: the bot's, cash and what it holds; an
    archived bot holds nothing, so its cash, which went back to the account. The running total after each line is what
    is still put in: below zero once more came out than went in."""
    lines = [dict(at=b.created_at, kind="start", amount=b.allocated_cash, note="")]
    lines += _bots_moves(list(session.scalars(select(MoneyMove).where(MoneyMove.bot_id == b.id))))
    archived = b.status == "archived"
    if archived:
        lines.append(dict(at=_archived_at(session, [b.id]).get(b.id) or b.created_at, kind="archived", amount=-b.cash, note=""))
    lines.sort(key=lambda x: ({"start": 0, "archived": 2}.get(x["kind"], 1), x["at"]))
    moves, net = [], 0.0
    for x in lines:
        net = round(net + x["amount"], 2)
        moves.append(BotMove(at=x["at"], kind=x["kind"], amount=round(x["amount"], 2), net_after=net, note=x["note"]))
    added, taken_out = round(b.added_cash or 0.0, 2), round(b.withdrawn_cash or 0.0, 2)
    put_in = round(capital(b) - taken_out, 2)
    fees = session.scalar(select(func.sum(AccountFee.amount)).where(AccountFee.bot_id == b.id)) or 0.0
    return BotLedgerOut(bot_id=b.id, start=b.allocated_cash, added=added, taken_out=taken_out, net=put_in, equity=equity,
                        result=round(equity - put_in, 2), fees=round(fees, 2), archived=archived, moves=moves)
