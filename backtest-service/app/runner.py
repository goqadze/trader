import asyncio
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pandas as pd
import yfinance as yf

from .decision_client import get_signal
from .engines import SimplePortfolioEngine
from .models import RunConfig


class Run:
    """In-memory record of one backtest, plus a tiny pub/sub so multiple monitors
    (WebSocket clients) can watch the same run live. Not persisted — restarting the
    service clears all runs; swap in a DB later if you need history."""

    def __init__(self, cfg: RunConfig):
        self.id = uuid.uuid4().hex[:12]
        self.cfg = cfg
        self.status = "pending"  # pending | running | done | error
        self.created_at = datetime.now(timezone.utc).isoformat()
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


NEW_YORK = "America/New_York"
CLOSE_HOUR = 16  # regular close, New York time (half days close earlier; waiting until 16:00 is still safe)


def _fetch_prices(symbol: str, start: date, end: date, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Download daily bars (Open, High, Low, Close) for the backtest window (runs in a thread: yfinance blocks).
    Decisions and equity use the close; the high and low tell whether a stop or target was hit during the day."""
    df = yf.download(symbol, start=start, end=end + timedelta(days=1), progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    bars = df[["Open", "High", "Low", "Close"]].dropna()
    bars.index = [d.date() for d in bars.index]  # use plain dates as the index
    return _finished_sessions(bars, now or pd.Timestamp.now(tz=NEW_YORK))


def _finished_sessions(close: pd.Series | pd.DataFrame, now: pd.Timestamp) -> pd.Series | pd.DataFrame:
    """Drop today's bar while the market is still open: yfinance returns the latest intraday price as today's
    "close", so a backtest ending today would decide and mark equity on a price that isn't a close yet."""
    now = now.tz_convert(NEW_YORK)
    if now.hour < CLOSE_HOUR:
        close = close.loc[[d < now.date() for d in close.index]]
    return close


# A strategy comparison starts one run per strategy at once. Each decision day is a decision-service call
# (news + LLM), so only this many runs execute together; the rest wait their turn as "pending".
MAX_PARALLEL_RUNS = int(os.getenv("MAX_PARALLEL_RUNS", "4"))
_slots = asyncio.Semaphore(MAX_PARALLEL_RUNS)

# Runs of one comparison ask for the same symbol and window: download it once and share it for a while.
PRICE_TTL_S = 600
_price_cache: dict[tuple, tuple[float, pd.DataFrame]] = {}
_price_locks: dict[tuple, asyncio.Lock] = {}


async def _load_prices(symbol: str, start: date, end: date) -> pd.DataFrame:
    """_fetch_prices through a short-lived cache. The per-key lock makes simultaneous runs wait for the
    first download instead of each hitting Yahoo (which rate-limits bursts)."""
    key = (symbol.upper(), start, end)
    async with _price_locks.setdefault(key, asyncio.Lock()):
        now = time.monotonic()
        hit = _price_cache.get(key)
        if hit and now - hit[0] < PRICE_TTL_S:
            return hit[1]
        prices = await asyncio.to_thread(_fetch_prices, symbol, start, end)
        for k in [k for k, (t, _) in _price_cache.items() if now - t >= PRICE_TTL_S]:  # drop stale windows
            del _price_cache[k]
            _price_locks.pop(k, None)
        _price_cache[key] = (now, prices)
        return prices


def start_run(cfg: RunConfig) -> Run:
    """Create a run and kick off its execution in the background."""
    run = Run(cfg)
    RUNS[run.id] = run
    asyncio.create_task(_execute(run))
    return run


async def _execute(run: Run) -> None:
    cfg = run.cfg
    async with _slots:  # stays "pending" until a slot frees up
        run.status = "running"
        try:
            # 1) Load prices for the window (the download runs in a thread)
            bars = await _load_prices(cfg.symbol, cfg.start, cfg.end)
            if bars is None or len(bars) < 2:
                raise ValueError(f"No price data for {cfg.symbol} in {cfg.start}..{cfg.end}")

            # 2) One shared HTTP client for all decision-service calls during this run
            async with httpx.AsyncClient() as client:
                async def decide(symbol: str, as_of: date) -> dict:
                    return await get_signal(client, symbol, as_of, cfg.strategy, cfg.stop_pct, cfg.target_pct)

                run.result = await SimplePortfolioEngine().run(cfg, bars["Close"], decide, run.emit, bars=bars)
            run.status = "done"
        except Exception as e:
            run.status = "error"
            await run.emit({"type": "error", "message": str(e)})
