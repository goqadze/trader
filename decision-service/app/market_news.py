"""The market's mood before the open: is today a risk-off day for US stocks as a whole, or for one sector?

On days the whole market is shaken (a hot inflation report, a hawkish Fed, a crash abroad) falls tend to go on, and a
stock's own headlines don't say so: on 2026-10-13 the chip stocks' own news was analysts' praise, and the CPI the next
morning sank them all. So once a day an LLM reads the market-wide headlines up to 9:25 New York (Alpaca's news tagged
SPY, QQQ, DIA or IWM since the previous weekday's open) and answers risk_off, neutral or risk_on.

The same for one sector (`sector_mood`): the headlines tagged with the sector's ETFs and biggest companies (SECTOR_TAGS,
a fixed list, so every reader sees the same news). trading-service's daily risk report (risk_report.py) emails both
before the open. Information only: as buy filters on 230 dip setups they cost more than they saved (Oct 2026).

Each day is judged once and saved in news-db (news_judgments, under the symbol MARKET or "SECTOR <name>"), so every
reader of that day sees the same answer. A caveat for backtests: the LLM may remember how a past day turned out."""

import logging
import os
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel

from . import agent, news
from .news import MARKET_TZ

logger = logging.getLogger("decision-service")

MARKET_SYMBOLS = "SPY,QQQ,DIA,IWM"  # Benzinga tags market-wide stories with the index funds
CUTOFF = time(9, 25)  # the CPI and jobs reports come out at 8:30: both are in
MAX_HEADLINES = 60  # the newest ones; a busy morning has more
JUDGMENT_SYMBOL = "MARKET"
# Bump after changing the headlines, the prompt or LLM_MODEL: a saved day is then judged again
MARKET_MOOD_VERSION = 1
# 2: market-wide news no longer counts for a sector unless it hits that sector harder (the market has its own verdict)
SECTOR_MOOD_VERSION = 2
MOODS = ("risk_off", "neutral", "risk_on")

# Yahoo's sectors (and the categories it gives the ETFs on our watchlists) -> the tickers whose news speaks for the sector:
# its ETFs first, then its biggest companies (an ETF alone is tagged on too few stories). A sector not here: "unavailable"
SECTOR_TAGS = {
    "Technology": "XLK,SMH,SOXX,IGV,NVDA,AVGO,AMD,MU,MSFT,ORCL",
    "Communication Services": "XLC,GOOGL,META,NFLX,DIS,T",
    "Consumer Cyclical": "XLY,AMZN,TSLA,HD,MCD,NKE",
    "Consumer Defensive": "XLP,WMT,COST,PG,KO,PEP",
    "Energy": "XLE,XOP,OIH,XOM,CVX,COP,SLB",
    "Financial Services": "XLF,KRE,JPM,BAC,GS,MS",
    "Healthcare": "XLV,XBI,LLY,UNH,JNJ,MRK,ABBV",
    "Industrials": "XLI,CAT,GE,HON,BA,ETN,UNP",
    "Utilities": "XLU,NEE,SO,DUK,VST,CEG",
    "Basic Materials": "XLB,LIN,FCX,NEM,NUE,ALB",
    "Real Estate": "XLRE,PLD,AMT,EQIX,O",
    # ETF categories
    "Commodities Focused": "USO,GLD,SLV,DBC,UNG",
    "Commodities Broad Basket": "DBC,GSG,DBA,USO,GLD",
    "Equity Energy": "XLE,XOP,OIH,XOM,CVX",
    "Equity Precious Metals": "GDX,GDXJ,GLD,NEM",
    "Natural Resources": "XME,COPX,URA,FCX,CCJ",
    "Long Government": "TLT,IEF,GOVT,SHY",
}
# Yahoo names some ETFs' categories differently from the stocks' sectors (XLF "Financial", XLV "Health" ...), and the
# index funds' categories are the whole market
SECTOR_TAGS |= {"Communications": SECTOR_TAGS["Communication Services"], "Financial": SECTOR_TAGS["Financial Services"],
                "Health": SECTOR_TAGS["Healthcare"],
                **{c: MARKET_SYMBOLS for c in ("Large Blend", "Large Growth", "Large Value", "Small Blend")}}

SECTOR_PROMPT = """You help a stock bot that buys dips decide whether to buy any {sector} stock today. Below are news \
headlines about the sector's ETFs and biggest companies published before today's US open, newest first, each with \
its age. Classify today for US {sector} stocks as a group, compared with the market as a whole (the whole market's \
mood is judged separately):
- risk_off: a shock under way or due today that is likely to keep pushing most of this sector down: sector-wide bad \
news (falling demand, regulation or export bans, a big company's warning, a sell-off in the sector abroad, prices that \
hit the whole sector), or market-wide news that hits this sector harder than the rest (rates for real estate and \
utilities, oil for energy, AI spending for chips). Market-wide news that hits all stocks alike (an inflation report, \
the Fed, the whole market falling) is not enough on its own.
- risk_on: a broad relief or rally catalyst for the sector.
- neutral: anything else, including one company's own news and normal day-to-day moves.
Judge only from these headlines, not from anything you may know about later events. Give the label and one sentence \
why.
Headlines:
- """

