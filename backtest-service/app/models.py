import math
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .futures import CONTRACTS

# decision-service's strategies (GET /strategies there describes each one). Keep in sync with
# decision-service/app/strategies.py STRATEGIES; validating here fails fast instead of a whole run of HOLDs.
Strategy = Literal["sma_rsi", "trend_following", "momentum", "breakout", "mean_reversion", "range_trading",
                   "ma_pullback", "reversal", "gap_and_go", "news_catalyst", "fibonacci", "orb", "ict_sweep_fvg", "ict_amd"]
# Intraday strategies (decision-service/app/intraday_strategies.py): one setup a day on 5-minute bars, every
# position closed by 15:55. They run in the intraday engine, not the daily one, and live bots can't run them yet.
# They also trade index futures (NQ, MNQ, ES, MES, YM, MYM; futures.py), which the daily strategies don't.
INTRADAY_STRATEGIES = {"orb", "ict_sweep_fvg", "ict_amd"}
FUTURES_NEED_INTRADAY = "index futures (NQ, MNQ, ES, MES, YM, MYM) run with the intraday strategies only"
INTRADAY_SLIPPAGE = 0.0001  # their default slippage per market fill (0.01%), see RunSettings._intraday_costs
# Crypto pairs, written like Yahoo (BTC-USD), whose daily prices the backtests read (Alpaca calls the pair BTC/USD).
# They trade around the clock, 7 days a week, in fractions of a coin, long only, and Alpaca charges a fee per trade:
# 0.25% for a market order at the lowest volume tier, the default unless a run sets its own fee_pct.
CRYPTO_FEE = 0.0025
CRYPTO_NEEDS_DAILY = "crypto (e.g. BTC-USD) runs with the daily strategies only: the intraday ones trade New York's session"


def is_crypto(symbol: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]+-USD", symbol.strip().upper()))


class RunSettings(BaseModel):
    """How a backtest trades: everything in a RunConfig except what and when (symbol and window).
    A scan runs one set of these per strategy across many symbols and two windows."""

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
    # False = technical only: decision-service skips the news step (no news fetch, no sentiment LLM). Much faster and
    # free, for screening; News catalyst can't trade without news. Live bots always use news.
    news: bool = True
    # Intraday strategies only. risk_pct: what one trade may lose (the stop decides the share count, capped by the
    # cash: no leverage). sides: "both" lets them short (live, that needs a margin account), "long" doesn't.
    risk_pct: float = Field(0.01, gt=0, le=0.05)
    sides: Literal["long", "both"] = "both"
    # The ICT strategies' way in: "limit" waits for the price to come back to the gap's middle (often it doesn't, and
    # the order is cancelled at 11:00); "market" buys at the next bar's open right after the setup, at a worse price
    # but every time. Same setup, stop and target. ORB always enters at market.
    entry: Literal["limit", "market"] = "limit"
    # The ICT strategies' higher-timeframe trend filter: only trade in the direction of the 1-hour, 4-hour or daily
    # trend (its last finished close above / below its 20-bar average). Fewer setups, never more.
    htf: Literal["off", "1h", "4h", "1d"] = "off"

    @model_validator(mode="after")
    def _intraday_costs(self):
        # Intraday strategies trade liquid ETFs with tight stops, where 0.05% a fill eats a third of a trade's risk:
        # QQQ and SPY trade with a one-cent spread (~0.002%), so 0.01% is still on the cautious side. Unless asked
        if self.strategy in INTRADAY_STRATEGIES and "slippage_pct" not in self.model_fields_set:
            self.slippage_pct = INTRADAY_SLIPPAGE
        return self


class RunConfig(RunSettings):
    """Everything the user configures from the frontend before starting a backtest."""

    symbol: str = "AAPL"  # a stock or ETF, or an index future (futures.CONTRACTS) for the intraday strategies
    start: date  # first day of the backtest window
    end: date  # last day of the backtest window

    @model_validator(mode="after")
    def _futures_intraday(self):
        if self.symbol.upper() in CONTRACTS and self.strategy not in INTRADAY_STRATEGIES:
            raise ValueError(FUTURES_NEED_INTRADAY)
        return self

    @model_validator(mode="after")
    def _crypto(self):
        if not is_crypto(self.symbol):
            return self
        if self.strategy in INTRADAY_STRATEGIES:
            raise ValueError(CRYPTO_NEEDS_DAILY)
        self.decide_at = "close"  # it never closes, so there's no 10:00 open or 15:30 slot: it decides on the daily close
        if "fee_pct" not in self.model_fields_set:
            self.fee_pct = CRYPTO_FEE
        return self


def clean_symbols(v: list[str]) -> list[str]:
    """Upper-cased tickers in their given order, each once; anything that isn't a ticker is refused."""
    out = []
    for s in v:
        s = s.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", s):
            raise ValueError(f"not a ticker: {s!r}")
        if s not in out:
            out.append(s)
    return out


