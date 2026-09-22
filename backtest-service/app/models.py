from datetime import date

from pydantic import BaseModel, Field


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
    engine: str = "simple"  # "simple" (built-in simulator) or "nautilus"
    mode: str = "rules"  # how decision-service decides: "rules" or "llm"


class RunSummary(BaseModel):
    """Compact view of a run for the list endpoint."""

    run_id: str
    symbol: str
    status: str  # pending | running | done | error
    created_at: str