PROMPT = """You help a stock bot that buys dips decide whether to buy any dip today. Below are market-wide news \
headlines published before today's US open, newest first, each with its age. Classify today for US stocks as a whole:
- risk_off: a market-wide shock is under way or due today that is likely to keep pushing most stocks down: an \
inflation or jobs report far from expectations, a hawkish central-bank surprise, a sharp sell-off in futures or in \
Asian or European markets, a financial or geopolitical crisis.
- risk_on: a broad relief or rally catalyst.
- neutral: anything else, including ordinary company news and normal day-to-day moves.
Judge only from these headlines, not from anything you may know about later events. Give the label and one sentence \
why.
Headlines:
- """


class MarketMood(BaseModel):
    """Structured output the LLM must return."""

    label: str  # risk_off | neutral | risk_on
    rationale: str


def cutoff(as_of: date) -> datetime:
    return datetime.combine(as_of, CUTOFF, tzinfo=MARKET_TZ)


def since(as_of: date) -> datetime:
    """The previous weekday's open: Friday's for a Monday."""
    back = 3 if as_of.weekday() == 0 else 1
    return datetime.combine(as_of - timedelta(days=back), time(9, 30), tzinfo=MARKET_TZ)


def headlines(as_of: date, tags: str = MARKET_SYMBOLS) -> list[str]:
    """The headlines tagged with `tags` between the previous weekday's open and 9:25 on as_of, newest first, each once."""
    start, end = since(as_of), cutoff(as_of)
    items = news._alpaca(tags, start.date(), end.date())
    items = sorted((i for i in items if start.timestamp() <= i["ts"] < end.timestamp()), key=lambda i: -i["ts"])
    out, seen = [], set()
    for i in items:
        text = " ".join(i["headline"].split())
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(f"[{news._age_label((end.timestamp() - i['ts']) / 3600)}] {text}")
        if len(out) >= MAX_HEADLINES:
            break
    return out


def _enabled() -> bool:
    return all(os.getenv(k) for k in ("OPENAI_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"))


def _judge(as_of: date, tags: str = MARKET_SYMBOLS, prompt: str = PROMPT) -> dict:
    """{"mood", "reason", "headlines", "complete"}; complete is False when the news or the LLM failed."""
    try:
        heads = headlines(as_of, tags)
    except Exception as e:
        return {"mood": "unavailable", "reason": f"market news failed: {news._safe_error(e)}", "headlines": [], "complete": False}
    if not heads:
        return {"mood": "neutral", "reason": "no headlines before the open", "headlines": [], "complete": True}
    llm = agent._chat(temperature=0, max_retries=agent.SENTIMENT_RETRIES).with_structured_output(MarketMood)
    try:
        out = llm.invoke(prompt + "\n- ".join(heads), config=agent.trace_config())
    except Exception as e:
        logger.warning("market mood failed for %s: %s", as_of, type(e).__name__)
        return {"mood": "unavailable", "reason": f"the LLM failed ({type(e).__name__})", "headlines": heads, "complete": False}
    label = out.label.strip().lower().replace("-", "_").replace(" ", "_")
    return {"mood": label if label in MOODS else "neutral", "reason": out.rationale, "headlines": heads, "complete": True}


_memory: dict[tuple[str, date], dict] = {}


def market_mood(as_of: date, now: datetime | None = None) -> dict:
    """The day's mood: {"mood", "reason", "headlines"}. Judged once the 9:25 cutoff has passed and kept (in memory and in
    news-db); asked before it, the answer covers the headlines so far and isn't kept."""
    return _mood(JUDGMENT_SYMBOL, MARKET_MOOD_VERSION, as_of, now, MARKET_SYMBOLS, PROMPT)


def sector_mood(sector: str, as_of: date, now: datetime | None = None) -> dict:
    """market_mood for one sector (SECTOR_TAGS); "unavailable" for a sector it has no news tickers for."""
    if sector not in SECTOR_TAGS:
        return {"mood": "unavailable", "reason": f"no news tickers for the sector {sector!r}", "headlines": []}
    return _mood(f"SECTOR {sector}", SECTOR_MOOD_VERSION, as_of, now, SECTOR_TAGS[sector], SECTOR_PROMPT.format(sector=sector))


def _mood(key: str, version: int, as_of: date, now: datetime | None, tags: str, prompt: str) -> dict:
    if not _enabled():
        return {"mood": "unavailable", "reason": "needs OPENAI_API_KEY and the Alpaca keys", "headlines": []}
    at = cutoff(as_of)
    final = (now or datetime.now(MARKET_TZ)) >= at
    if final and (key, as_of) in _memory:
        return _memory[(key, as_of)]
    saved = None
    if final:
        try:
            saved = agent._judgments().get_judgment(key, at, version)
        except Exception as e:  # the database being down must not stop the answer
            logger.warning("%s mood lookup failed for %s: %s", key, as_of, type(e).__name__)
    if saved is None:
        out = _judge(as_of, tags, prompt)
        complete = out.pop("complete")
        if final and complete:
            try:
                agent._judgments().put_judgment(key, at, version, out)
            except Exception as e:
                logger.warning("%s mood save failed for %s: %s", key, as_of, type(e).__name__)
        if not (final and complete):
            return out
        saved = out
    _memory[(key, as_of)] = saved
    return saved
