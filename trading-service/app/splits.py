"""Stock splits: the bots' records follow them, so a split never looks like a crash (or a jump) to a stop-loss.

On a split's ex-date the shares and the price change by the same ratio: NVDA's 10-for-1 on June 10 2024 turned 2 shares
at $1,200 into 20 at $120. Alpaca changes the account by itself before the open; a bot's records don't know. Without
this, the first check after a split sees the price ten times under the stop and sells the bot's old 2 shares at $120.

So once a day, before any bot looks at its holdings (scheduler.tick -> apply_all; dip.check too):
  - each holding bought before a split's ex-date gets it: shares x ratio; its buy, reference, target and stop prices and
    its last price / ratio (what it cost stays the same);
  - a fall a bot was following (wait for the turn) that started on or before the ex-date is dropped: Yahoo's bars may
    not have been adjusted yet that day; the next fall starts from adjusted ones;
  - on the ex-date itself the symbol isn't bought or followed (dip.check), for the same reason.
A reverse split that leaves a fraction of a share for whole-share holdings is paid in cash by the broker: the records
then differ from the account, and the account check pauses the bot for you to look.

Where splits come from: Alpaca's corporate actions (the source of the account's own changes) when keys are set, else
Yahoo; fetched once a day per symbol, retried after FAIL_RETRY when both fail. When nothing answers, nothing changes:
dip.check's own guard (a price 45%+ away from the last one while the account's shares changed by the same ratio)
keeps an unrecorded split from selling.
"""

import logging
import threading
from datetime import date, datetime, timedelta

import httpx
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .market import ny_date
from .models import DIP, Bot, Holding, WatchItem
from .shares import fmt_qty, tidy
from .trader import bot_lock, holdings, log_event

logger = logging.getLogger("trading-service")

Split = tuple[date, float]  # (ex-date, new shares per old share): 10.0 for a 10-for-1, 0.1 for a 1-for-10
ALPACA_URL = "https://data.alpaca.markets/v1/corporate-actions"
HISTORY_DAYS = 400  # how far back a day's fetch reaches: further than any holding goes without a daily pass
FAIL_RETRY = timedelta(minutes=15)

_cache: dict[str, tuple[date, list[Split]]] = {}  # symbol -> (the day it was fetched, its splits)
_failed_at: dict[str, datetime] = {}
_lock = threading.Lock()


