"""Request/response shapes of the trading API. Validation lives here, so a bad value (a 150% stop-loss,
a negative position size) is rejected with a clear 422 before it can reach a bot."""

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# decision-service's strategies (its GET /strategies describes each). Keep in sync with
# decision-service/app/strategies.py STRATEGIES.
Strategy = Literal["sma_rsi", "trend_following", "momentum", "breakout", "mean_reversion", "range_trading",
                   "ma_pullback", "reversal", "gap_and_go", "news_catalyst", "fibonacci"]
DecideAt = Literal["close", "open", "both"]  # which of the day's decision slots a bot uses (see market.slot_window)
Rotation = Literal["momentum_rotation"]  # models.ROTATION: a rotation bot's strategy (see rotation.py)
TICKER = r"^[A-Z][A-Z.\-]{0,9}$"


class StrategyParams(BaseModel):
    """The tunable knobs. Same names and meaning as the backtest's RunConfig."""

    strategy: Strategy = "sma_rsi"
    min_confidence: float = Field(0.6, ge=0, le=1)
    rebalance_days: int = Field(5, ge=1, le=60)
    decide_at: DecideAt = "close"  # live bots only: the backtest always decides at the close
    position_pct: float = Field(1.0, gt=0, le=1)
    stop_pct: float = Field(0.04, gt=0, le=0.5)
    target_pct: float = Field(0.08, gt=0, le=2)
    fee_pct: float = Field(0.0, ge=0, le=0.05)
    slippage_pct: float = Field(0.0005, ge=0, le=0.05)
    max_drawdown_pct: float = Field(0.2, ge=0, le=1)  # 0 turns the breaker off


def _no_crypto(symbols: list[str]) -> None:
    """Crypto pairs (BTC-USD, as the backtests write them) can be backtested but not traded by a bot yet: Alpaca trades
    them as BTC/USD, around the clock, in fractions, without stop orders, and the bots assume none of that."""
    coins = [s for s in symbols if s.upper().endswith("-USD")]
    if coins:
        raise ValueError(f"bots can't trade crypto yet: {', '.join(coins)} (backtests can)")


class BotCreate(StrategyParams):
    name: str | None = Field(None, max_length=80)
    symbol: str = Field(..., pattern=r"^[A-Za-z][A-Za-z.\-]{0,9}$")
    broker: str = "paper"
    allocated_cash: float = Field(10_000, ge=100, le=10_000_000)
    # A real-money bot must be confirmed explicitly by the caller, not just by choosing it in a dropdown
    confirm_live: bool = False

    @field_validator("symbol")
    @classmethod
    def _tradable(cls, v: str) -> str:
        _no_crypto([v])
        return v


class RotationBotCreate(BaseModel):
    """A momentum rotation bot: the backtest's RotationConfig (same names) plus the bot's broker and capital."""

    name: str | None = Field(None, max_length=80)
    broker: str = "paper"
    allocated_cash: float = Field(10_000, ge=100, le=10_000_000)
    universe: list[str] = Field(..., min_length=2, max_length=60)
    top_n: int = Field(3, ge=1, le=20)
    lookback_months: int = Field(12, ge=1, le=24)
    skip_months: int = Field(1, ge=0, le=3)
    abs_filter: bool = True
    fee_pct: float = Field(0.0, ge=0, le=0.05)
    slippage_pct: float = Field(0.0005, ge=0, le=0.05)
    max_drawdown_pct: float = Field(0.2, ge=0, le=1)

    @field_validator("universe")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        out = list(dict.fromkeys(s.strip().upper() for s in v if s.strip()))
        bad = [s for s in out if not re.match(TICKER, s)]
        if bad:
            raise ValueError(f"not a ticker: {', '.join(bad)}")
        if len(out) < 2:
            raise ValueError("pick at least 2 symbols")
        _no_crypto(out)
        return out

    @model_validator(mode="after")
    def _top_n_fits(self):
        if self.top_n > len(self.universe):
            raise ValueError(f"top_n ({self.top_n}) is more than the universe ({len(self.universe)} symbols)")
        return self


