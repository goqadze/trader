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
Dip = Literal["dip_buyer"]  # models.DIP: a dip buyer's strategy (see dip.py)
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


def _tickers(v: list[str]) -> list[str]:
    """Upper-cased tickers, each once, in the given order; crypto and anything that isn't a ticker is refused."""
    out = list(dict.fromkeys(s.strip().upper() for s in v if s.strip()))
    bad = [s for s in out if not re.match(TICKER, s)]
    if bad:
        raise ValueError(f"not a ticker: {', '.join(bad)}")
    _no_crypto(out)
    return out


Interval = Literal["5m", "15m", "30m", "1h", "1d"]  # how often a dip bot checks (dip.INTERVAL_MINUTES)
INTERVAL_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 390}
# The longest window per interval in days: Yahoo, where the bot reads its bars, keeps 5- to 30-minute ones for 60 days
MAX_LOOKBACK_DAYS = {"5m": 40, "15m": 40, "30m": 40, "1h": 250, "1d": 250}


def check_window(interval: str, lookback: int, unit: str, live: bool = True) -> None:
    """A dip bot's window must make sense for its interval and fit the price history it can read. live=False: a
    backtest's window, which reads years of bars, so only has to make sense."""
    if interval == "1d" and unit == "hours":
        raise ValueError("a once-a-day check measures the fall in days, not hours")
    if unit == "hours" and lookback * 60 < INTERVAL_MINUTES[interval]:
        raise ValueError("the window must be at least one check long")
    days = lookback if unit == "days" else lookback / 6.5
    if live and days > MAX_LOOKBACK_DAYS[interval]:
        raise ValueError(f"with {interval} checks the window can be at most {MAX_LOOKBACK_DAYS[interval]} trading days "
                         "(the live price feed keeps only so much history)")


class PriceTier(BaseModel):
    """drop_mode "price": symbols whose reference price is under `up_to` (the last tier: any price) need `share` of
    drop_pct to be bought (0.5 = half of it). The backtest's PriceTier."""

    up_to: float | None = Field(None, gt=0)
    share: float = Field(gt=0, le=5)


# Under $100: x; $100-500: 5/6 of x; $500-1000: 2/3 of x; $1000 and up: 1/3 of x (3% -> 3 / 2.5 / 2 / 1%)
DEFAULT_PRICE_TIERS = [{"up_to": 100, "share": 1.0}, {"up_to": 500, "share": 5 / 6}, {"up_to": 1000, "share": 2 / 3},
                       {"up_to": None, "share": 1 / 3}]


def check_tiers(tiers: list[PriceTier] | None) -> list[PriceTier] | None:
    """Prices going up, each tier but the last with its top, the last one without (it holds every price above)."""
    if tiers is None:
        return None
    tops = [t.up_to for t in tiers[:-1]]
    if not tiers or None in tops or tiers[-1].up_to is not None:
        raise ValueError("every price tier but the last needs its top price; the last one has none")
    if any(b <= a for a, b in zip(tops, tops[1:])):
        raise ValueError("price tiers must go up")
    return tiers


DropMode = Literal["percent", "price", "volatility"]


class DipRules(BaseModel):
    """A dip buyer's rules: the backtest's DipConfig, same names and meaning, so a tested setup deploys 1:1."""

    interval: Interval = "15m"  # check every ...
    drop_pct: float = Field(0.05, gt=0, le=0.5)  # buy a fall this big ...
    # ... for every symbol, scaled by its price tier ("price"), or drop_atr times its usual daily move ("volatility")
    drop_mode: DropMode = "percent"
    price_tiers: list[PriceTier] = Field(default_factory=lambda: [PriceTier(**t) for t in DEFAULT_PRICE_TIERS],
                                         min_length=1, max_length=8)
    drop_atr: float = Field(1.5, gt=0, le=10)
    lookback: int = Field(5, ge=1, le=1600)  # ... during the last `lookback` ...
    lookback_unit: Literal["days", "hours"] = "days"  # ... trading days or market hours
    drop_from: Literal["high", "start"] = "high"  # measured from the window's highest close, or its first one
    target_mode: Literal["reference", "percent"] = "reference"  # sell back at that price, or rise_pct above the buy
    rise_pct: float = Field(0.05, gt=0, le=2)
    stop_pct: float = Field(0.05, gt=0, le=0.5)  # sell this far under the buy, and blacklist the symbol ...
    reenable_days: int = Field(0, ge=0, le=500)  # ... for this many trading days; 0 = until you re-enable it
    max_positions: int = Field(5, ge=1, le=20)  # slots: each buy gets 1/max_positions of the equity
    max_hold_days: int = Field(0, ge=0, le=250)  # sell after this many trading days whatever the price; 0 = never
    news: bool = False  # ask the news before a buy: bearish = not today
    trend_filter: bool = False  # only buy dips of symbols in an uptrend (50-day average over the 200-day)
    # Wait for the turn: buy a fall only once it is back up rebound_pct from its low (bearish turned bullish)
    rebound: bool = True
    rebound_pct: float = Field(0.01, gt=0, le=0.5)
    # Buy fractions of a share, so a slot smaller than one share's price still buys (Alpaca: $1 or more, symbols it
    # can split; any other symbol is bought in whole shares). Off = whole shares only
    fractional: bool = False

    @field_validator("price_tiers")
    @classmethod
    def _tiers(cls, v: list[PriceTier]) -> list[PriceTier]:
        return check_tiers(v)

    @model_validator(mode="after")
    def _window_fits(self):
        check_window(self.interval, self.lookback, self.lookback_unit)
        return self


