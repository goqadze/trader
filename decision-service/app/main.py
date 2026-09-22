import logging
import os
from datetime import date

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .agent import agent  # the compiled LangGraph workflow

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


@app.post("/signal", response_model=Signal)
def signal(symbol: str, as_of: date | None = None, mode: str = "rules", account_balance: float = 500.0):
    """as_of lets the backtester replay history without look-ahead.
    mode = 'rules' (SMA/RSI logic) or 'llm' (the model decides).
    account_balance sizes a BUY (risk a fixed % of it)."""
    as_of = as_of or date.today()  # default to today for live use
    if mode not in ("rules", "llm"):
        raise HTTPException(422, "mode must be 'rules' or 'llm'")
    try:
        # Run the whole graph: fetch_data -> news_rag -> decide -> size_position -> explain
        out = agent.invoke({"symbol": symbol.upper(), "as_of": as_of, "mode": mode, "account_balance": account_balance})
    except ValueError as e:
        # e.g. unknown ticker or not enough price history -> 422 instead of a server crash
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        # Anything else: log the full traceback and return the real error text (not an opaque 500)
        logger.exception("signal failed for %s as_of=%s mode=%s", symbol, as_of, mode)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    return Signal(**out)
