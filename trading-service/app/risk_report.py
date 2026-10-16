"""The daily risk report: an email before the open saying where trading looks risky today, and how risky.

It reads, for every sector the bots watch or hold:
  - the sector's own news: decision-service's LLM on the headlines about the sector's ETFs and biggest companies
    published so far (up to 9:25 New York at most; GET /news/sector), and the whole market's the same way
    (GET /news/market);
  - how the sector's ETF moved yesterday and over the last 5 days;
  - which symbols move with it: the correlation of their daily returns with the sector's ETF over the last 120 trading
    days (0.6 or more: grid stocks move with chips), so a symbol of a calm sector that moves with a sector in trouble
    is flagged too;
  - the macro calendar (macro_dates.json): the consumer price index (CPI), the jobs report and the Fed's rate decision,
    today, tomorrow and in the coming week.
Then it rates each symbol HIGH, ELEVATED or NORMAL (see LEVELS) and adds up what the real-money and the paper bots hold
at each level.

Information only: the bots trade as usual. Tested as buy filters on 230 dip setups (Oct 2026, Mac study "Dip sweep 4"),
skipping buys on these signals cost more than it saved: on fearful days dip buying finds its best prices.

Sent once per trading day at REPORT_AT, 8:45 New York (run_forever, started by main.py), as an email of the "report"
category when email alerts are on and that category is chosen; its first line says how far the news goes. GET
/risk-report shows the same report without sending it. Every function takes the day and the data sources as arguments,
so tests build a report without the network."""

import asyncio
import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import notify
from .db import SessionLocal, utcnow
from .decision_client import get_market_mood, get_sector_mood
from .market import NY, ny_date, session_bounds
from .models import DIP, ROTATION, Bot, Holding, Notification, SymbolSector, WatchItem
from .rotation import yahoo_closes

logger = logging.getLogger("trading-service.risk_report")

# New York: after the 8:30 CPI and jobs reports, 45 minutes before the open (the bots' first checks: 9:35-9:45), so
# there is time to pause a bot. The news is read up to the moment the report is built (decision-service stops at 9:25)
REPORT_AT = time(8, 45)
NEWS_CUTOFF = time(9, 25)  # decision-service market_news.CUTOFF
LATE_UNTIL = time(15, 30)  # a service that was down at REPORT_AT still sends the day's report until then
CHECK_EVERY_S = 60

# Yahoo's names for the ETFs' categories, as the stocks' sectors (the index funds' category is the market itself)
MARKET = "Whole market"
CANON = {"Communications": "Communication Services", "Financial": "Financial Services", "Health": "Healthcare",
         "Equity Energy": "Energy", "Commodities Broad Basket": "Commodities Focused", "Large Blend": MARKET,
         "Large Growth": MARKET, "Large Value": MARKET, "Small Blend": MARKET, "Mid-Cap Blend": MARKET}
MARKET_ETFS = ("SPY", "QQQ")
# Each sector's ETF(s): its moves, and what moves with it (the market isn't one: nearly everything moves with it)
SECTOR_ETFS = {
    "Technology": ("XLK", "SMH"), "Communication Services": ("XLC",), "Consumer Cyclical": ("XLY",),
    "Consumer Defensive": ("XLP",), "Energy": ("XLE",), "Financial Services": ("XLF",), "Healthcare": ("XLV",),
    "Industrials": ("XLI",), "Utilities": ("XLU",), "Basic Materials": ("XLB",), "Real Estate": ("XLRE",),
    "Commodities Focused": ("DBC",), "Equity Precious Metals": ("GDX",), "Natural Resources": ("XME",),
}
LINK_DAYS = 120  # trading days of daily returns the correlation is measured over
LINK = 0.6  # "moves with": a correlation at least this
HISTORY_DAYS = 200  # calendar days of daily closes: 121+ trading days
SECTOR_RECHECK = timedelta(days=30)  # a symbol Yahoo couldn't place is asked again after this
MOOD_WORKERS = 4  # sectors whose news is read at once

