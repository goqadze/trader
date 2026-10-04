import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# decision-service's strategies (GET /strategies there describes each one). Keep in sync with
# decision-service/app/strategies.py STRATEGIES; validating here fails fast instead of a whole run of HOLDs.
Strategy = Literal["sma_rsi", "trend_following", "momentum", "breakout", "mean_reversion", "range_trading",
                   "ma_pullback", "reversal", "gap_and_go", "news_catalyst", "fibonacci", "orb", "ict_sweep_fvg"]
# Intraday strategies (decision-service/app/intraday_strategies.py): one setup a day on 5-minute bars, every
# position closed by 15:55. They run in the intraday engine, not the daily one, and live bots can't run them yet.
INTRADAY_STRATEGIES = {"orb", "ict_sweep_fvg"}
INTRADAY_SLIPPAGE = 0.0001  # their default slippage per market fill (0.01%), see RunSettings._intraday_costs


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

    @model_validator(mode="after")
    def _intraday_costs(self):
        # Intraday strategies trade liquid ETFs with tight stops, where 0.05% a fill eats a third of a trade's risk:
        # QQQ and SPY trade with a one-cent spread (~0.002%), so 0.01% is still on the cautious side. Unless asked
        if self.strategy in INTRADAY_STRATEGIES and "slippage_pct" not in self.model_fields_set:
            self.slippage_pct = INTRADAY_SLIPPAGE
        return self


class RunConfig(RunSettings):
    """Everything the user configures from the frontend before starting a backtest."""

    symbol: str = "AAPL"
    start: date  # first day of the backtest window
    end: date  # last day of the backtest window


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
    def _clean_symbols(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s = s.strip().upper()
            if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", s):
                raise ValueError(f"not a ticker: {s!r}")
            if s not in out:
                out.append(s)
        return out

    @model_validator(mode="after")
    def _check(self):
        if len({r.strategy for r in self.runs}) != len(self.runs):
            raise ValueError("each strategy at most once")
        if self.exam and self.exam.start <= self.practice.end:
            raise ValueError("the exam period must start after the practice period ends, or it isn't an unseen test")
        return self


class RunSummary(BaseModel):
    """Compact view of a run for the list endpoint."""

    run_id: str
    symbol: str
    status: str  # pending | running | done | error
    created_at: str
