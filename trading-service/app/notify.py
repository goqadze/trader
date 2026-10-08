"""Email alerts: buys and sells, stop-loss sales and blacklisted symbols, the drawdown breaker, errors, and (if you want
them) the dip buyer's signals, sent to the addresses set on the dashboard's Email alerts page.

An outbox: the rest of the service only QUEUES an email (a Notification row, in the same transaction as the event it
tells about), and run_forever() sends what is queued every few seconds, everything that piled up in one email, retrying
a failed send later. So a slow or broken mail server never holds up or breaks trading, an event that was rolled back
never emails, and a restart loses nothing.

Any SMTP server works (SMTP_* in .env). Gmail: smtp.gmail.com, port 587, your address and an App Password. Port 587
is also the one a new Hetzner server may use (it blocks 25 and 465)."""

import asyncio
import logging
import re
import smtplib
import ssl
from collections.abc import Callable
from datetime import datetime, timedelta
from email.message import EmailMessage
from html import escape

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, utcnow
from .market import NY
from .models import DIP, Bot, Holding, Notification, Order, Setting, Signal
from .shares import fmt_qty

logger = logging.getLogger("trading-service.notify")

KEY = "email_alerts"  # its Setting row
# What you can choose to get (models.NOTIFY_CATEGORIES): (label, what it covers), in the dashboard's words
CATEGORIES = {
    "trades": ("Buys and sells", "Every filled order; a sale with its profit or loss"),
    "risk": ("Stop-loss and risk", "Stop-loss sales, blacklisted symbols, the drawdown breaker, HALT ALL"),
    "problems": ("Problems", "Errors, and orders that failed or need a look (at most one per bot and kind an hour)"),
    "signals": ("Dip signals", "Every dip buyer signal: buy zone, turned up, back up, held too long, news veto (chatty)"),
}
DEFAULTS = {"enabled": False, "recipients": [], "categories": ["trades", "risk", "problems"]}
SEND_EVERY_S = 15
MAX_PER_EMAIL = 50
RETRY_AFTER = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))  # then "failed"
PROBLEM_QUIET = timedelta(hours=1)  # a bot failing every check emails once an hour, not every 15 minutes
COLORS = {"trades": "#4f8cff", "risk": "#ef5b6b", "problems": "#d4a72c", "signals": "#13c2c2", "test": "#33c088"}
REASONS = {"dip": "bought the dip", "signal": "buy signal", "target": "back at its target", "stop-loss": "stop-loss",
           "time": "held too long", "manual": "you asked", "rebalance": "rebalance"}
SIGNAL_HEADS = {"down": "in the buy zone", "rebound": "turned up from its low", "up": "is back up", "stop": "hit its stop",
                "time": "held too long", "news": "bearish news, not bought"}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load(session: Session) -> dict:
    """Who gets the alerts and about what: {"enabled", "recipients", "categories"}."""
    row = session.get(Setting, KEY)
    return {**DEFAULTS, **(row.value if row else {})}


def save(session: Session, value: dict, now: datetime) -> dict:
    row = session.get(Setting, KEY)
    if row is None:
        session.add(Setting(key=KEY, value=value, updated_at=now))
    else:
        row.value, row.updated_at = value, now
    return {**DEFAULTS, **value}


def smtp_ready() -> bool:
    return bool(settings.smtp_host)


def sender() -> str:
    return settings.smtp_from or settings.smtp_username


def smtp_server() -> str:
    """"smtp.gmail.com:587 (starttls)", for the dashboard; empty when none is set up."""
    return f"{settings.smtp_host}:{settings.smtp_port} ({settings.smtp_security})" if smtp_ready() else ""


def dashboard_url() -> str:
    return settings.dashboard_url


# ---------------------------------------------------------------------------
# Queueing: called from where things happen, inside their transaction
# ---------------------------------------------------------------------------

def enqueue(session: Session, category: str, kind: str, subject: str, body: str, bot_id: int | None = None,
            now: datetime | None = None) -> Notification | None:
    """Queue an email when alerts are on and this category is wanted. Never raises: an alert must not break a trade."""
    try:
        if not smtp_ready():
            return None
        cfg = load(session)
        if not cfg["enabled"] or not cfg["recipients"] or category not in cfg["categories"]:
            return None
        now = now or utcnow()
        if category == "problems" and session.scalar(
                select(Notification.id).where(Notification.category == category, Notification.kind == kind,
                                              Notification.bot_id.is_(None) if bot_id is None else Notification.bot_id == bot_id,
                                              Notification.created_at > now - PROBLEM_QUIET).limit(1)):
            return None
        n = Notification(created_at=now, bot_id=bot_id, category=category, kind=kind[:32], subject=subject[:200], body=body)
        session.add(n)
        return n
    except Exception:
        logger.exception("could not queue an email alert")
        return None


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _bot_line(bot: Bot) -> str:
    where = {"paper": "paper (built-in simulator)", "alpaca-paper": "Alpaca paper account",
             "alpaca-live": "REAL MONEY (Alpaca live)"}.get(bot.broker, bot.broker)
    return f"Bot: {bot.name}, {where} · cash ${bot.cash:,.2f}"