LEVELS = {
    "HIGH": "the sector's own news reads risk-off this morning (worse than for the market), or that of a sector the "
            "symbol moves with does; for the index funds, the market's news",
    "ELEVATED": "the whole market's news reads risk-off, or a CPI, jobs report or Fed decision is due today",
    "NORMAL": "none of these",
}
RANK = {"HIGH": 2, "ELEVATED": 1, "NORMAL": 0}


# ---------------------------------------------------------------------------
# The macro calendar
# ---------------------------------------------------------------------------

KINDS = {"cpi": "CPI", "jobs": "jobs report", "fed": "Fed decision"}


@lru_cache(maxsize=1)
def calendar() -> dict:
    """macro_dates.json: the release dates and their sources. BLS and the Fed publish a year ahead: add the next year's
    dates before `known_through` (the report says so once it is near)."""
    return json.loads(Path(__file__).with_name("macro_dates.json").read_text())


def releases(start: date, end: date) -> list[tuple[date, str]]:
    """(day, "CPI" | "jobs report" | "Fed decision") between start and end, in order."""
    out = [(date.fromisoformat(d), label) for key, label in KINDS.items() for d in calendar()[key]]
    return sorted((d, label) for d, label in out if start <= d <= end)


# ---------------------------------------------------------------------------
# Sectors, moves and links
# ---------------------------------------------------------------------------

def yahoo_sector(symbol: str) -> str | None:
    """Yahoo's sector for a stock (Technology ...), its category for an ETF (Equity Energy ...); None when it can't
    tell. Raises when Yahoo fails."""
    info = yf.Ticker(symbol.replace(".", "-")).info or {}
    return info.get("sector") or info.get("category") or None


def sectors_of(session: Session, symbols: list[str], now: datetime, lookup) -> dict[str, str]:
    """symbol -> its sector, kept in symbol_sectors (looked up once; an unknown one again after SECTOR_RECHECK). A
    symbol Yahoo can't place is left out."""
    have = {r.symbol: r for r in session.scalars(select(SymbolSector).where(SymbolSector.symbol.in_(symbols)))}
    for s in symbols:
        row = have.get(s)
        if row is not None and (row.sector or now - row.checked_at < SECTOR_RECHECK):
            continue
        try:
            sector = lookup(s)
        except Exception as e:  # Yahoo down: asked again next time
            logger.warning("sector of %s unknown: %s", s, e)
            continue
        if row is None:
            row = have[s] = SymbolSector(symbol=s)
            session.add(row)
        row.sector, row.checked_at = sector, now
    return {s: r.sector for s, r in have.items() if r.sector}


def moves(closes: pd.DataFrame, ticker: str) -> tuple[float, float] | None:
    """(yesterday's change, the last 5 days') of a ticker's daily closes before today; None without them."""
    if ticker not in closes:
        return None
    c = closes[ticker].dropna()
    if len(c) < 6:
        return None
    return float(c.iloc[-1] / c.iloc[-2] - 1), float(c.iloc[-1] / c.iloc[-6] - 1)


def links(closes: pd.DataFrame, symbols: list[str]) -> dict[str, dict[str, float]]:
    """symbol -> {sector: the correlation of its daily returns with the sector's ETF (the higher of two) over the last
    LINK_DAYS returns}; sectors it can't be measured against are left out."""
    r = closes.pct_change(fill_method=None).tail(LINK_DAYS)
    out = {}
    for s in symbols:
        mine = {}
        for sector, tickers in SECTOR_ETFS.items():
            best = None
            for t in tickers:
                if s not in r or t not in r or s == t:
                    continue
                pair = r[[s, t]].dropna()
                if len(pair) < LINK_DAYS or pair[t].std() == 0 or pair[s].std() == 0:
                    continue
                c = float(pair[s].corr(pair[t]))
                best = c if best is None else max(best, c)
            if best is not None:
                mine[sector] = best
        out[s] = mine
    return out


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _money(x: float) -> str:
    return f"${x:,.0f}"


def _day(d: date) -> str:
    return f"{d:%a} {d:%b} {d.day}"


