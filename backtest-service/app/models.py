from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

# decision-service's strategies (GET /strategies there describes each one). Keep in sync with
# decision-service/app/strategies.py STRATEGIES; validating here fails fast instead of a whole run of HOLDs.
Strategy = Literal["sma_rsi", "trend_following", "momentum", "breakout", "mean_reversion", "range_trading",
                   "ma_pullback", "reversal", "gap_and_go", "news_catalyst", "fibonacci"]


class RunConfig(BaseModel):
    """Everything the user configures from the frontend before starting a backtest."""

    symbol: str = "AAPL"
    start: date  # first day of the backtest window
    end: date  # last day of the backtest window
    initial_cash: float = 10_000.0
    min_confidence: float = Field(0.6, ge=0, le=1)  # ignore signals weaker than this
    rebalance_days: int = Field(5, ge=1)  # ask decision-service every N trading days (5 ≈ weekly) to limit LLM calls
    position_pct: float = Field(1.0, gt=0, le=1)  # fraction of cash to deploy on a BUY
    # --- Trading-cost realism (make backtests honest) ---
    fee_pct: float = Field(0.0, ge=0)  # commission per trade as a fraction of value; 0 = commission-free stocks, ~0.0015 for crypto
    slippage_pct: float = Field(0.0005, ge=0)  # fill worse than the close by this fraction (0.0005 = 0.05%); buys fill higher, sells lower
    # --- Risk levels for each entry (None = decision-service's STOP_PCT / TARGET_PCT defaults) ---
    stop_pct: float | None = Field(None, gt=0, le=0.5)  # stop-loss this far below the entry (0.04 = 4%)
    target_pct: float | None = Field(None, gt=0, le=2)  # take profit this far above the entry
    strategy: Strategy = "sma_rsi"  # which decision-service strategy decides (sma_rsi = the original simple one)
    # When a decision day decides, like a trading bot's "Check at": before the close (on the day's close),
    # after the open (10:00 New York, replayed from 30-minute bars), or both
    decide_at: Literal["close", "open", "both"] = "close"
    # Like a trading bot's drawdown breaker: once equity falls this far below its peak, stop making decisions
    # (the stop-loss and target still guard an open position). 0 = off. Same default as the bots.
    max_drawdown_pct: float = Field(0.2, ge=0, lt=1)


class RunSummary(BaseModel):
    """Compact view of a run for the list endpoint."""

    run_id: str
    symbol: str
    status: str  # pending | running | done | error
    created_at: str
