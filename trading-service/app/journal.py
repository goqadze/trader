"""The trading journal: each signed-in user's own record of their trades and of how they felt making them.

Traders keep one to learn from their own behaviour, not only from the market:
  - a trade goes planned -> open -> closed (or skipped). At each step they write a note: the emotions they feel, how
    strongly, and what they think. Afterwards they review it: did I follow my plan, what went wrong, the lesson.
  - each trading day has a page: the plan before the open, the review after the close, and check-ins in between.
  - the statistics show what their own record says: win rate, profit factor, expectancy, R multiples, and how they
    do by setup, by emotion, by mistake, by weekday ...
  - the bots' trades can be imported (one journal trade per sale), so those can be journaled too.

Every row belongs to one user, and every endpoint here reads and writes only the signed-in user's rows: nobody else,
admins included, sees someone's journal. A trading day is a New York date, like the market's (market.ny_date).
A journal holds thousands of trades at most, so the list filters and the statistics work in Python on the user's rows.
"""

import csv
import io
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .auth import current_user
from .brokers import catalog
from .db import get_session, utcnow
from .market import NY, ny_date
from .models import (DIP, ROTATION, Bot, JournalDay, JournalImage, JournalNote, JournalTrade, Order, User)
from .schemas import (JournalCalendarDayOut, JournalDayIn, JournalDayOut, JournalGroupOut, JournalImageOut, JournalImportBotOut,
                      JournalImportIn, JournalImportOut, JournalNoteIn, JournalNoteOut, JournalNoteUpdate, JournalOptionsOut,
                      JournalSide, JournalStatsOut, JournalStatus, JournalTradeCreate, JournalTradeDetailOut, JournalTradeIn,
                      JournalTradeOut, JournalTradePageOut, JournalTradeUpdate)

router = APIRouter(prefix="/journal", tags=["journal"])

EDITABLE = tuple(JournalTradeIn.model_fields)  # what a trade's form sets
HALF_CENT = 0.005  # a P&L this close to zero is breakeven
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
ENTRY_STAGES = ("plan", "entry")  # the emotions a trade was taken with: felt while planning or entering it
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 20  # per trade
MAX_DAYS = 400  # the longest calendar asked for at once
# What a bot's sale was, in the journal's words
EXIT_REASONS = {"target": "Target reached", "stop-loss": "Stop-loss hit", "time": "Held its maximum days",
                "manual": "Sold by hand from the dashboard", "signal": "The strategy's sell signal",
                "rotation": "Rotated out (no longer among the strongest)", "rebalance": "Trimmed in a rebalance",
                "withdrawal": "Sold to raise money taken out of the bot"}
ACCOUNTS = {"paper": "Simulator (paper)", "alpaca-paper": "Alpaca paper", "alpaca-live": "Alpaca live"}


# ---------------------------------------------------------------------------
# A trade's numbers
# ---------------------------------------------------------------------------

def _risk(t) -> float | None:
    """$ lost if the first stop is hit: one R. None without a stop (or a stop on the wrong side)."""
    if t.entry_price is None or t.stop_price is None or not t.quantity:
        return None
    per_share = t.entry_price - t.stop_price if t.side == "long" else t.stop_price - t.entry_price
    return round(per_share * t.quantity, 2) if per_share > 0 else None


def apply(trade: JournalTrade, body: JournalTradeIn) -> None:
    """Set a trade from its checked form, clear what its status doesn't have (a planned trade no times or exit, an open
    one no exit), and work out its P&L (after fees) and R multiple."""
    for f in EDITABLE:
        setattr(trade, f, getattr(body, f))
    if trade.status in ("planned", "skipped"):
        trade.opened_at = None
    if trade.status != "closed":
        trade.closed_at = trade.exit_price = None
    trade.pnl = trade.r_multiple = None
    if trade.status == "closed":
        sign = 1 if trade.side == "long" else -1
        trade.pnl = round(sign * (trade.exit_price - trade.entry_price) * trade.quantity - trade.fees, 2)
        risk = _risk(trade)
        trade.r_multiple = round(trade.pnl / risk, 2) if risk else None


def trade_day(t: JournalTrade) -> date:
    """The trading day a trade belongs to: of its exit, else its entry, else the day it was written down."""
    return ny_date(t.closed_at or t.opened_at or t.created_at)


