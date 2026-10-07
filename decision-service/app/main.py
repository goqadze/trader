import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from .agent import agent, news_rag  # the compiled LangGraph workflow, and its news step on its own
from .futures import FUTURES
from .intraday import bars_between
from .intraday_strategies import HTF_HISTORY_DAYS, INTRADAY_STRATEGIES, plans as intraday_plans
from .strategies import DEFAULT_STRATEGY, STRATEGIES, catalog

logger = logging.getLogger("decision-service")
logging.basicConfig(level=logging.INFO)

# --- Optional error monitoring (Sentry). Activates only if SENTRY_DSN is set. ---
# LangSmith LLM tracing is separate and needs no code here: set LANGCHAIN_TRACING_V2=true
# and LANGCHAIN_API_KEY in .env and LangChain traces every LLM/agent call automatically.
_dsn = os.getenv("SENTRY_DSN")
if _dsn:
    import sentry_sdk

    sentry_sdk.init(dsn=_dsn, traces_sample_rate=0.1)
    logger.info("Sentry error monitoring enabled")

app = FastAPI(title="Decision Support Service")


class Signal(BaseModel):
    """Shape of the JSON response returned by /signal."""

    symbol: str
    as_of: date  # the date the analysis was done "as of"
    strategy: str  # which strategy decided (see GET /strategies)
    action: str  # BUY | SELL | HOLD
    confidence: float  # 0..1
    sentiment: str = "unavailable"  # bullish | bearish | neutral | unavailable | off (from the news layer)
    reasoning: str  # human-readable explanation
    position: dict | None = None  # share count + stop/target for a BUY; None for SELL/HOLD
    steps: list[str]  # step-by-step trail of what the agent did ("show your work")


@app.get("/health")
def health():
    """Simple liveness check, handy for Docker and monitoring."""
    return {"status": "ok"}


@app.get("/strategies")
def strategies():
    """Every decision strategy: its rules in words, what it suits, and suggested starting parameters."""
    return catalog()


@app.get("/bars/intraday")
def intraday_bars(symbol: str, start: date, end: date, timeframe: Literal["30Min", "15Min", "5Min"] = "30Min"):
    """Regular-hours bars (New York time, each stamped with its START). 30-minute ones for backtests that decide
    after the open (each day's 10:00 price and the range around it), 5-minute ones for the intraday strategies,
    15-minute (and 5- and 30-minute) ones for the dip buyer, which checks its symbols every few minutes.
    From Alpaca when its keys are set (years of history), else Yahoo (the last 60 days). The data keys stay here.
    Index futures (NQ, MNQ, ES, MES, YM, MYM) come rebuilt from their ETFs (futures.py)."""
    if end < start:
        raise HTTPException(422, "end must not be before start")
    try:
        df = bars_between(symbol.upper(), start, end, timeframe)
    except Exception as e:
        logger.exception("intraday bars failed for %s %s..%s", symbol, start, end)
        raise HTTPException(502, f"{timeframe} prices unavailable: {type(e).__name__}: {e}")
    return [{"t": ts.isoformat(), "open": float(r.Open), "high": float(r.High), "low": float(r.Low),
             "close": float(r.Close), "volume": float(r.Volume)} for ts, r in df.iterrows()]


@app.get("/intraday/plans")
def intraday_plan_list(symbol: str, start: date, end: date, strategy: str, sides: Literal["long", "both"] = "both",
                       entry: Literal["limit", "market"] = "limit", htf: Literal["off", "1h", "4h", "1d"] = "off"):
    """Each day's order plan from an intraday strategy (orb, ict_sweep_fvg, ict_amd) on 5-minute bars: side, entry
    (a limit, or the next bar's open), stop, target, when it was placed and until when it may fill. Days without a
    setup are left out. Built from the bars that had finished when each setup completed, so backtest-service can
    replay them without look-ahead. A future's prices come on its tick. `entry`: the ICT strategies' order, a limit
    at the gap's middle or in at market right after the setup (same setup, stop and target). `htf`: their
    higher-timeframe trend filter (only trade with the 1-hour, 4-hour or daily trend)."""
    if strategy not in INTRADAY_STRATEGIES:
        raise HTTPException(422, f"unknown intraday strategy '{strategy}'; one of {list(INTRADAY_STRATEGIES)}")
    if end < start:
        raise HTTPException(422, "end must not be before start")
    try:
        # A week before the start, so the first day knows the previous session's high and low; with the pre-market
        # (the Power of 3's accumulation range)
        history = HTF_HISTORY_DAYS if htf != "off" else 10  # the trend filter needs 20 daily bars before the start
        bars = bars_between(symbol.upper(), start - timedelta(days=history), end, "5Min", extended=True)
    except Exception as e:
        logger.exception("5-minute bars failed for %s %s..%s", symbol, start, end)
        raise HTTPException(502, f"5-minute prices unavailable: {type(e).__name__}: {e}")
    future = FUTURES.get(symbol.upper())
    return intraday_plans(bars, strategy, start, end, sides, tick=future.tick if future else 0.01, entry=entry, htf=htf)