def positions(session: Session) -> tuple[list[Bot], dict[int, dict[str, float]], dict[str, set[int]]]:
    """The bots that aren't archived; per bot, what it holds ({symbol: dollars}); per symbol, the bots watching or
    holding it."""
    bots = list(session.scalars(select(Bot).where(Bot.status != "archived").order_by(Bot.id)))
    ids = [b.id for b in bots]
    held: dict[int, dict[str, float]] = defaultdict(dict)
    by_symbol: dict[str, set[int]] = defaultdict(set)
    for h in session.scalars(select(Holding).where(Holding.bot_id.in_(ids), Holding.shares > 0)):
        held[h.bot_id][h.symbol] = h.shares * (h.last_price or h.entry_price or 0.0)
        by_symbol[h.symbol].add(h.bot_id)
    for w in session.scalars(select(WatchItem).where(WatchItem.bot_id.in_(ids))):
        by_symbol[w.symbol].add(w.bot_id)
    for b in bots:
        if b.strategy == ROTATION:
            for s in b.universe or []:
                by_symbol[s].add(b.id)
        elif b.strategy != DIP:  # a one-symbol bot: its position lives on the bot
            by_symbol[b.symbol].add(b.id)
            if b.shares:
                held[b.id][b.symbol] = b.shares * (b.last_price or 0.0)
    return bots, held, by_symbol


def live_slots(session: Session, live: list[Bot], held: dict[int, dict[str, float]], sym_rows: dict) -> list[dict]:
    """Per real-money bot: its slots (max_positions; a one-symbol bot has one), what each held one is worth, how far it
    is from the buy and from its stop-loss, with its level; and the watched symbols its free slots could buy today."""
    ids = [b.id for b in live]
    holding = {(h.bot_id, h.symbol): h
               for h in session.scalars(select(Holding).where(Holding.bot_id.in_(ids), Holding.shares > 0))}
    watch = defaultdict(list)
    for w in session.scalars(select(WatchItem).where(WatchItem.bot_id.in_(ids)).order_by(WatchItem.symbol)):
        watch[w.bot_id].append(w)
    out = []
    for b in live:
        items = held.get(b.id, {})
        rows = []
        for s, value in sorted(items.items()):
            h = holding.get((b.id, s))
            last = (h.last_price if h else b.last_price) or 0.0
            entry = (h.entry_price if h else None) or 0.0
            stop = h.stop_price if h else None
            level, why, sector = sym_rows.get(s, ("NORMAL", [], None))
            rows.append({"symbol": s, "value": round(value, 2), "sector": sector, "level": level, "why": why,
                         "pnl_pct": round(last / entry - 1, 4) if entry and last else None,
                         "stop_gap": round(stop / last - 1, 4) if stop and last else None})
        slots = (b.max_positions or 1) if b.strategy == DIP else 1
        out.append({"id": b.id, "name": b.name, "status": b.status, "cash": round(b.cash, 2), "slots": slots,
                    "free_slots": max(0, slots - len(items)), "held": rows,
                    "watch": [{"symbol": w.symbol, "level": sym_rows.get(w.symbol, ("NORMAL",))[0],
                               "sector": sym_rows.get(w.symbol, (None, None, None))[2],
                               "blacklisted": w.status == "blacklisted"}
                              for w in watch[b.id] if w.symbol not in items]})
    return out


def _more(items: list[str], n: int) -> str:
    return ", ".join(items[:n]) + (f" +{len(items) - n} more" if len(items) > n else "")


