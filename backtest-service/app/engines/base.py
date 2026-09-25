from abc import ABC, abstractmethod
from typing import Awaitable, Callable

import pandas as pd

from ..models import RunConfig

# Callback types the runner passes into an engine:
#   DecideFn: given (symbol, as_of) -> signal dict from decision-service
#   EmitFn:   push a progress event out to any connected monitors (WebSocket)
DecideFn = Callable[[str, "date"], Awaitable[dict]]  # noqa: F821
EmitFn = Callable[[dict], Awaitable[None]]


class BacktestEngine(ABC):
    """What the runner needs from a backtest simulator. SimplePortfolioEngine is the one in use."""

    name: str = "base"

    @abstractmethod
    async def run(self, cfg: RunConfig, prices: pd.Series, decide: DecideFn, emit: EmitFn,
                  bars: pd.DataFrame | None = None) -> dict:
        """Run the backtest and return a result dict:
        {metrics: {...}, trades: [...], equity_curve: [{date, equity, price}]}.
        `prices` is a close-price Series indexed by trading day for the whole window. `bars` (optional, same
        index) adds each day's Open/High/Low, so stops and targets can trigger during the day."""
        raise NotImplementedError