class NewsSentiment(BaseModel):
    """Shape of /news/sentiment: the news step alone, no strategy."""

    symbol: str
    as_of: date
    sentiment: str  # bullish | bearish | neutral | unavailable (no keys, or a source / the LLM failed)
    headlines: list[str]
    catalysts: list[str]  # the fresh (<48h) company-specific events among them
    steps: list[str]


@app.get("/news/sentiment", response_model=NewsSentiment)
def news_sentiment(symbol: str, as_of: date | None = None, decided_at: datetime | None = None):
    """The news step of /signal on its own: the recent headlines about a symbol and the LLM's verdict on them. The dip
    buyer asks this before it buys a drop: bearish news says the drop has a reason and may go on. Same rules as
    /signal: only news published before decided_at (default 15:30 New York on as_of) counts, and a day that's over is
    judged once and reused (news_judgments), so a backtest and a rerun see the same verdict."""
    as_of = as_of or datetime.now(ZoneInfo("America/New_York")).date()
    state = {"symbol": symbol.upper(), "as_of": as_of, "steps": [], "use_news": True}
    if decided_at is not None:
        decided_at = decided_at if decided_at.tzinfo else decided_at.replace(tzinfo=timezone.utc)
        if decided_at.astimezone(ZoneInfo("America/New_York")).date() != as_of:
            raise HTTPException(422, "decided_at must be on the as_of date (New York time)")
        state["decided_at"] = decided_at
    try:
        out = news_rag(state)
    except Exception as e:  # news_rag already turns source failures into "unavailable"; this is anything else
        logger.exception("news sentiment failed for %s as_of=%s", symbol, as_of)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    return NewsSentiment(symbol=symbol.upper(), as_of=as_of, sentiment=out.get("sentiment", "unavailable"),
                         headlines=out.get("headlines", []), catalysts=out.get("catalysts", []), steps=out.get("steps", []))


@app.post("/signal", response_model=Signal)
def signal(
    symbol: str,
    as_of: date | None = None,
    strategy: str = DEFAULT_STRATEGY,
    account_balance: float = 500.0,
    stop_pct: float | None = Query(None, gt=0, le=0.5),
    target_pct: float | None = Query(None, gt=0, le=2),
    decided_at: datetime | None = None,
    explain: bool = True,
    news: bool = True,
):
    """as_of lets the backtester replay history without look-ahead.
    strategy = which decision strategy runs (GET /strategies lists them; default: the simple SMA/RSI one).
    account_balance sizes a BUY (risk a fixed % of it).
    stop_pct / target_pct override the STOP_PCT / TARGET_PCT env defaults, so each trading bot or
    backtest can run its own risk levels.
    decided_at (live bots) = the exact decision moment, e.g. 10:00 New York: news published after it is
    ignored. Without it the news cutoff is 15:30 New York on as_of. A past decided_at (a backtest replaying
    10:00) sees that day as it stood then, rebuilt from 30-minute bars.
    explain=false skips the LLM-written explanation (the reasoning is the rule text instead): backtests make
    thousands of decisions, and the explanation doesn't change any of them.
    news=false skips the news step (technical only): no news fetch, no sentiment LLM, no confidence nudge. Fast
    and free, for screening many symbols and strategies; News catalyst has nothing to trade on then."""
    as_of = as_of or date.today()  # default to today for live use
    if strategy not in STRATEGIES:
        raise HTTPException(422, f"unknown strategy '{strategy}'; one of {list(STRATEGIES)}")
    state = {"symbol": symbol.upper(), "as_of": as_of, "strategy": strategy, "account_balance": account_balance,
             "llm_explanation": explain, "use_news": news}
    if decided_at is not None:
        decided_at = decided_at if decided_at.tzinfo else decided_at.replace(tzinfo=timezone.utc)
        # Must fall on as_of's New York date, or a backtest could let tomorrow's news into today's decision
        if decided_at.astimezone(ZoneInfo("America/New_York")).date() != as_of:
            raise HTTPException(422, "decided_at must be on the as_of date (New York time)")
        state["decided_at"] = decided_at
    if stop_pct is not None:
        state["stop_pct"] = stop_pct
    if target_pct is not None:
        state["target_pct"] = target_pct
    try:
        # Run the whole graph: fetch_data -> news_rag -> decide -> size_position -> explain
        out = agent.invoke(state)
    except ValueError as e:
        # e.g. unknown ticker or not enough price history -> 422 instead of a server crash
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        # Anything else: log the full traceback and return the real error text (not an opaque 500)
        logger.exception("signal failed for %s as_of=%s strategy=%s", symbol, as_of, strategy)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    return Signal(**out)