class DipBotCreate(DipRules):
    """A dip buyer: its rules plus the watchlist, the broker and the capital."""

    name: str | None = Field(None, max_length=80)
    broker: str = "paper"
    confirm_live: bool = False  # a real-money broker: you said yes to real orders
    allocated_cash: float = Field(10_000, ge=100, le=10_000_000)
    symbols: list[str] = Field(..., min_length=1, max_length=60)
    fee_pct: float = Field(0.0, ge=0, le=0.05)
    slippage_pct: float = Field(0.0005, ge=0, le=0.05)
    max_drawdown_pct: float = Field(0.2, ge=0, le=1)

    @field_validator("symbols")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        out = _tickers(v)
        if not out:
            raise ValueError("pick at least one symbol")
        return out


class CapitalAdd(BaseModel):
    """More money for a running bot (POST /bots/{id}/capital)."""

    amount: float = Field(gt=0, le=10_000_000)
    top_up: bool = False  # dip bots: the next check that can buy brings each holding up to a full slot first
    confirm_live: bool = False  # required on a real-money bot


class DipBotUpdate(BaseModel):
    """Change a dip buyer's rules while it runs; only the fields sent change. New rules apply from the next check; a
    position already held keeps the target and stop it was bought with."""

    name: str | None = Field(None, max_length=80)
    interval: Interval | None = None
    drop_pct: float | None = Field(None, gt=0, le=0.5)
    drop_mode: DropMode | None = None
    price_tiers: list[PriceTier] | None = Field(None, min_length=1, max_length=8)
    drop_atr: float | None = Field(None, gt=0, le=10)
    lookback: int | None = Field(None, ge=1, le=1600)
    lookback_unit: Literal["days", "hours"] | None = None
    drop_from: Literal["high", "start"] | None = None
    target_mode: Literal["reference", "percent"] | None = None
    rise_pct: float | None = Field(None, gt=0, le=2)
    stop_pct: float | None = Field(None, gt=0, le=0.5)
    reenable_days: int | None = Field(None, ge=0, le=500)
    max_positions: int | None = Field(None, ge=1, le=20)
    max_hold_days: int | None = Field(None, ge=0, le=250)
    news: bool | None = None
    trend_filter: bool | None = None
    rebound: bool | None = None
    rebound_pct: float | None = Field(None, gt=0, le=0.5)
    fractional: bool | None = None
    fee_pct: float | None = Field(None, ge=0, le=0.05)
    slippage_pct: float | None = Field(None, ge=0, le=0.05)
    max_drawdown_pct: float | None = Field(None, ge=0, le=1)

    @field_validator("price_tiers")
    @classmethod
    def _tiers(cls, v: list[PriceTier] | None) -> list[PriceTier] | None:
        return check_tiers(v)


class WatchSymbols(BaseModel):
    """Symbols to add to a dip bot's watchlist."""

    symbols: list[str] = Field(..., min_length=1, max_length=60)

    @field_validator("symbols")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return _tickers(v)


