import asyncio
import uuid
from datetime import date, datetime, timedelta

import httpx
import pandas as pd
import yfinance as yf

from .decision_client import get_signal
from .engines import ENGINES
from .models import RunConfig


class Run:
    """In-memory record of one backtest, plus a tiny pub/sub so multiple monitors
    (WebSocket clients) can watch the same run live. Not persisted — restarting the
    service clears all runs; swap in a DB later if you need history."""

    def __init__(self, cfg: RunConfig):
        self.id = uuid.uuid4().hex[:12]
        self.cfg = cfg
        self.status = "pending"  # pending | running | done | error
        self.created_at = datetime.utcnow().isoformat()
        self.events: list[dict] = []  # full history, so late joiners can catch up
        self.result: dict | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self._seq = 0

    async def emit(self, ev: dict) -> None:
        """Record an event and fan it out to every connected monitor."""
        ev = {**ev, "seq": self._seq}
        self._seq += 1
        self.events.append(ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)


# All runs live here, keyed by id.
RUNS: dict[str, Run] = {}


def _fetch_prices(symbol: str, start: date, end: date) -> pd.Series:
    """Download daily closes for the backtest window (runs in a thread — yfinance is blocking)."""
    df = yf.download(symbol, start=start, end=end + timedelta(days=1), progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"]
    close.index = [d.date() for d in close.index]  # use plain dates as the index
    return close


def start_run(cfg: RunConfig) -> Run:
    """Create a run and kick off its execution in the background."""
    run = Run(cfg)
    RUNS[run.id] = run
    asyncio.create_task(_execute(run))
    return run


async def _execute(run: Run) -> None:
    cfg = run.cfg
    run.status = "running"
    try:
        engine_cls = ENGINES.get(cfg.engine)
        if engine_cls is None:
            raise ValueError(f"Unknown engine '{cfg.engine}'. Available: {list(ENGINES)}")

        # 1) Load prices for the window (blocking call offloaded to a thread)
        prices = await asyncio.to_thread(_fetch_prices, cfg.symbol, cfg.start, cfg.end)
        if prices is None or len(prices) < 2:
            raise ValueError(f"No price data for {cfg.symbol} in {cfg.start}..{cfg.end}")

        # 2) One shared HTTP client for all decision-service calls during this run
        async with httpx.AsyncClient() as client:
            async def decide(symbol: str, as_of: date) -> dict:
                return await get_signal(client, symbol, as_of, cfg.mode)

            engine = engine_cls()
            run.result = await engine.run(cfg, prices, decide, run.emit)
        run.status = "done"
    except Exception as e:
        run.status = "error"
        await run.emit({"type": "error", "message": str(e)})
