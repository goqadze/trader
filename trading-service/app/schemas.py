"""Request/response shapes of the trading API. Validation lives here, so a bad value (a 150% stop-loss,
a negative position size) is rejected with a clear 422 before it can reach a bot."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["rules", "llm"]


class StrategyParams(BaseModel):
    """The tunable knobs. Same names and meaning as the backtest's RunConfig."""

    mode: Mode = "rules"
    min_confidence: float = Field(0.6, ge=0, le=1)
    rebalance_days: int = Field(5, ge=1, le=60)
    position_pct: float = Field(1.0, gt=0, le=1)
    stop_pct: float = Field(0.04, gt=0, le=0.5)
    target_pct: float = Field(0.08, gt=0, le=2)
    fee_pct: float = Field(0.0, ge=0, le=0.05)
    slippage_pct: float = Field(0.0005, ge=0, le=0.05)
    max_drawdown_pct: float = Field(0.2, ge=0, le=1)  # 0 turns the breaker off


class BotCreate(StrategyParams):
    name: str | None = Field(None, max_length=80)
    symbol: str = Field(..., pattern=r"^[A-Za-z][A-Za-z.\-]{0,9}$")
    broker: str = "paper"
    allocated_cash: float = Field(10_000, ge=100, le=10_000_000)
    # A real-money bot must be confirmed explicitly by the caller, not just by choosing it in a dropdown
    confirm_live: bool = False


class BotUpdate(BaseModel):
    """Everything is optional: only the fields sent are changed. Symbol, broker and capital are fixed
    for a bot's life (create a new bot instead) so its history always describes one consistent setup."""

    name: str | None = Field(None, max_length=80)
    mode: Mode | None = None
    min_confidence: float | None = Field(None, ge=0, le=1)
    rebalance_days: int | None = Field(None, ge=1, le=60)
    position_pct: float | None = Field(None, gt=0, le=1)
    stop_pct: float | None = Field(None, gt=0, le=0.5)
    target_pct: float | None = Field(None, gt=0, le=2)
    fee_pct: float | None = Field(None, ge=0, le=0.05)
    slippage_pct: float | None = Field(None, ge=0, le=0.05)
    max_drawdown_pct: float | None = Field(None, ge=0, le=1)


class BotOut(StrategyParams):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    symbol: str
    broker: str
    live: bool
    status: str
    allocated_cash: float
    cash: float
    shares: int
    entry_price: float | None
    cost_basis: float
    stop_price: float | None
    target_price: float | None
    realized_pnl: float
    peak_equity: float
    benchmark_price: float | None
    last_price: float | None
    last_price_at: datetime | None
    last_decision_date: date | None
    created_at: datetime
    # --- derived for the dashboard ---
    equity: float
    return_pct: float
    unrealized_pnl: float
    buy_hold_return_pct: float | None
    pending_order: bool  # a market order is still working at the broker
    stop_at_broker: bool  # the stop-loss rests at the broker as a real order (Alpaca), not only in this service


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    session_date: date
    kind: str
    action: str
    confidence: float
    sentiment: str
    reasoning: str
    steps: list
    price: float | None
    outcome: str


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    decision_id: int | None
    created_at: datetime
    updated_at: datetime
    side: str
    order_type: str  # market | stop
    stop_price: float | None
    qty: int
    reason: str
    status: str
    client_order_id: str
    broker_order_id: str | None
    filled_qty: int
    avg_price: float | None
    fee: float
    pnl: float | None
    error: str | None


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day: date
    equity: float
    cash: float
    shares: int
    price: float


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_id: int | None
    created_at: datetime
    level: str
    kind: str
    message: str


class BrokerInfo(BaseModel):
    name: str
    label: str
    live: bool
    available: bool
    reason: str


class StatusOut(BaseModel):
    now: datetime
    market_open: bool
    session_open: datetime | None  # today's open/close (UTC), None on a non-trading day
    session_close: datetime | None
    next_decision_at: datetime
    decision_minutes_before_close: int
    risk_check_minutes: int
    scheduler_enabled: bool
    scheduler_running: bool
    scheduler_last_tick: datetime | None
    live_trading_allowed: bool
    brokers: list[BrokerInfo]