def trade_out(t: JournalTrade, notes: int = 0, images: int = 0, emotions: list[str] | None = None) -> JournalTradeOut:
    risk = _risk(t)
    reward_risk = None
    if risk and t.target_price is not None:
        reward_risk = round(abs(t.target_price - t.entry_price) / abs(t.entry_price - t.stop_price), 2)
    put_in = (t.entry_price or 0) * (t.quantity or 0)
    return JournalTradeOut.model_validate({
        **{c: getattr(t, c) for c in JournalTradeOut.model_fields if hasattr(t, c)},
        "day": trade_day(t),
        "risk": risk,
        "reward_risk": reward_risk,
        "return_pct": round(t.pnl / put_in * 100, 2) if t.pnl is not None and put_in else None,
        "hold_minutes": int((t.closed_at - t.opened_at).total_seconds() // 60) if t.closed_at and t.opened_at else None,
        "emotions": emotions or [],
        "notes": notes,
        "images": images,
    })


def _extras(session: Session, ids: list[int]) -> dict[int, tuple[int, int, list[str]]]:
    """Per trade: (notes, images, every emotion in its notes, first felt first)."""
    out = {i: [0, 0, []] for i in ids}
    if not ids:
        return {}
    for trade_id, emotions in session.execute(select(JournalNote.trade_id, JournalNote.emotions)
                                              .where(JournalNote.trade_id.in_(ids)).order_by(JournalNote.at, JournalNote.id)):
        row = out[trade_id]
        row[0] += 1
        row[2] += [e for e in emotions if e not in row[2]]
    for trade_id, n in session.execute(select(JournalImage.trade_id, func.count()).where(JournalImage.trade_id.in_(ids))
                                       .group_by(JournalImage.trade_id)):
        out[trade_id][1] = n
    return {i: (n, im, em) for i, (n, im, em) in out.items()}


def _outs(session: Session, trades: list[JournalTrade]) -> list[JournalTradeOut]:
    extras = _extras(session, [t.id for t in trades])
    return [trade_out(t, *extras[t.id]) for t in trades]


def _note_out(n: JournalNote, symbol: str | None = None) -> JournalNoteOut:
    return JournalNoteOut.model_validate(n).model_copy(update={"symbol": symbol})


# ---------------------------------------------------------------------------
# Filters (the list, the statistics and the CSV export share them)
# ---------------------------------------------------------------------------

class Filters:
    """Which of the user's trades to show. Dates are trading days (New York), both ends included."""

    def __init__(self, status: JournalStatus | None = None, q: str = Query("", max_length=40),
                 setup: str | None = None, tag: str | None = None, mistake: str | None = None,
                 emotion: str | None = None, account: str | None = None,
                 source: Literal["manual", "bot"] | None = None, side: JournalSide | None = None,
                 result: Literal["win", "loss", "breakeven"] | None = None,
                 start: date | None = None, end: date | None = None):
        self.status, self.q, self.setup, self.tag, self.mistake = status, q.strip().lower(), setup, tag, mistake
        self.emotion, self.account, self.source, self.side, self.result = emotion, account, source, side, result
        self.start, self.end = start, end

    def about_trades(self) -> bool:
        """Anything chosen besides the dates."""
        return any(v not in (None, "") for v in (self.status, self.q, self.setup, self.tag, self.mistake, self.emotion,
                                                  self.account, self.source, self.side, self.result))

    def in_range(self, day: date) -> bool:
        return (self.start is None or day >= self.start) and (self.end is None or day <= self.end)


def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _note_emotions(session: Session, user: User) -> dict[int, set[str]]:
    """Per trade: the emotions in any of its notes."""
    out: dict[int, set[str]] = {}
    for trade_id, emotions in session.execute(select(JournalNote.trade_id, JournalNote.emotions)
                                              .where(JournalNote.user_id == user.id, JournalNote.trade_id.is_not(None))):
        out.setdefault(trade_id, set()).update(emotions)
    return out


def _note_texts(session: Session, user: User) -> dict[int, str]:
    """Per trade: everything its notes say."""
    out: dict[int, str] = {}
    for trade_id, text in session.execute(select(JournalNote.trade_id, JournalNote.text)
                                          .where(JournalNote.user_id == user.id, JournalNote.trade_id.is_not(None))):
        out[trade_id] = out.get(trade_id, "") + " " + text
    return out


def matching(session: Session, user: User, f: Filters) -> list[JournalTrade]:
    """The user's trades that pass the filters, newest first."""
    query = select(JournalTrade).where(JournalTrade.user_id == user.id)
    for col, value in ((JournalTrade.status, f.status), (JournalTrade.source, f.source), (JournalTrade.side, f.side)):
        if value:
            query = query.where(col == value)
    felt = _note_emotions(session, user) if f.emotion else {}
    words = _note_texts(session, user) if f.q else {}
    out = []
    for t in session.scalars(query):
        if not f.in_range(trade_day(t)):
            continue
        if f.q and f.q not in " ".join([t.symbol, t.setup, *t.tags, t.thesis, t.exit_reason, t.review, t.lessons,
                                        words.get(t.id, "")]).lower():
            continue
        if f.setup is not None and not _same(t.setup, f.setup):
            continue
        if f.account is not None and not _same(t.account, f.account):
            continue
        if f.tag and not any(_same(x, f.tag) for x in t.tags):
            continue
        if f.mistake and not any(_same(x, f.mistake) for x in t.mistakes):
            continue
        if f.emotion and f.emotion.strip().lower() not in felt.get(t.id, ()):
            continue
        if f.result and _result(t) != f.result:
            continue
        out.append(t)
    out.sort(key=lambda t: (t.closed_at or t.opened_at or t.created_at, t.id), reverse=True)
    return out


def _result(t: JournalTrade) -> str | None:
    if t.pnl is None:
        return None
    return "win" if t.pnl > HALF_CENT else "loss" if t.pnl < -HALF_CENT else "breakeven"


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _group(rows: list[tuple[str, JournalTrade]], order: list[str] | None = None) -> list[JournalGroupOut]:
    """Closed trades by a key, with how each group did; the best P&L first unless an order of the keys is given."""
    groups: dict[str, list[JournalTrade]] = {}
    for key, t in rows:
        groups.setdefault(key, []).append(t)
    out = []
    for key, ts in groups.items():
        pnl = round(sum(t.pnl for t in ts), 2)
        wins = sum(_result(t) == "win" for t in ts)
        rs = [t.r_multiple for t in ts if t.r_multiple is not None]
        out.append(JournalGroupOut(key=key, trades=len(ts), wins=wins, losses=sum(_result(t) == "loss" for t in ts),
                                   win_rate=round(wins / len(ts) * 100, 1), pnl=pnl, avg_pnl=round(pnl / len(ts), 2),
                                   avg_r=round(sum(rs) / len(rs), 2) if rs else None))
    if order is not None:
        return sorted(out, key=lambda g: order.index(g.key))
    return sorted(out, key=lambda g: (-g.pnl, g.key))


def _streaks(results: list[str]) -> tuple[int, int, int]:
    """(longest run of wins, of losses, the run at the end: +wins / -losses). A breakeven trade ends a run."""
    best_win = best_loss = run = 0
    for r in results:
        if r == "win":
            run = run + 1 if run > 0 else 1
        elif r == "loss":
            run = run - 1 if run < 0 else -1
        else:
            run = 0
        best_win, best_loss = max(best_win, run), max(best_loss, -run)
    return best_win, best_loss, run


def _avg_minutes(ts: list[JournalTrade]) -> int | None:
    held = [(t.closed_at - t.opened_at).total_seconds() / 60 for t in ts if t.opened_at and t.closed_at]
    return round(sum(held) / len(held)) if held else None


def stats(trades: list[JournalTrade], entry_emotions: dict[int, list[str]], felt: Counter) -> JournalStatsOut:
    """The statistics of these trades. entry_emotions: per trade, what was felt planning or entering it; felt: how
    often each emotion was written down in the period."""
    closed = sorted((t for t in trades if t.status == "closed"), key=lambda t: (t.closed_at, t.id))
    count = Counter(t.status for t in trades)
    wins = [t for t in closed if _result(t) == "win"]
    losses = [t for t in closed if _result(t) == "loss"]
    profit = round(sum(t.pnl for t in wins), 2)
    loss = round(sum(t.pnl for t in losses), 2)
    net = round(sum(t.pnl for t in closed), 2)
    rs = [t.r_multiple for t in closed if t.r_multiple is not None]
    avg_win = round(profit / len(wins), 2) if wins else None
    avg_loss = round(loss / len(losses), 2) if losses else None

    running = peak = drawdown = 0.0
    daily: dict[date, list[JournalTrade]] = {}
    for t in closed:
        running += t.pnl
        peak = max(peak, running)
        drawdown = min(drawdown, running - peak)
        daily.setdefault(ny_date(t.closed_at), []).append(t)
    cumulative, days = 0.0, []
    for day in sorted(daily):
        pnl = round(sum(t.pnl for t in daily[day]), 2)
        cumulative = round(cumulative + pnl, 2)
        days.append({"day": day, "pnl": pnl, "trades": len(daily[day]), "cumulative": cumulative})

    def entered(t: JournalTrade):  # the entry's moment in New York, else the exit's
        return (t.opened_at or t.closed_at).astimezone(NY)

    hours = sorted({entered(t).hour for t in closed})
    best = max(closed, key=lambda t: t.pnl, default=None)
    worst = min(closed, key=lambda t: t.pnl, default=None)
    win_streak, loss_streak, current = _streaks([_result(t) for t in closed])
    extreme = (lambda t: {"id": t.id, "symbol": t.symbol, "pnl": t.pnl, "day": ny_date(t.closed_at)})
    return JournalStatsOut(
        trades=len(closed), open=count["open"], planned=count["planned"], skipped=count["skipped"],
        wins=len(wins), losses=len(losses), breakeven=len(closed) - len(wins) - len(losses),
        win_rate=round(len(wins) / len(closed) * 100, 1) if closed else None,
        net_pnl=net, gross_profit=profit, gross_loss=loss, fees=round(sum(t.fees for t in closed), 2),
        profit_factor=round(profit / -loss, 2) if losses and loss else None,
        avg_win=avg_win, avg_loss=avg_loss,
        payoff_ratio=round(avg_win / -avg_loss, 2) if avg_win and avg_loss else None,
        expectancy=round(net / len(closed), 2) if closed else None,
        avg_r=round(sum(rs) / len(rs), 2) if rs else None, total_r=round(sum(rs), 2) if rs else None, r_trades=len(rs),
        best=extreme(best) if best and best.pnl > HALF_CENT else None,
        worst=extreme(worst) if worst and worst.pnl < -HALF_CENT else None,
        max_win_streak=win_streak, max_loss_streak=loss_streak, current_streak=current,
        max_drawdown=round(drawdown, 2),
        avg_hold_minutes_win=_avg_minutes(wins), avg_hold_minutes_loss=_avg_minutes(losses),
        daily=days,
        by_setup=_group([(t.setup, t) for t in closed if t.setup]),
        by_tag=_group([(tag, t) for t in closed for tag in t.tags]),
        by_mistake=_group([(m, t) for t in closed for m in t.mistakes]),
        by_emotion=_group([(e, t) for t in closed for e in entry_emotions.get(t.id, [])]),
        by_symbol=_group([(t.symbol, t) for t in closed]),
        by_side=_group([(t.side, t) for t in closed], order=["long", "short"]),
        by_weekday=_group([(WEEKDAYS[entered(t).weekday()], t) for t in closed], order=list(WEEKDAYS)),
        by_hour=_group([(f"{entered(t).hour}:00", t) for t in closed], order=[f"{h}:00" for h in hours]),
        by_grade=_group([(str(t.grade), t) for t in closed if t.grade], order=[str(i) for i in range(1, 6)]),
        by_confidence=_group([(str(t.confidence), t) for t in closed if t.confidence], order=[str(i) for i in range(1, 6)]),
        by_plan=_group([("followed" if t.followed_plan else "broke", t) for t in closed if t.followed_plan is not None],
                       order=["followed", "broke"]),
        by_account=_group([(t.account, t) for t in closed if t.account]),
        emotions=[{"emotion": e, "count": n} for e, n in sorted(felt.items(), key=lambda kv: (-kv[1], kv[0]))],
    )


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

def _trade(session: Session, user: User, trade_id: int) -> JournalTrade:
    t = session.get(JournalTrade, trade_id)
    if t is None or t.user_id != user.id:  # someone else's trade doesn't exist for you
        raise HTTPException(404, "journal trade not found")
    return t


def _add_note(session: Session, user: User, trade: JournalTrade | None, note: JournalNoteIn) -> JournalNote:
    if trade is None and note.stage != "checkin":
        raise HTTPException(422, "a note without a trade is a check-in (stage checkin)")
    if trade is not None and note.stage == "checkin":
        raise HTTPException(422, "a note on a trade says its stage (plan, entry, during, exit, review)")
    n = JournalNote(user_id=user.id, trade_id=trade.id if trade else None, at=note.at or utcnow(), stage=note.stage,
                    emotions=note.emotions, intensity=note.intensity, text=note.text)
    session.add(n)
    return n


def _detail(session: Session, t: JournalTrade) -> JournalTradeDetailOut:
    notes = session.scalars(select(JournalNote).where(JournalNote.trade_id == t.id).order_by(JournalNote.at, JournalNote.id)).all()
    images = session.scalars(select(JournalImage).where(JournalImage.trade_id == t.id).order_by(JournalImage.id)).all()
    bot = session.get(Bot, t.bot_id) if t.bot_id else None
    out = _outs(session, [t])[0]
    return JournalTradeDetailOut(**out.model_dump(), note_list=[_note_out(n, t.symbol) for n in notes],
                                 image_list=[JournalImageOut.model_validate(i) for i in images],
                                 bot_name=bot.name if bot else None)


def _problems(e: ValidationError) -> str:
    """A validation error in words: "quantity: Input should be greater than 0; an open trade needs its entry price"."""
    return "; ".join((f"{err['loc'][-1]}: " if err["loc"] else "") + err["msg"].removeprefix("Value error, ") for err in e.errors())


def _checked(fields: dict) -> JournalTradeIn:
    try:
        return JournalTradeIn.model_validate(fields)
    except ValidationError as e:
        raise HTTPException(422, _problems(e))


@router.get("/trades", response_model=JournalTradePageOut)
def list_trades(f: Filters = Depends(), sort: Literal["day", "pnl", "r", "symbol"] = "day",
                order: Literal["asc", "desc"] = "desc", offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500),
                user: User = Depends(current_user), session: Session = Depends(get_session)):
    trades = matching(session, user, f)
    if sort != "day":
        value = {"pnl": lambda t: t.pnl, "r": lambda t: t.r_multiple, "symbol": lambda t: t.symbol}[sort]
        known = sorted((t for t in trades if value(t) is not None), key=value, reverse=order == "desc")
        trades = known + [t for t in trades if value(t) is None]  # no P&L / R yet: last either way
    elif order == "asc":
        trades.reverse()
    return JournalTradePageOut(total=len(trades), items=_outs(session, trades[offset:offset + limit]))