def alpaca_splits(symbols: list[str], start: date, end: date, get=httpx.get) -> dict[str, list[Split]]:
    """Forward and reverse splits with an ex-date from start to end, from Alpaca's corporate actions (market data)."""
    key, secret = ((settings.alpaca_paper_key_id, settings.alpaca_paper_secret_key) if settings.alpaca_paper_key_id
                   else (settings.alpaca_live_key_id, settings.alpaca_live_secret_key))
    if not (key and secret):
        raise RuntimeError("no Alpaca keys")
    out: dict[str, list[Split]] = {s: [] for s in symbols}
    params = {"symbols": ",".join(symbols), "types": "forward_split,reverse_split", "start": start.isoformat(),
              "end": end.isoformat(), "limit": 1000}
    while True:
        r = get(ALPACA_URL, params=params, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Alpaca corporate actions {r.status_code}: {r.text[:200]}")
        body = r.json()
        actions = body.get("corporate_actions") or {}
        for a in (actions.get("forward_splits") or []) + (actions.get("reverse_splits") or []):
            if a.get("symbol") in out and a.get("old_rate") and a.get("new_rate"):
                out[a["symbol"]].append((date.fromisoformat(a["ex_date"]), float(a["new_rate"]) / float(a["old_rate"])))
        if not body.get("next_page_token"):
            return out
        params = {**params, "page_token": body["next_page_token"]}


def yahoo_splits(symbols: list[str], start: date, end: date) -> dict[str, list[Split]]:
    """The same from Yahoo (its split history: the ratio by ex-date)."""
    out: dict[str, list[Split]] = {}
    for s in symbols:
        history = yf.Ticker(s.replace(".", "-")).splits  # Yahoo writes share classes with a dash (BRK-B)
        out[s] = [(t.date(), float(r)) for t, r in history.items() if start <= t.date() <= end and r > 0]
    return out


def fetch(symbols: list[str], today: date, now: datetime | None = None) -> dict[str, list[Split]]:
    """Each symbol's splits through today: fetched once a day (Alpaca, else Yahoo), from the cache after that.
    Raises when neither answers (and doesn't ask again before FAIL_RETRY)."""
    now = now or datetime.now().astimezone()
    with _lock:
        need = [s for s in symbols if _cache.get(s, (None,))[0] != today]
        if need and any(now - _failed_at[s] < FAIL_RETRY for s in need if s in _failed_at):
            raise RuntimeError("split data unavailable (retried later)")
    if need:
        start = today - timedelta(days=HISTORY_DAYS)
        try:
            found = alpaca_splits(need, start, today)
        except Exception as e:
            logger.warning("splits from Alpaca failed (%s); asking Yahoo", e)
            try:
                found = yahoo_splits(need, start, today)
            except Exception as e2:
                with _lock:
                    _failed_at.update({s: now for s in need})
                raise RuntimeError(f"split data unavailable: Alpaca ({e}), Yahoo ({e2})") from e2
        with _lock:
            for s in need:
                _cache[s] = (today, sorted(found.get(s, [])))
                _failed_at.pop(s, None)
    with _lock:
        return {s: _cache[s][1] for s in symbols if s in _cache}


def forget(symbol: str) -> None:
    """Ask again at the next check (dip.check suspects a split the data didn't show)."""
    with _lock:
        _cache.pop(symbol, None)
        _failed_at.pop(symbol, None)


def _ratio_text(ratio: float) -> str:
    return f"{fmt_qty(ratio)}-for-1" if ratio >= 1 else f"1-for-{fmt_qty(1 / ratio)}"


def apply(session: Session, bot: Bot, today: date, splits_fn=None, now: datetime | None = None) -> set[str]:
    """Bring one bot's records up to date with the splits through today (see the module docstring). Returns the
    symbols whose ex-date is today. When the split data can't be had, changes nothing and returns an empty set."""
    held = holdings(session, bot)
    items = list(session.scalars(select(WatchItem).where(WatchItem.bot_id == bot.id)))
    symbols = sorted({h.symbol for h in held} | {i.symbol for i in items})
    if not symbols:
        return set()
    try:
        known = (splits_fn or fetch)(symbols, today)
    except Exception as e:
        logger.warning("bot=%s splits: %s", bot.id, e)
        return set()
    for h in held:  # each split with an ex-date after the buy (or the last one applied), once
        for ex, ratio in known.get(h.symbol, []):
            if (h.splits_through or ny_date(h.opened_at)) < ex <= today and ratio > 0 and abs(ratio - 1) > 1e-9:
                before = (h.shares, h.entry_price, h.stop_price, h.target_price)
                h.shares = tidy(h.shares * ratio)
                for field in ("entry_price", "reference_price", "target_price", "stop_price"):
                    if getattr(h, field) is not None:
                        setattr(h, field, round(getattr(h, field) / ratio, 4))
                if h.last_price and (h.last_price_at is None or ny_date(h.last_price_at) < ex):
                    h.last_price = h.last_price / ratio
                log_event(session, bot.id, "split", f"{h.symbol} split {_ratio_text(ratio)} (ex-date {ex:%a %b %d}): "
                          f"{fmt_qty(before[0])} -> {fmt_qty(h.shares)} shares; bought at ${before[1] or 0:,.2f} -> "
                          f"${h.entry_price or 0:,.2f}, stop ${before[2] or 0:,.2f} -> ${h.stop_price or 0:,.2f}, target "
                          f"${before[3] or 0:,.2f} -> ${h.target_price or 0:,.2f} (what it cost is the same)", now=now)
                h.splits_through = ex  # not today: a split the data shows only later that day must still apply
    for i in items:
        if i.dip_reference and i.armed_at and any(ny_date(i.armed_at) <= ex <= today for ex, _ in known.get(i.symbol, [])):
            i.dip_reference = i.armed_at = i.turned_at = i.trough_price = i.trough_at = None  # dip.end_fall
    return {s for s, found in known.items() if any(ex == today for ex, _ in found)}


def apply_all(now: datetime, splits_fn=None) -> None:
    """Every dip bot's records, before a scheduler tick handles the bots in parallel: so the account check of one bot
    sharing a symbol never compares the account with another bot's records from before a split. A bot busy elsewhere
    (an API action) is done at the next tick, or by its own check."""
    today = ny_date(now)
    with SessionLocal() as session:
        ids = list(session.scalars(select(Bot.id).where(Bot.strategy == DIP, Bot.status != "archived")))
        for bot_id in ids:
            lock = bot_lock(bot_id)
            if not lock.acquire(blocking=False):
                continue
            try:
                bot = session.get(Bot, bot_id)
                if bot is not None and session.scalar(select(Holding.id).where(Holding.bot_id == bot_id).limit(1)):
                    apply(session, bot, today, splits_fn, now)
                    session.commit()
            except Exception:
                session.rollback()
                logger.exception("bot=%s split pass failed", bot_id)
            finally:
                lock.release()