def build(session: Session, today: date, now: datetime, mood_fn=None, sector_mood_fn=None, closes_fn=None,
          sector_lookup=None) -> dict:
    """The day's report: {"subject", "text", "overall", "market", "sectors", "symbols"}. The sources can be swapped
    (tests): mood_fn(day), sector_mood_fn(sector, day), closes_fn(symbols, start) -> daily closes, sector_lookup(symbol)."""
    mood_fn, sector_mood_fn = mood_fn or get_market_mood, sector_mood_fn or get_sector_mood
    closes_fn, sector_lookup = closes_fn or yahoo_closes, sector_lookup or yahoo_sector
    bots, held, by_symbol = positions(session)
    symbols = sorted(by_symbol)
    sectors = {s: CANON.get(g, g) for s, g in sectors_of(session, symbols, now, sector_lookup).items()}
    etfs = sorted({t for ts in SECTOR_ETFS.values() for t in ts} | set(MARKET_ETFS))
    try:
        closes = closes_fn(list(dict.fromkeys(symbols + etfs)), today - timedelta(days=HISTORY_DAYS))
        closes = closes[[d < today for d in closes.index.date]]
    except Exception as e:
        logger.warning("risk report without prices: %s", e)
        closes = pd.DataFrame()
    linked = links(closes, symbols) if not closes.empty else {}

    market = mood_fn(today)
    market_off = market.get("mood") == "risk_off"
    groups = sorted({g for g in sectors.values()} | {g for m in linked.values() for g, c in m.items() if c >= LINK})
    asked = [g for g in groups if g != MARKET]
    with ThreadPoolExecutor(MOOD_WORKERS) as pool:  # a few seconds each: a few at a time
        moods = dict(zip(asked, pool.map(lambda g: sector_mood_fn(g, today), asked)))
    moods[MARKET] = market
    due_today = [label for d, label in releases(today, today)]
    due_tomorrow = [label for d, label in releases(today + timedelta(days=1), today + timedelta(days=1))]
    coming = releases(today + timedelta(days=1), today + timedelta(days=7))
    general = (["the market's news reads risk-off"] if market_off else []) + ([f"{' + '.join(due_today)} today"] if due_today else [])

    def group_level(g: str | None) -> tuple[str, list[str]]:
        """A sector's level (None: a symbol Yahoo can't place). The index funds' group is the market itself."""
        if g == MARKET and market_off:
            return "HIGH", ["the market's news reads risk-off"]
        if g and g != MARKET and moods.get(g, {}).get("mood") == "risk_off":
            return "HIGH", [f"{g} news reads risk-off"]
        return ("ELEVATED" if general else "NORMAL"), list(general)

    group_rows = {g: group_level(g) for g in groups}
    sym_rows = {}
    for s in symbols:
        own = sectors.get(s)
        level, why = group_rows[own] if own in group_rows else group_level(None)
        through = [(c, g) for g, c in linked.get(s, {}).items()
                   if g != own and c >= LINK and group_rows.get(g, ("NORMAL", []))[0] == "HIGH"]
        if through and level != "HIGH":
            c, g = max(through)
            level, why = "HIGH", [f"moves with {g} ({c:.2f}), whose news reads risk-off"] + why
        sym_rows[s] = (level, why, own)

    # What the bots hold at each level
    bot_by_id = {b.id: b for b in bots}
    exposure = {"live": defaultdict(float), "paper": defaultdict(float)}
    for bot_id, items in held.items():
        kind = "live" if bot_by_id[bot_id].broker == "alpaca-live" else "paper"
        for s, value in items.items():
            exposure[kind][sym_rows.get(s, ("NORMAL",))[0]] += value
    live = [b for b in bots if b.broker == "alpaca-live"]
    live_high = any(sym_rows.get(s, ("NORMAL",))[0] == "HIGH" for b in live for s in held.get(b.id, {}))
    real = [g for g in groups if g != MARKET]
    n_high = sum(1 for g in real if group_rows[g][0] == "HIGH")
    overall = ("HIGH" if market_off or live_high or n_high * 2 >= max(1, len(real))
               else "ELEVATED" if n_high or due_today else "NORMAL")

    def etf_moves(tickers) -> list[tuple[str, tuple[float, float]]]:
        return [(t, mv) for t in tickers if (mv := moves(closes, t)) is not None]

    def moves_text(tickers) -> str:
        return "; ".join(f"{t} {_pct(mv[0])} yesterday, {_pct(mv[1])} in 5 days" for t, mv in etf_moves(tickers))

    # --- The email's text: the market, the calendar, your money, then each sector ---
    local = now.astimezone(NY)
    seen = min(local.time(), NEWS_CUTOFF) if local.date() == today else NEWS_CUTOFF  # the news so far, up to the cutoff
    lines = [f"Before the open, {_day(today)} {today.year} (news up to {seen:%H:%M} New York). Information only: "
             "the bots trade as usual.", "", f"OVERALL: {overall}"]
    lines.append(f"Market news: {(market.get('mood') or 'unavailable').upper().replace('_', '-')}"
                 + (f" — {market['reason']}" if market.get("reason") else "")
                 + (f" ({moves_text(MARKET_ETFS)})" if moves_text(MARKET_ETFS) else ""))
    lines.append("Macro: " + (f"{' + '.join(due_today)} TODAY" if due_today else "nothing due today")
                 + (f" · {' + '.join(due_tomorrow)} tomorrow" if due_tomorrow else "")
                 + (" · this week: " + ", ".join(f"{_day(d)} {label}" for d, label in coming) if coming
                    else "".join(f" · next: {_day(d)} {label}" for d, label in releases(today + timedelta(days=8),
                                                                                     today + timedelta(days=60))[:1])))
    if calendar()["known_through"] < (today + timedelta(days=30)).isoformat():
        lines.append(f"(The macro calendar ends {calendar()['known_through']}: add the next dates to macro_dates.json.)")

    live_out = live_slots(session, live, held, sym_rows)
    lines += ["", "REAL MONEY (live bots)"]
    for b, row in zip(live, live_out):
        items = held.get(b.id, {})
        if items:
            parts = [f"{s} {_money(v)} {sym_rows.get(s, ('NORMAL',))[0]}" for s, v in sorted(items.items())]
            lines.append(f"#{b.id} {b.name}: " + ", ".join(parts) + f" · cash ${b.cash:,.2f}")
        else:
            lines.append(f"#{b.id} {b.name}: holding cash only (${b.cash:,.2f})")
        for h in row["held"]:
            if h["level"] != "NORMAL" and h["why"]:
                gap = f"; {_pct(h['stop_gap'])} to its stop" if h["stop_gap"] is not None else ""
                lines.append(f"          {h['symbol']}: {'; '.join(h['why'])}{gap}")
        if row["free_slots"]:
            by_level = defaultdict(list)
            for w in row["watch"]:
                if not w["blacklisted"]:
                    by_level[w["level"]].append(w["symbol"])
            counts = [f"{len(by_level[lv])} {lv}" + (f" ({_more(sorted(by_level[lv]), 6)})" if lv == "HIGH" else "")
                      for lv in ("HIGH", "ELEVATED", "NORMAL") if by_level[lv]]
            lines.append(f"          {row['free_slots']} free slot{'s' if row['free_slots'] > 1 else ''}: of what it can "
                         f"buy today, {', '.join(counts) or 'nothing (all blacklisted)'}")
    if live:
        ex = exposure["live"]
        lines.append(f"Held by live bots: {_money(ex['HIGH'])} HIGH, {_money(ex['ELEVATED'])} elevated, "
                     f"{_money(ex['NORMAL'])} normal")
    else:
        lines.append("No real-money bots.")
    ex = exposure["paper"]
    lines += ["", f"PAPER bots: {_money(ex['HIGH'])} HIGH, {_money(ex['ELEVATED'])} elevated, {_money(ex['NORMAL'])} "
              f"normal ({sum(1 for b in bots if b.broker != 'alpaca-live' and held.get(b.id))} bots holding something)"]

    def worst_move(g: str) -> float:
        found = [mv[1] for _, mv in etf_moves(SECTOR_ETFS.get(g, MARKET_ETFS if g == MARKET else ()))]
        return min(found) if found else 0.0

    lines += ["", "SECTORS your bots watch or hold: riskiest first, then the worst 5 days first"]
    for g in sorted(groups, key=lambda g: (-RANK[group_rows[g][0]], worst_move(g), g)):
        level, why = group_rows[g]
        m = moods.get(g, {})
        mv = moves_text(SECTOR_ETFS.get(g, MARKET_ETFS if g == MARKET else ()))
        lines.append(f"{level:<9} {g} — news {(m.get('mood') or 'unavailable').replace('_', '-')}" + (f" · {mv}" if mv else ""))
        if m.get("reason") and g != MARKET:
            lines.append(f"          {m['reason']}")
        mine = sorted(s for s, row in sym_rows.items() if row[2] == g)
        if mine:
            lines.append(f"          Its symbols ({len(mine)}): {_more(mine, 12)}")
        followers = sorted(((c, s) for s, m2 in linked.items() for g2, c in m2.items()
                            if g2 == g and sectors.get(s) != g and c >= LINK), reverse=True)
        if followers:
            lines.append("          Moving with it (correlation 0.6+): "
                         + _more([f"{s} {c:.2f} ({sectors.get(s, '?')})" for c, s in followers], 8))
    lines += ["", "What the levels mean"] + [f"{k}: {v}." for k, v in LEVELS.items()]
    lines.append("Tested as rules on 230 dip setups (2022-2026), skipping buys on these signals cost more than it "
                 "saved: this report is for your information, the bots don't follow it.")

    subject = (f"Risk report {today:%b} {today.day}: {overall} · market {(market.get('mood') or 'unavailable').replace('_', '-')}"
               f" · {n_high} of {len(real)} sectors high" + (f" · {' + '.join(due_today)} today" if due_today else ""))
    sector_info = []
    for g in sorted(groups, key=lambda g: (-RANK[group_rows[g][0]], worst_move(g), g)):
        tickers = SECTOR_ETFS.get(g, MARKET_ETFS if g == MARKET else ())
        followers = sorted(((c, s) for s, m2 in linked.items() for g2, c in m2.items()
                            if g2 == g and sectors.get(s) != g and c >= LINK), reverse=True)
        sector_info.append({"sector": g, "level": group_rows[g][0], "why": group_rows[g][1],
                            "news": moods.get(g, {}).get("mood"),
                            "reason": None if g == MARKET else moods.get(g, {}).get("reason"),
                            "etf": [{"ticker": tk, "yesterday": round(mv[0], 4), "five_days": round(mv[1], 4)}
                                    for tk, mv in etf_moves(tickers)],
                            "moves_with_it": [f"{s} {c:.2f}" for c, s in followers[:8]]})
    return {"subject": subject, "text": "\n".join(lines), "overall": overall, "market": market,
            "news_until": f"{seen:%H:%M}", "macro": {"today": due_today, "tomorrow": due_tomorrow,
                                                     "coming": [f"{d.isoformat()} {label}" for d, label in coming]},
            "sector_info": sector_info, "live": live_out,
            "exposure": {kind: {lv: round(ex[lv], 2) for lv in RANK} for kind, ex in exposure.items()},
            "sectors": {g: {"level": lv, "why": why, "mood": moods.get(g, {}).get("mood")} for g, (lv, why) in group_rows.items()},
            "symbols": {s: {"level": lv, "why": why, "sector": g} for s, (lv, why, g) in sym_rows.items()}}