def _live(bot: Bot | None) -> str:
    return "[LIVE] " if bot is not None and bot.broker == "alpaca-live" else ""


def trade(session: Session, bot: Bot, order: Order, now: datetime) -> None:
    """A filled order (trader.apply_broker_state). A stop-loss sale counts as risk, every other fill as a trade."""
    symbol, qty, price = order.symbol or bot.symbol, order.filled_qty, order.avg_price
    why = REASONS.get(order.reason, order.reason)
    if order.side == "BUY":
        subject = f"{_live(bot)}BUY {symbol}: {fmt_qty(qty)} @ ${price:,.2f}"
        lines = [f"Bought {fmt_qty(qty)} {symbol} at ${price:,.2f} (${qty * price:,.2f}{f' + ${order.fee:,.2f} fee' if order.fee else ''}; {why})."]
        if bot.strategy == DIP:
            h = session.scalar(select(Holding).where(Holding.bot_id == bot.id, Holding.symbol == symbol))
            target, stop = (h.target_price, h.stop_price) if h else (None, None)
        else:
            target, stop = bot.target_price, bot.stop_price
        if target and stop:
            lines.append(f"Sells at its target ${target:,.2f} ({target / price - 1:+.1%}) or its stop-loss ${stop:,.2f} "
                         f"({stop / price - 1:+.1%}).")
    else:
        pnl = order.pnl
        basis = qty * price - order.fee - pnl if pnl is not None else 0.0
        result = (f" {_money(pnl)}" + (f" ({pnl / basis:+.1%})" if basis else "")) if pnl is not None else ""
        label = "STOP-LOSS" if order.reason == "stop-loss" else "SELL"
        subject = f"{_live(bot)}{label} {symbol}{result}"
        lines = [f"Sold {fmt_qty(qty)} {symbol} at ${price:,.2f} ({why})."]
        if pnl is not None:
            lines.append(f"Profit/loss {_money(pnl)}" + (f" ({pnl / basis:+.1%} on ${basis:,.2f})" if basis else "")
                         + ", after fees.")
    lines.append(_bot_line(bot))
    enqueue(session, "risk" if order.reason == "stop-loss" else "trades", order.side.lower(), subject, "\n".join(lines),
            bot.id, now)


def from_event(session: Session, bot_id: int | None, kind: str, message: str, level: str, now: datetime | None) -> None:
    """trader.log_event: what in the audit log deserves an email. Routine (info) events don't."""
    if kind == "error" or (level in ("warning", "error") and kind in ("order", "reconcile")):
        category = "problems"
    elif level in ("warning", "error") and kind in ("risk", "halt"):
        category = "risk"
    else:
        return
    bot = session.get(Bot, bot_id) if bot_id else None
    head = re.split(r"(?<=[a-z0-9)])\. ", message, maxsplit=1)[0].rstrip(".")
    subject = f"{_live(bot)}{bot.name + ': ' if bot else ''}{head}"
    body = message + ("\n" + _bot_line(bot) if bot else "")
    enqueue(session, category, kind, subject, body, bot_id, now)


def dip_signal(session: Session, bot: Bot, s: Signal) -> None:
    """dip.signal: a recommendation, for those who want every one of them."""
    change = f" ({s.change_pct:+.1f}%)" if s.change_pct is not None else ""
    subject = f"{_live(bot)}{s.symbol} {SIGNAL_HEADS.get(s.kind, s.kind)}{change}"
    body = "\n".join(x for x in (s.message, s.outcome, _bot_line(bot)) if x)
    enqueue(session, "signals", f"signal-{s.kind}", subject, body, bot.id, s.created_at)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _when(t: datetime) -> str:
    return f"{t.astimezone(NY):%a %b %d, %H:%M} New York ({t:%H:%M} UTC)"


def _link(n: Notification) -> str:
    return f"{settings.dashboard_url}/trading/{n.bot_id}" if n.bot_id else settings.dashboard_url