class DipPresetConfig(DipRules):
    """What a saved setup keeps: the rules and the watchlist, plus the backtest's capital, blacklist and periods. Saved
    from a backtest, so its window may be longer than a live bot's feed allows (the bot form says so when loaded)."""

    symbols: list[str] = Field(..., min_length=1, max_length=60)
    initial_cash: float | None = Field(None, ge=100, le=10_000_000)
    practice: tuple[date, date] | None = None
    exam: tuple[date, date] | None = None  # None: no exam
    period_months: int | None = Field(None, ge=1, le=120)  # "the last n months": the dashboard recounts from today

    @field_validator("symbols")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        out = _tickers(v)
        if not out:
            raise ValueError("pick at least one symbol")
        return out

    @model_validator(mode="after")
    def _window_fits(self):  # replaces DipRules' check: a backtest isn't limited by the live feed
        check_window(self.interval, self.lookback, self.lookback_unit, live=False)
        for period in (self.practice, self.exam):
            if period and period[1] <= period[0]:
                raise ValueError("a period must end after it starts")
        return self


class DipPresetIn(BaseModel):
    """Save a dip buyer setup under a name; a name that exists (ignoring case) is replaced."""

    name: str = Field(..., max_length=60)
    config: DipPresetConfig

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("give it a name")
        return v


class DipPresetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    config: dict  # as saved: DipPresetConfig (the dashboard fills any rule added since with its default)
    created_at: datetime
    updated_at: datetime


class DipTestIn(BaseModel):
    """One tested setup of a study: the setup (as a saved setup keeps it) and its numbers per window."""

    code: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=1, max_length=60)
    watchlist: str = Field("", max_length=60)
    config: DipPresetConfig
    results: dict[str, dict]  # period -> the grid's numbers
    detail: dict[str, dict] = {}  # period -> {"curve": [[date, equity, buy & hold], ...], "by_symbol": [...], ...}
    score: float | None = None
    pick: int | None = Field(None, ge=1)
    note: str = Field("", max_length=2000)
    extra: str | None = Field(None, max_length=120)  # a filter beyond the rules, which a saved setup can't keep
    quarters: dict | None = None  # twelve 3-month tests: {"won", "of", "worst", "returns"}


class DipStudyIn(BaseModel):
    """A study (a batch of tests run together). Saving a name that exists (ignoring case) replaces it and drops its
    tests; add the tests with POST /dip/studies/{id}/tests, in as many batches as you like."""

    name: str = Field(..., max_length=80)
    description: str = Field("", max_length=20_000)
    periods: dict[str, tuple[date, date]] = Field(..., min_length=1)  # in the grid's order

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("give it a name")
        return v


class DipStudyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    periods: dict
    created_at: datetime
    tests: int = 0


class DipTestOut(BaseModel):
    """A test in the grid: everything but its curves (GET /dip/tests/{id} has those)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    study_id: int
    code: str
    name: str
    watchlist: str
    config: dict
    results: dict
    score: float | None
    pick: int | None
    note: str
    extra: str | None = None
    quarters: dict | None = None


class DipTestDetailOut(DipTestOut):
    detail: dict


NotifyCategory = Literal["trades", "risk", "problems", "signals"]  # models.NOTIFY_CATEGORIES
EMAIL = r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$"


class EmailAlertsIn(BaseModel):
    """Who gets the email alerts and about what (notify.py). The mail server itself is set in .env (SMTP_*)."""

    enabled: bool = False
    recipients: list[str] = Field(default_factory=list, max_length=10)
    categories: list[NotifyCategory] = Field(default_factory=list)

    @field_validator("recipients")
    @classmethod
    def _addresses(cls, v: list[str]) -> list[str]:
        out = list(dict.fromkeys(a.strip() for a in v if a.strip()))
        bad = [a for a in out if not re.match(EMAIL, a)]
        if bad:
            raise ValueError(f"not an email address: {', '.join(bad)}")
        return out

    @field_validator("categories")
    @classmethod
    def _once(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _somewhere_to_send(self):
        if self.enabled and not self.recipients:
            raise ValueError("add an address to send the alerts to")
        return self


class NotifyCategoryOut(BaseModel):
    key: str
    label: str
    description: str


class EmailAlertsOut(BaseModel):
    enabled: bool
    recipients: list[str]
    categories: list[str]
    smtp_configured: bool  # SMTP_HOST is set: without it nothing is sent
    smtp_server: str  # "smtp.gmail.com:587 (starttls)"
    sender: str
    dashboard_url: str  # where the emails' links point (DASHBOARD_URL)
    available: list[NotifyCategoryOut]


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    bot_id: int | None
    category: str
    kind: str
    subject: str
    body: str
    status: str  # pending | sent | failed | skipped
    attempts: int
    next_attempt_at: datetime | None
    sent_at: datetime | None
    error: str | None


class WatchItemOut(BaseModel):
    """One symbol on a dip bot's watchlist, as of its last check."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    status: str  # watching | blacklisted
    added_at: datetime
    blacklisted_at: datetime | None
    blacklist_reason: str | None
    reenable_on: date | None = None  # a stop's blacklist ends that trading day; None = when you re-enable it
    checked_at: datetime | None
    last_price: float | None
    reference_price: float | None  # the window's high (or start)
    drop: float | None  # 0.06 = 6% under the reference
    buy_below: float | None = None  # the price at or under which it is in the buy zone
    in_zone: bool
    buy_drop: float | None = None  # the fall that was the buy zone at the last check (its own, with drop_mode)
    held: bool = False
    # The fall being followed: where it started (the target), since when, its low, when it turned up (rebound)
    dip_reference: float | None = None
    armed_at: datetime | None = None
    trough_price: float | None = None
    trough_at: datetime | None = None
    turned_at: datetime | None = None
    rebound_at: float | None = None  # waiting for the turn: bought at or above this price (the low + rebound_pct)
    sold_on: date | None
    news_sentiment: str | None
    news_at: datetime | None
    news_blocked_on: date | None
    trend_ok: bool | None