class BotUpdate(BaseModel):
    """Everything is optional: only the fields sent are changed. Symbol, broker and capital are fixed
    for a bot's life (create a new bot instead) so its history always describes one consistent setup."""

    name: str | None = Field(None, max_length=80)
    strategy: Strategy | None = None
    min_confidence: float | None = Field(None, ge=0, le=1)
    rebalance_days: int | None = Field(None, ge=1, le=60)
    decide_at: DecideAt | None = None
    position_pct: float | None = Field(None, gt=0, le=1)
    stop_pct: float | None = Field(None, gt=0, le=0.5)
    target_pct: float | None = Field(None, gt=0, le=2)
    fee_pct: float | None = Field(None, ge=0, le=0.05)
    slippage_pct: float | None = Field(None, ge=0, le=0.05)
    max_drawdown_pct: float | None = Field(None, ge=0, le=1)


class HoldingOut(BaseModel):
    """One symbol a rotation bot holds, valued at its last price."""

    symbol: str
    shares: int
    cost_basis: float
    last_price: float | None
    last_price_at: datetime | None
    value: float
    weight_pct: float  # of the bot's equity
    unrealized_pnl: float


class BotOut(StrategyParams):
    model_config = ConfigDict(from_attributes=True)

    strategy: Strategy | Rotation
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
    last_decision_at: datetime | None
    created_at: datetime
    # --- derived for the dashboard ---
    next_decision_at: datetime | None  # when the scheduler will next decide for this bot (None = paused/archived)
    equity: float
    return_pct: float
    unrealized_pnl: float
    buy_hold_return_pct: float | None
    pending_order: bool  # a market order is still working at the broker
    stop_at_broker: bool  # the stop-loss rests at the broker as a real order (Alpaca), not only in this service
    # --- rotation bots only (None / empty on the others) ---
    universe: list[str] | None = None
    top_n: int | None = None
    lookback_months: int | None = None
    skip_months: int | None = None
    abs_filter: bool | None = None
    holdings: list[HoldingOut] = []
    rebalancing: bool = False  # a rebalance's orders are still being sent (sells first, then the buys)


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
    symbol: str | None  # None on orders from before it was stored: the bot's symbol
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
    next_decision_at: datetime  # the soonest decision of any active bot (or the next close slot if none)
    decision_minutes_before_close: int
    decision_minutes_after_open: int
    risk_check_minutes: int
    scheduler_enabled: bool
    scheduler_running: bool
    scheduler_last_tick: datetime | None
    live_trading_allowed: bool
    brokers: list[BrokerInfo]


# ---------------------------------------------------------------------------
# Users and sign-in (auth.py)
# ---------------------------------------------------------------------------

class Credentials(BaseModel):
    # Signing in checks no format: a mistyped username is just "wrong username or password" (401)
    username: str = Field(..., min_length=1, max_length=64)
    # 128 max: a sanity cap on what the (deliberately slow) password hash is asked to chew on
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def _normalize(cls, v):
        # Lowercased, so "Alice" and "alice" are the same account
        return v.strip().lower() if isinstance(v, str) else v


class LoginIn(Credentials):
    remember: bool = True  # "Keep me signed in": a cookie that survives closing the browser


class SignupIn(Credentials):
    username: str = Field(..., pattern=r"^[a-z0-9._-]{3,32}$")  # checked after lowercasing
    name: str = Field(..., min_length=1, max_length=80)
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("name", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v


class PasswordChange(BaseModel):
    current_password: str = Field(..., max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    role: str
    status: str
    created_at: datetime
    approved_at: datetime | None
    approved_by: str | None
    last_login_at: datetime | None


class UserUpdate(BaseModel):
    """An admin's decision about someone else's account. Only the fields sent are changed."""

    status: Literal["active", "rejected", "disabled"] | None = None
    role: Literal["admin", "user"] | None = None