def compose(rows: list[Notification], recipients: list[str]) -> EmailMessage:
    """One email for everything that piled up: a plain-text and an HTML version."""
    if len(rows) == 1:
        subject = rows[0].subject
    else:
        subject = f"{len(rows)} alerts: " + " · ".join(r.subject for r in rows[:3]) + (" …" if len(rows) > 3 else "")
    msg = EmailMessage()
    msg["Subject"] = f"[Trading] {subject}"[:250]
    msg["From"] = sender()
    msg["To"] = ", ".join(recipients)
    text = "\n\n----------\n\n".join(f"{r.subject}\n{_when(r.created_at)}\n\n{r.body}\n\n{_link(r)}" for r in rows)
    msg.set_content(text + "\n\n--\nTrading dashboard email alerts. Choose what you get under Email alerts on the dashboard.")
    blocks = "".join(
        f'<div style="border-left:4px solid {COLORS.get(r.category, "#888")};padding:8px 12px;margin:0 0 14px">'
        f'<div style="font-size:15px;font-weight:600">{escape(r.subject)}</div>'
        f'<div style="color:#777;font-size:12px;margin:2px 0 8px">{escape(_when(r.created_at))} · {escape(r.category)}</div>'
        f'<div style="white-space:pre-wrap;font-size:14px">{escape(r.body)}</div>'
        f'<div style="margin-top:8px"><a href="{escape(_link(r))}">Open in the dashboard</a></div></div>'
        for r in rows)
    msg.add_alternative(
        f'<!doctype html><html><head><meta charset="utf-8"></head><body>'
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px">{blocks}'
        f'<div style="color:#999;font-size:11px">Trading dashboard email alerts. Choose what you get under Email alerts '
        f'on the dashboard.</div></div></body></html>', subtype="html")
    return msg


def deliver(msg: EmailMessage) -> None:
    """Hand one email to the SMTP server (raises when it can't)."""
    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    with server:
        if settings.smtp_security == "starttls":
            server.starttls(context=context)
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)


def send_pending(now: datetime | None = None, send: Callable[[EmailMessage], None] | None = None) -> int:
    """Send what is queued, in one email; a failure is retried after RETRY_AFTER, then given up. Returns how many alerts
    went out."""
    now, send = now or utcnow(), send or deliver
    with SessionLocal() as session:
        rows = list(session.scalars(
            select(Notification).where(Notification.status == "pending",
                                       or_(Notification.next_attempt_at.is_(None), Notification.next_attempt_at <= now))
            .order_by(Notification.id).limit(MAX_PER_EMAIL)))
        if not rows:
            return 0
        cfg = load(session)
        if not smtp_ready() or not cfg["enabled"] or not cfg["recipients"]:
            for r in rows:
                r.status, r.error = "skipped", "email alerts were turned off before it went out"
            session.commit()
            return 0
        try:
            send(compose(rows, cfg["recipients"]))
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:500]
            for r in rows:
                r.attempts += 1
                r.error = error
                if r.attempts > len(RETRY_AFTER):
                    r.status = "failed"
                else:
                    r.next_attempt_at = now + RETRY_AFTER[r.attempts - 1]
            session.commit()
            logger.warning("email alert not sent (%d alerts): %s", len(rows), error)
            return 0
        for r in rows:
            r.attempts += 1
            r.status, r.sent_at, r.error = "sent", now, None
        session.commit()
        return len(rows)


def send_test(session: Session, recipients: list[str], now: datetime,
              send: Callable[[EmailMessage], None] | None = None) -> Notification:
    """Send a test email right away (raises with the server's answer when it fails); kept in the history either way."""
    cfg = load(session)
    wanted = ", ".join(CATEGORIES[c][0].lower() for c in cfg["categories"] if c in CATEGORIES) or "nothing yet"
    n = Notification(created_at=now, category="test", kind="test", subject="Test email: alerts work",
                     body=f"This address will get the trading alerts you chose: {wanted}.\n"
                          f"Sent through {settings.smtp_host}:{settings.smtp_port} as {sender()}.")
    session.add(n)
    try:
        (send or deliver)(compose([n], recipients))
    except Exception as e:
        n.status, n.attempts, n.error = "failed", 1, f"{type(e).__name__}: {e}"[:500]
        session.commit()
        raise
    n.status, n.attempts, n.sent_at = "sent", 1, now
    session.commit()
    return n


async def run_forever() -> None:
    """Started by main.py next to the scheduler; cancelled on shutdown."""
    while True:
        try:
            await asyncio.to_thread(send_pending)
        except Exception:
            logger.exception("sending email alerts failed")
        await asyncio.sleep(SEND_EVERY_S)