class SignalOut(BaseModel):
    """A dip bot's recommendation (models.SIGNAL_KINDS) and what the bot did about it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bot_id: int
    created_at: datetime
    symbol: str
    kind: str  # down | rebound | up | stop | time | news
    price: float | None
    reference_price: float | None
    change_pct: float | None
    message: str
    outcome: str


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
    # Buy fractions of a share where the broker can split the symbol, so a slot smaller than one share still buys
    fractional: bool = True
    fee_pct: float = Field(0.0, ge=0, le=0.05)
    slippage_pct: float = Field(0.0005, ge=0, le=0.05)
    max_drawdown_pct: float = Field(0.2, ge=0, le=1)

    @field_validator("universe")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        out = _tickers(v)
        if len(out) < 2:
            raise ValueError("pick at least 2 symbols")
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
    fractional: bool | None = None  # rotation bots only (a dip bot's goes through PATCH /bots/{id}/dip)


class HoldingOut(BaseModel):
    """One symbol a rotation or dip bot holds, valued at its last price."""

    symbol: str
    shares: float  # a fraction of a share on a dip or rotation bot with `fractional`
    cost_basis: float
    last_price: float | None
    last_price_at: datetime | None
    value: float
    weight_pct: float  # of the bot's equity
    unrealized_pnl: float
    opened_at: datetime | None = None
    # --- dip bots only ---
    entry_price: float | None = None
    reference_price: float | None = None  # the price the fall started from
    target_price: float | None = None
    stop_price: float | None = None
    on_watchlist: bool = True  # False: removed from the watchlist; held until it exits


class BotOut(StrategyParams):
    model_config = ConfigDict(from_attributes=True)

    strategy: Strategy | Rotation | Dip
    id: int
    name: str
    symbol: str
    broker: str
    live: bool
    status: str
    allocated_cash: float  # the starting capital
    added_cash: float | None = None  # money added since (None = none)
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
    capital: float  # what you put in: allocated_cash + added_cash; return_pct and buy_hold_return_pct are measured on it
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
    fractional: bool | None = None  # rotation and dip bots: fractions of a share (None on bots from before = whole shares)
    holdings: list[HoldingOut] = []
    rebalancing: bool = False  # a rebalance's orders are still being sent (sells first, then the buys)
    # --- dip buyers only (None / empty on the others); `universe` = the starting watchlist (the benchmark) ---
    interval: Interval | None = None
    drop_pct: float | None = None
    drop_mode: str | None = None
    price_tiers: list[dict] | None = None
    drop_atr: float | None = None
    lookback: int | None = None
    lookback_unit: str | None = None
    drop_from: str | None = None
    target_mode: str | None = None
    rise_pct: float | None = None
    max_positions: int | None = None
    max_hold_days: int | None = None
    reenable_days: int | None = None
    news: bool | None = None
    trend_filter: bool | None = None
    rebound: bool | None = None
    rebound_pct: float | None = None
    top_up: bool | None = None  # money was added with "top up": the next check that can buy tops up the holdings
    watchlist: list[WatchItemOut] = []


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
    qty: float
    reason: str
    status: str
    client_order_id: str
    broker_order_id: str | None
    filled_qty: float
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
    benchmark: float | None = None  # buy & hold's value that day, money added included (None on days from before)


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