# ---------------------------------------------------------------------------
# Sending it
# ---------------------------------------------------------------------------

def wanted(session: Session) -> bool:
    cfg = notify.load(session)
    return notify.smtp_ready() and cfg["enabled"] and bool(cfg["recipients"]) and "report" in cfg["categories"]


def sent_today(session: Session, today: date) -> bool:
    start = datetime.combine(today, time(0, 0), tzinfo=NY)
    return session.scalar(select(Notification.id).where(Notification.category == "report",
                                                        Notification.created_at >= start).limit(1)) is not None


def queue(session: Session, now: datetime, **sources) -> Notification:
    """Build today's report and queue it as an email (notify sends it on its own, not merged with other alerts)."""
    report = build(session, ny_date(now), now, **sources)
    n = Notification(created_at=now, category="report", kind="risk-report", subject=report["subject"][:200],
                     body=report["text"])
    session.add(n)
    session.commit()
    return n


def due(session: Session, now: datetime) -> bool:
    """Is it time for today's report: a trading day, between REPORT_AT and LATE_UNTIL New York, wanted, not sent yet?"""
    today, local = ny_date(now), now.astimezone(NY).time()
    return (session_bounds(today) is not None and REPORT_AT <= local < LATE_UNTIL and wanted(session)
            and not sent_today(session, today))


def run_once(now: datetime | None = None, **sources) -> bool:
    now = now or utcnow()
    with SessionLocal() as session:
        if not due(session, now):
            return False
        queue(session, now, **sources)
        return True


async def run_forever() -> None:
    """Started by main.py next to the scheduler and the email sender; cancelled on shutdown."""
    while True:
        try:
            await asyncio.to_thread(run_once)
        except Exception:
            logger.exception("the daily risk report failed")
        await asyncio.sleep(CHECK_EVERY_S)
