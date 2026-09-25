import logging
import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from .agent import agent  # the compiled LangGraph workflow
from .intraday import bars_between
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
    sentiment: str = "unavailable"  # bullish | bearish | neutral | unavailable (from the news layer)
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
def intraday_bars(symbol: str, start: date, end: date):
    """Regular-hours 30-minute bars (New York time, each stamped with its START) for backtests that decide after
    the open: they need each day's 10:00 price and the range around it. From Alpaca when its keys are set (years
    of history), else Yahoo (the last 60 days). The data keys stay in this service."""
    if end < start:
        raise HTTPException(422, "end must not be before start")
    try:
        df = bars_between(symbol.upper(), start, end)
    except Exception as e:
        logger.exception("intraday bars failed for %s %s..%s", symbol, start, end)
        raise HTTPException(502, f"30-minute prices unavailable: {type(e).__name__}: {e}")
    return [{"t": ts.isoformat(), "open": float(r.Open), "high": float(r.High), "low": float(r.Low),
             "close": float(r.Close), "volume": float(r.Volume)} for ts, r in df.iterrows()]


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
    thousands of decisions, and the explanation doesn't change any of them."""
    as_of = as_of or date.today()  # default to today for live use
    if strategy not in STRATEGIES:
        raise HTTPException(422, f"unknown strategy '{strategy}'; one of {list(STRATEGIES)}")
    state = {"symbol": symbol.upper(), "as_of": as_of, "strategy": strategy, "account_balance": account_balance,
             "llm_explanation": explain}
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
