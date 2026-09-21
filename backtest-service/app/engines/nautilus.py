"""NautilusTrader engine (optional, scaffold).

NautilusTrader (https://github.com/nautechsystems/nautilus_trader) is a production-grade,
event-driven backtest + live-trading engine. Wiring it in means, roughly:

  1. `pip install nautilus_trader` (uncomment it in requirements.txt).
  2. Define the instrument (e.g. AAPL on NASDAQ) and a Venue.
  3. Load historical bars into a BacktestEngine as a data catalog.
  4. Write a `Strategy` whose `on_bar` calls decision-service (via httpx) and submits
     MarketOrders based on the returned action/confidence.
  5. Run the engine and translate Nautilus' fills/positions into the same result dict
     shape returned by SimplePortfolioEngine (metrics + trades + equity_curve).

Until that is implemented, selecting the "nautilus" engine raises a clear error and you
should use the "simple" engine, which produces the same result shape and already works
end to end against decision-service."""

import pandas as pd

from ..models import RunConfig
from .base import BacktestEngine, DecideFn, EmitFn


class NautilusEngine(BacktestEngine):
    name = "nautilus"

    async def run(self, cfg: RunConfig, prices: pd.Series, decide: DecideFn, emit: EmitFn) -> dict:
        await emit({"type": "error", "message": "Nautilus engine is not implemented yet — use the 'simple' engine."})
        raise NotImplementedError(
            "NautilusEngine is a scaffold. Install nautilus_trader and implement the Strategy/on_bar loop, "
            "or use engine='simple'."
        )
