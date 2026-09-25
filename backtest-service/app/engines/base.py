from abc import ABC, abstractmethod
from typing import Awaitable, Callable

import pandas as pd

from ..models import RunConfig

# Callback types the runner passes into an engine:
#   DecideFn: given (symbol, as_of, slot) -> signal dict from decision-service; slot is "open" (10:00) or "close"
#   EmitFn:   push a progress event out to any connected monitors (WebSocket)
DecideFn = Callable[[str, "date", str], Awaitable[dict]]  # noqa: F821
EmitFn = Callable[[dict], Awaitable[None]]


class BacktestEngine(ABC):
    """What the runner needs from a backtest simulator. SimplePortfolioEngine is the one in use."""

    name: str = "base"

    @abstractmethod
    async def run(self, cfg: RunConfig, prices: pd.Series, decide: DecideFn, emit: EmitFn,
                  bars: pd.DataFrame | None = None, open_slots: pd.DataFrame | None = None) -> dict:
        """Run the backtest and return a result dict:
        {metrics: {...}, trades: [...], equity_curve: [{date, equity, price}]}.
        `prices` is a close-price Series indexed by trading day for the whole window. `bars` (optional, same
        index) adds each day's Open/High/Low, so stops and targets can trigger during the day. `open_slots`
        (needed when cfg.decide_at is "open" or "both") gives each day's 10:00 price and the range before and
        after it."""
        raise NotImplementedError