@router.post("/trades", response_model=JournalTradeDetailOut, status_code=201)
def create_trade(body: JournalTradeCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
    trade = JournalTrade(user_id=user.id, source="manual")
    apply(trade, body)
    session.add(trade)
    session.flush()
    if body.note:
        _add_note(session, user, trade, body.note)
    session.commit()
    return _detail(session, trade)


@router.get("/trades.csv")
def export_csv(f: Filters = Depends(), user: User = Depends(current_user), session: Session = Depends(get_session)):
    """The filtered trades as a spreadsheet (oldest first). Text a spreadsheet would run as a formula is quoted."""
    trades = matching(session, user, f)[::-1]
    extras = _extras(session, [t.id for t in trades])
    cols = ["id", "day", "status", "symbol", "side", "account", "setup", "tags", "quantity", "entry_price", "stop_price",
            "target_price", "exit_price", "fees", "pnl", "return_pct", "r_multiple", "risk", "reward_risk", "opened_at",
            "closed_at", "hold_minutes", "confidence", "followed_plan", "grade", "mistakes", "emotions", "thesis",
            "exit_reason", "review", "lessons", "source"]
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(cols)
    for t in trades:
        row = trade_out(t, *extras[t.id]).model_dump()
        cells = []
        for c in cols:
            v = row[c]
            v = "; ".join(v) if isinstance(v, list) else "" if v is None else v.isoformat() if isinstance(v, (date, datetime)) else v
            if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
                v = "'" + v
            cells.append(v)
        out.writerow(cells)
    name = f"journal-{ny_date(utcnow()).isoformat()}.csv"
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/trades/{trade_id}", response_model=JournalTradeDetailOut)
def get_trade(trade_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    return _detail(session, _trade(session, user, trade_id))


@router.patch("/trades/{trade_id}", response_model=JournalTradeDetailOut)
def update_trade(trade_id: int, body: JournalTradeUpdate, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    """Change some fields (the whole trade is checked again), e.g. entering it, closing it, or reviewing it, with a note
    on how that step feels."""
    trade = _trade(session, user, trade_id)
    changes = body.model_dump(exclude_unset=True, exclude={"note"})
    apply(trade, _checked({f: getattr(trade, f) for f in EDITABLE} | changes))
    if body.note:
        _add_note(session, user, trade, body.note)
    session.commit()
    return _detail(session, trade)


@router.delete("/trades/{trade_id}", status_code=204)
def delete_trade(trade_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Delete a trade with its notes and screenshots, for good."""
    trade = _trade(session, user, trade_id)
    session.execute(delete(JournalNote).where(JournalNote.trade_id == trade.id))
    session.execute(delete(JournalImage).where(JournalImage.trade_id == trade.id))
    session.delete(trade)
    session.commit()


# ---------------------------------------------------------------------------
# Notes: a trade's step by step, and check-ins
# ---------------------------------------------------------------------------

def _note(session: Session, user: User, note_id: int) -> JournalNote:
    n = session.get(JournalNote, note_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(404, "journal note not found")
    return n


def _symbols(session: Session, notes: list[JournalNote]) -> dict[int, str]:
    ids = {n.trade_id for n in notes if n.trade_id}
    return dict(session.execute(select(JournalTrade.id, JournalTrade.symbol).where(JournalTrade.id.in_(ids))).all()) if ids else {}


@router.post("/notes", response_model=JournalNoteOut, status_code=201)
def add_note(body: JournalNoteIn, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """A note on one of your trades, or a check-in (no trade): how you feel right now."""
    trade = _trade(session, user, body.trade_id) if body.trade_id is not None else None
    n = _add_note(session, user, trade, body)
    session.commit()
    return _note_out(n, trade.symbol if trade else None)


@router.get("/notes", response_model=list[JournalNoteOut])
def list_notes(start: date | None = None, end: date | None = None, stage: str | None = None,
               limit: int = Query(200, ge=1, le=1000), user: User = Depends(current_user),
               session: Session = Depends(get_session)):
    """Your notes, newest first: trading days start-end (New York), one stage (checkin: the check-ins)."""
    query = select(JournalNote).where(JournalNote.user_id == user.id)
    if start:
        query = query.where(JournalNote.at >= _day_start(start))
    if end:
        query = query.where(JournalNote.at < _day_start(end + timedelta(days=1)))
    if stage:
        query = query.where(JournalNote.stage == stage)
    notes = session.scalars(query.order_by(JournalNote.at.desc(), JournalNote.id.desc()).limit(limit)).all()
    symbols = _symbols(session, notes)
    return [_note_out(n, symbols.get(n.trade_id)) for n in notes]


@router.patch("/notes/{note_id}", response_model=JournalNoteOut)
def update_note(note_id: int, body: JournalNoteUpdate, user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    n = _note(session, user, note_id)
    changes = body.model_dump(exclude_unset=True)
    try:
        checked = JournalNoteIn.model_validate({"stage": n.stage, "at": n.at, "emotions": n.emotions, "intensity": n.intensity,
                                                "text": n.text} | changes)
    except ValidationError as e:
        raise HTTPException(422, _problems(e))
    if (n.trade_id is None) != (checked.stage == "checkin"):
        raise HTTPException(422, "a check-in has no trade, and a trade's note isn't a check-in")
    n.stage, n.at, n.emotions, n.intensity, n.text = checked.stage, checked.at, checked.emotions, checked.intensity, checked.text
    session.commit()
    return _note_out(n, _symbols(session, [n]).get(n.trade_id))


@router.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    session.delete(_note(session, user, note_id))
    session.commit()


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------

def image_type(data: bytes) -> str | None:
    """What an image file is from its first bytes, if it's one the journal takes (never SVG: it can carry scripts)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


async def _image_body(request: Request) -> bytes:
    """The upload's bytes, refused once they pass MAX_IMAGE_BYTES (never read whole into memory first)."""
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, f"the image is over {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    return bytes(data)


@router.post("/trades/{trade_id}/images", response_model=JournalImageOut, status_code=201)
def upload_image(trade_id: int, caption: str = Query("", max_length=200), data: bytes = Depends(_image_body),
                 user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Attach a screenshot: the request's body is the image file itself (PNG, JPEG, WebP or GIF)."""
    trade = _trade(session, user, trade_id)
    kind = image_type(data)
    if kind is None:
        raise HTTPException(415, "not an image the journal takes: PNG, JPEG, WebP or GIF")
    if session.scalar(select(func.count()).select_from(JournalImage).where(JournalImage.trade_id == trade.id)) >= MAX_IMAGES:
        raise HTTPException(409, f"a trade keeps at most {MAX_IMAGES} screenshots")
    image = JournalImage(user_id=user.id, trade_id=trade.id, content_type=kind, size=len(data), caption=caption.strip(), data=data)
    session.add(image)
    session.commit()
    return JournalImageOut.model_validate(image)


def _image(session: Session, user: User, image_id: int) -> JournalImage:
    image = session.get(JournalImage, image_id)
    if image is None or image.user_id != user.id:
        raise HTTPException(404, "image not found")
    return image


@router.get("/images/{image_id}")
def get_image(image_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    image = _image(session, user, image_id)
    # An image never changes (a new one gets a new id), so the browser may keep it; nosniff + no CSP sources: it's only
    # ever shown as the image type it was checked to be
    return Response(image.data, media_type=image.content_type,
                    headers={"Cache-Control": "private, max-age=604800, immutable", "X-Content-Type-Options": "nosniff",
                             "Content-Security-Policy": "default-src 'none'"})


@router.delete("/images/{image_id}", status_code=204)
def delete_image(image_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    session.delete(_image(session, user, image_id))
    session.commit()


# ---------------------------------------------------------------------------
# Days: the plan before the open, the review after the close; the calendar
# ---------------------------------------------------------------------------

def _day_start(day: date) -> datetime:
    """Midnight in New York at the start of a trading day."""
    return datetime.combine(day, time(), NY)


def _has_page(d: JournalDay | None) -> bool:
    return d is not None and any([d.plan, d.review, d.lessons, d.mood, d.energy, d.sleep, d.grade, d.followed_rules is not None])


@router.get("/days/{day}", response_model=JournalDayOut)
def get_day(day: date, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """A trading day's page, with that day's notes (check-ins and trade notes) and its trades."""
    page = session.scalar(select(JournalDay).where(JournalDay.user_id == user.id, JournalDay.day == day))
    notes = session.scalars(select(JournalNote).where(JournalNote.user_id == user.id, JournalNote.at >= _day_start(day),
                                                      JournalNote.at < _day_start(day + timedelta(days=1)))
                            .order_by(JournalNote.at, JournalNote.id)).all()
    symbols = _symbols(session, notes)
    trades = [t for t in session.scalars(select(JournalTrade).where(JournalTrade.user_id == user.id))
              if day in {ny_date(x) for x in (t.opened_at, t.closed_at) if x} or trade_day(t) == day]
    trades.sort(key=lambda t: (t.opened_at or t.closed_at or t.created_at, t.id))
    fields = {c: getattr(page, c) for c in JournalDayIn.model_fields} if page else {}
    return JournalDayOut(**fields, day=day, updated_at=page.updated_at if page else None,
                         notes=[_note_out(n, symbols.get(n.trade_id)) for n in notes], trades=_outs(session, trades),
                         pnl=round(sum(t.pnl for t in trades if t.pnl is not None and ny_date(t.closed_at) == day), 2))


@router.put("/days/{day}", response_model=JournalDayOut)
def save_day(day: date, body: JournalDayIn, user: User = Depends(current_user), session: Session = Depends(get_session)):
    page = session.scalar(select(JournalDay).where(JournalDay.user_id == user.id, JournalDay.day == day))
    if page is None:
        page = JournalDay(user_id=user.id, day=day)
        session.add(page)
    for f, v in body.model_dump().items():
        setattr(page, f, v)
    page.updated_at = utcnow()
    session.commit()
    return get_day(day, user, session)


@router.get("/calendar", response_model=list[JournalCalendarDayOut])
def calendar(start: date, end: date, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Each trading day from start to end that has something in the journal: its closed trades' P&L, the trades
    entered, the notes, and the day's page."""
    if end < start or (end - start).days > MAX_DAYS:
        raise HTTPException(422, f"end must be after start, at most {MAX_DAYS} days later")
    days: dict[date, dict] = {}

    def row(d: date) -> dict:
        return days.setdefault(d, {"day": d, "pnl": 0.0, "closed": 0, "wins": 0, "losses": 0, "opened": 0, "notes": 0,
                                   "mood": None, "grade": None, "page": False})

    for t in session.scalars(select(JournalTrade).where(JournalTrade.user_id == user.id)):
        if t.closed_at and start <= (d := ny_date(t.closed_at)) <= end and t.pnl is not None:
            r = row(d)
            r["pnl"] = round(r["pnl"] + t.pnl, 2)
            r["closed"] += 1
            r["wins"] += _result(t) == "win"
            r["losses"] += _result(t) == "loss"
        if t.opened_at and start <= (d := ny_date(t.opened_at)) <= end:
            row(d)["opened"] += 1
    for (at,) in session.execute(select(JournalNote.at).where(JournalNote.user_id == user.id, JournalNote.at >= _day_start(start),
                                                               JournalNote.at < _day_start(end + timedelta(days=1)))):
        row(ny_date(at))["notes"] += 1
    for page in session.scalars(select(JournalDay).where(JournalDay.user_id == user.id, JournalDay.day >= start,
                                                         JournalDay.day <= end)):
        if _has_page(page):
            r = row(page.day)
            r.update(mood=page.mood, grade=page.grade, page=True)
    return [JournalCalendarDayOut(**days[d]) for d in sorted(days)]


# ---------------------------------------------------------------------------
# Statistics, suggestions
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=JournalStatsOut)
def get_stats(f: Filters = Depends(), user: User = Depends(current_user), session: Session = Depends(get_session)):
    trades = matching(session, user, f)
    ids = {t.id for t in trades}
    entry: dict[int, list[str]] = {}
    felt: Counter = Counter()
    for trade_id, stage, at, emotions in session.execute(
            select(JournalNote.trade_id, JournalNote.stage, JournalNote.at, JournalNote.emotions)
            .where(JournalNote.user_id == user.id).order_by(JournalNote.at, JournalNote.id)):
        if trade_id in ids and stage in ENTRY_STAGES:
            entry[trade_id] = list(dict.fromkeys(entry.get(trade_id, []) + emotions))
        # How often each emotion came up: the filtered trades' notes; without a trade filter, the check-ins too
        if (trade_id in ids or (trade_id is None and not f.about_trades())) and f.in_range(ny_date(at)):
            felt.update(emotions)
    return stats(trades, entry, felt)


def _ranked(values) -> list[str]:
    """Distinct values, the most used first (ties: alphabetical); the first spelling of each (ignoring case) kept."""
    counts: Counter = Counter()
    spelling: dict[str, str] = {}
    for v in values:
        if v:
            spelling.setdefault(v.lower(), v)
            counts[v.lower()] += 1
    return [spelling[k] for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


@router.get("/options", response_model=JournalOptionsOut)
def options(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """What you wrote before, for the form's suggestions."""
    trades = session.scalars(select(JournalTrade).where(JournalTrade.user_id == user.id)).all()
    emotions = session.scalars(select(JournalNote.emotions).where(JournalNote.user_id == user.id)).all()
    return JournalOptionsOut(symbols=_ranked(t.symbol for t in trades), accounts=_ranked(t.account for t in trades),
                             setups=_ranked(t.setup for t in trades), tags=_ranked(x for t in trades for x in t.tags),
                             mistakes=_ranked(x for t in trades for x in t.mistakes),
                             emotions=_ranked(e for row in emotions for e in row))


# ---------------------------------------------------------------------------
# Importing the bots' trades
# ---------------------------------------------------------------------------

# A bot's strategy as an imported trade's setup (the short form of the dashboard's names, frontend/src/strategies.ts)
SETUP_NAMES = {DIP: "Dip buyer", ROTATION: "Momentum rotation", "sma_rsi": "SMA trend + RSI", "trend_following": "Trend following",
               "momentum": "Momentum", "breakout": "Breakout", "mean_reversion": "Mean reversion", "range_trading": "Range trading",
               "ma_pullback": "Pullback to the 20 EMA", "reversal": "Reversal", "gap_and_go": "Gap and go",
               "news_catalyst": "News catalyst", "fibonacci": "Fibonacci retracement"}


def _setup_name(bot: Bot) -> str:
    return SETUP_NAMES.get(bot.strategy, bot.strategy.replace("_", " ").capitalize())


def round_trips(session: Session, bot: Bot) -> list[dict]:
    """A bot's closed trades, one per sale that filled (a rotation's trim too): the journal trade's fields. The entry
    price is worked out from the sale's P&L as the bot booked it (its cost, buy fees included, per share), so the
    journal's P&L is the bot's to the cent. The entry time: the first buy since that symbol was last fully sold."""
    held: dict[str, float] = {}
    first_buy: dict[str, datetime] = {}
    trips = []
    for o in session.scalars(select(Order).where(Order.bot_id == bot.id, Order.filled_qty > 0).order_by(Order.created_at, Order.id)):
        symbol = o.symbol or bot.symbol
        if o.side == "BUY":
            first_buy.setdefault(symbol, o.created_at)
            held[symbol] = held.get(symbol, 0.0) + o.filled_qty
            continue
        opened = first_buy.get(symbol)
        held[symbol] = held.get(symbol, 0.0) - o.filled_qty
        if held[symbol] <= 1e-9:  # all sold: the next buy opens a new position
            held.pop(symbol)
            first_buy.pop(symbol, None)
        if o.pnl is None or not o.avg_price:
            continue
        entry = (o.avg_price * o.filled_qty - o.fee - o.pnl) / o.filled_qty
        if entry <= 0:
            continue
        stop = round(entry * (1 - bot.stop_pct), 4) if bot.strategy != ROTATION and bot.stop_pct else None
        trips.append({
            "order_id": o.id, "symbol": symbol, "quantity": o.filled_qty, "entry_price": entry, "stop_price": stop,
            "opened_at": opened or o.created_at, "closed_at": max(o.updated_at, opened or o.created_at),
            "exit_price": o.avg_price, "fees": o.fee, "exit_reason": EXIT_REASONS.get(o.reason, o.reason),
            "thesis": f"Imported from bot #{bot.id} ({bot.name})."
                      + (f" Stop = the bot's stop-loss ({bot.stop_pct:.0%}) under the cost per share." if stop else ""),
        })
    return trips


def _sells(session: Session) -> dict[int, int]:
    """Per bot: how many of its sales closed (part of) a position."""
    return dict(session.execute(select(Order.bot_id, func.count()).where(Order.side == "SELL", Order.filled_qty > 0,
                                                                           Order.pnl.is_not(None)).group_by(Order.bot_id)).all())


@router.get("/import/bots", response_model=list[JournalImportBotOut])
def import_choices(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """The bots with closed trades to import, live ones first, and how many of them are in your journal already."""
    sells = _sells(session)
    done = dict(session.execute(select(JournalTrade.bot_id, func.count()).where(JournalTrade.user_id == user.id,
                                                                                  JournalTrade.source == "bot")
                                .group_by(JournalTrade.bot_id)).all())
    live = {b["name"]: b["live"] for b in catalog()}
    bots = session.scalars(select(Bot).where(Bot.id.in_(sells))).all() if sells else []
    out = [JournalImportBotOut(id=b.id, name=b.name, strategy=b.strategy, broker=b.broker, live=live.get(b.broker, False),
                               status=b.status, closed_trades=sells[b.id], imported=done.get(b.id, 0)) for b in bots]
    return sorted(out, key=lambda b: (not b.live, b.status == "archived", -b.id))


@router.post("/import", response_model=JournalImportOut)
def import_bot_trades(body: JournalImportIn, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Copy the chosen bots' closed trades into your journal (tagged "bot"), each sale once: importing again adds only
    the new ones. Then journal them like your own: how you felt about what the bot did, and your review."""
    have = set(session.scalars(select(JournalTrade.source_key).where(JournalTrade.user_id == user.id,
                                                                     JournalTrade.source_key.is_not(None))))
    imported = already = 0
    for bot_id in dict.fromkeys(body.bot_ids):
        bot = session.get(Bot, bot_id)
        if bot is None:
            raise HTTPException(404, f"bot #{bot_id} not found")
        for trip in round_trips(session, bot):
            key = f"order:{trip.pop('order_id')}"
            if key in have:
                already += 1
                continue
            trade = JournalTrade(user_id=user.id, source="bot", source_key=key, bot_id=bot.id)
            apply(trade, JournalTradeIn(status="closed", side="long", account=ACCOUNTS.get(bot.broker, bot.broker),
                                        setup=_setup_name(bot), tags=["bot"], **trip))
            session.add(trade)
            have.add(key)
            imported += 1
    session.commit()
    return JournalImportOut(imported=imported, already=already)