class Period(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class ScanConfig(BaseModel):
    """Many symbols x a few strategies, on a practice window and optionally an unseen exam window after it.
    Each strategy brings its own settings (the frontend sends each strategy's suggested ones)."""

    symbols: list[str] = Field(min_length=1, max_length=60)  # e.g. the top 20 ETFs + the top 20 companies
    runs: list[RunSettings] = Field(min_length=1, max_length=11)  # one per strategy
    practice: Period
    exam: Period | None = None
    # False: only the combinations that passed practice ("Worth paper trading") sit the exam, as the method says
    # (testing everything on the exam and keeping the winners would turn the exam into more practice)
    exam_all: bool = False

    @field_validator("symbols")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return clean_symbols(v)

    @model_validator(mode="after")
    def _check(self):
        if len({r.strategy for r in self.runs}) != len(self.runs):
            raise ValueError("each strategy at most once")
        if self.exam and self.exam.start <= self.practice.end:
            raise ValueError("the exam period must start after the practice period ends, or it isn't an unseen test")
        if any(s in CONTRACTS for s in self.symbols) and any(r.strategy not in INTRADAY_STRATEGIES for r in self.runs):
            raise ValueError(FUTURES_NEED_INTRADAY)
        if any(is_crypto(s) for s in self.symbols) and any(r.strategy in INTRADAY_STRATEGIES for r in self.runs):
            raise ValueError(CRYPTO_NEEDS_DAILY)
        return self


class RotationConfig(BaseModel):
    """Momentum rotation: a portfolio across several symbols. At the start and each month's last trading day, rank
    the universe by its return over the last `lookback_months` (skipping the latest `skip_months`, the classic 12-1
    momentum), hold the top `top_n` in equal parts, sell the rest. Judged against holding the whole universe in
    equal parts. One window per run: the frontend runs a practice and an exam window."""

    strategy: Literal["momentum_rotation"] = "momentum_rotation"
    symbols: list[str] = Field(min_length=2, max_length=60)
    start: date
    end: date
    top_n: int = Field(3, ge=1, le=20)
    lookback_months: int = Field(12, ge=1, le=24)
    skip_months: int = Field(1, ge=0, le=3)  # the latest month tends to reverse: the classic momentum skips it
    abs_filter: bool = True  # only hold symbols that rose over the lookback; a slot without one stays in cash
    initial_cash: float = 10_000.0
    slippage_pct: float = Field(0.0005, ge=0)
    fee_pct: float = Field(0.0, ge=0)
    max_drawdown_pct: float = Field(0.0, ge=0, lt=1)  # off by default: a portfolio isn't paused like a bot

    @field_validator("symbols")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return clean_symbols(v)

    @model_validator(mode="after")
    def _check(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.top_n > len(self.symbols):
            raise ValueError("top_n can't be more than the number of symbols")
        return self


# The dip buyer's check intervals: how often it looks at its symbols. "1d" = once a day, on the close (a live bot: in
# the last 30 minutes before it). Minutes of each: a check happens at the end of each bar of that length.
DIP_INTERVALS = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 390}
MOVE_DAYS = 14  # drop_mode "volatility": a symbol's usual daily move is the average of its last 14 daily ranges


class PriceTier(BaseModel):
    """drop_mode "price": symbols whose reference price is under `up_to` (the last tier: any price) need `share` of
    drop_pct to be bought (0.5 = half of it)."""

    up_to: float | None = Field(None, gt=0)
    share: float = Field(gt=0, le=5)


# Under $100: x; $100-500: 5/6 of x; $500-1000: 2/3 of x; $1000 and up: 1/3 of x (3% -> 3 / 2.5 / 2 / 1%)
DEFAULT_PRICE_TIERS = [{"up_to": 100, "share": 1.0}, {"up_to": 500, "share": 5 / 6}, {"up_to": 1000, "share": 2 / 3},
                       {"up_to": None, "share": 1 / 3}]


def check_tiers(tiers: list[PriceTier]) -> list[PriceTier]:
    """Prices going up, each tier but the last with its top, the last one without (it holds every price above)."""
    tops = [t.up_to for t in tiers[:-1]]
    if not tiers or None in tops or tiers[-1].up_to is not None:
        raise ValueError("every price tier but the last needs its top price; the last one has none")
    if any(b <= a for a, b in zip(tops, tops[1:])):
        raise ValueError("price tiers must go up")
    return tiers


class DipConfig(BaseModel):
    """Buy the dip: watch several symbols; whenever one has fallen `drop_pct` during the last `lookback` days (or
    hours) -- and, with `rebound`, then turned up `rebound_pct` from its low -- buy it, and sell when it is back at the
    price the fall started from (or up `rise_pct` from the buy). A
    stop-loss `stop_pct` below the buy sells it and blacklists the symbol: the bot never buys it again until you
    re-enable it. Every `interval` it checks all of them, like the trading-service's dip bot (same names there).
    Judged against holding all the symbols in equal parts."""

    strategy: Literal["dip_buyer"] = "dip_buyer"
    symbols: list[str] = Field(min_length=1, max_length=60)
    start: date
    end: date
    interval: Literal["5m", "15m", "30m", "1h", "1d"] = "15m"
    drop_pct: float = Field(0.05, gt=0, le=0.5)  # x: how far it must fall to be bought
    # How x applies to each symbol: "percent" = drop_pct for all; "price" = drop_pct times the share of the tier its
    # reference price is in (price_tiers: pricier stocks need a smaller fall); "volatility" = drop_atr times its usual
    # daily move (the average of its last MOVE_DAYS daily ranges, as a fraction of the price). See `buy_fall`
    drop_mode: Literal["percent", "price", "volatility"] = "percent"
    price_tiers: list[PriceTier] = Field(default_factory=lambda: [PriceTier(**t) for t in DEFAULT_PRICE_TIERS],
                                         min_length=1, max_length=8)
    drop_atr: float = Field(1.5, gt=0, le=10)
    lookback: int = Field(5, ge=1, le=250)  # y: the window the fall is measured in ...
    lookback_unit: Literal["days", "hours"] = "days"  # ... in trading days, or market hours
    # The price the fall is measured from (and, with target_mode "reference", the price to get back to): the highest
    # close in the window (it rose, then turned down) or the close at its start (down x% over the last y days)
    drop_from: Literal["high", "start"] = "high"
    target_mode: Literal["reference", "percent"] = "reference"  # back at the starting price, or up rise_pct from the buy
    rise_pct: float = Field(0.05, gt=0, le=2)
    stop_pct: float = Field(0.05, gt=0, le=0.5)  # z: sell this far below the buy, and blacklist the symbol
    max_positions: int = Field(5, ge=1, le=20)  # each buy gets an equal slot of the equity: 1/max_positions
    max_hold_days: int = Field(0, ge=0, le=250)  # sell after this many trading days whatever the price; 0 = never
    # Wait for the turn (x1): after the fall, buy only once the price is back up rebound_pct from its lowest close since
    # the fall reached drop_pct -- bearish turned bullish -- and still under the price the fall started from. Off: buy
    # as soon as it has fallen drop_pct
    rebound: bool = True
    rebound_pct: float = Field(0.01, gt=0, le=0.5)
    # Ask the news before each buy: bearish news (the fall has a reason) blocks buying that symbol for the rest of
    # the day. Slower: one news call (+ the sentiment LLM) per buy
    news: bool = False
    trend_filter: bool = False  # only buy dips of symbols in a long-term uptrend: 50-day average above the 200-day
    # Buy fractions of a share (to a millionth, $1 or more, like Alpaca's fractional orders), so a slot smaller than one
    # share's price still buys. Off = whole shares only. The backtest assumes every symbol can be split; the bot buys
    # whole shares of one its broker can't split
    fractional: bool = False
    # Backtest only: re-enable a blacklisted symbol after this many trading days (stands in for you doing it).
    # 0 = never, like a bot whose blacklist you never touch
    reenable_days: int = Field(0, ge=0, le=500)
    initial_cash: float = 10_000.0
    slippage_pct: float = Field(0.0005, ge=0)
    fee_pct: float = Field(0.0, ge=0)
    max_drawdown_pct: float = Field(0.0, ge=0, lt=1)  # stop buying once equity is this far below its peak; 0 = off

    @field_validator("symbols")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        out = clean_symbols(v)
        if bad := [s for s in out if s in CONTRACTS or is_crypto(s)]:
            raise ValueError(f"the dip buyer trades stocks and ETFs, not {', '.join(bad)}")
        return out

    @field_validator("price_tiers")
    @classmethod
    def _tiers(cls, v: list[PriceTier]) -> list[PriceTier]:
        return check_tiers(v)

    @model_validator(mode="after")
    def _check(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.interval == "1d" and self.lookback_unit == "hours":
            raise ValueError("a once-a-day check measures the fall in days, not hours")
        if self.lookback_unit == "hours" and self.lookback * 60 < DIP_INTERVALS[self.interval]:
            raise ValueError("the window must be at least one check long")
        return self

    @property
    def minutes(self) -> int:
        return DIP_INTERVALS[self.interval]

    def buy_fall(self, reference: float, move: float | None = None) -> float | None:
        """The fall (0.03 = 3%) that puts a symbol whose reference price is `reference` in the buy zone, its usual daily
        move being `move` (drop_mode "volatility" only). None when it can't be told (no daily move yet): not bought.
        The trading-service's dip bot has the same rule (app/dip.py buy_fall): keep the two the same."""
        if self.drop_mode == "price":
            share = next(t.share for t in self.price_tiers if t.up_to is None or reference < t.up_to)
            return min(0.5, self.drop_pct * share)
        if self.drop_mode == "volatility":
            return None if move is None or not move > 0 else min(0.5, self.drop_atr * move)
        return self.drop_pct

    def window_bars(self) -> int:
        """The window in checks: its length in bars of the check interval (a session has 390 minutes)."""
        if self.interval == "1d":
            return self.lookback
        per_day = math.ceil(390 / self.minutes)
        if self.lookback_unit == "days":
            return self.lookback * per_day
        return max(1, math.ceil(self.lookback * 60 / self.minutes))


class RunSummary(BaseModel):
    """Compact view of a run for the list endpoint."""

    run_id: str
    symbol: str
    status: str  # pending | running | done | error
    created_at: str
