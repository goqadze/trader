import asyncio
import os
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

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
NY = ZoneInfo(NEW_YORK)
CLOSE_HOUR = 16  # regular close, New York time (half days close earlier; waiting until 16:00 is still safe)
# The "after the open" decision moment, as trading-service's default (DECISION_MINUTES_AFTER_OPEN = 30)
OPEN_SLOT = dtime(10, 0)
HALF_HOUR = pd.Timedelta(minutes=30)
# Yahoo keeps 30-minute bars for 60 days; a day of margin for time zones and the request's own timing
INTRADAY_DAYS = 59


def earliest_intraday_start() -> date:
    """The earliest backtest start that can replay 10:00 decisions."""
    return datetime.now(NY).date() - timedelta(days=INTRADAY_DAYS)


def _fetch_prices(symbol: str, start: date, end: date, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Download daily bars (Open, High, Low, Close) for the backtest window (runs in a thread: yfinance blocks).
    Decisions and equity use the close; the high and low tell whether a stop or target was hit during the day."""
    df = yf.download(symbol, start=start, end=end + timedelta(days=1), progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    bars = df[["Open", "High", "Low", "Close"]].dropna()
    bars.index = [d.date() for d in bars.index]  # use plain dates as the index
    return _finished_sessions(bars, now or pd.Timestamp.now(tz=NEW_YORK))


def _fetch_open_slots(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Per trading day, from 30-minute bars: the 10:00 New York price, the range before it (9:30-10:00) and the
    range after it (10:00 to the close). The engine decides at 10:00 on that price, then checks the stop and
    target on the rest of the day."""
    df = yf.download(symbol, start=start, end=end + timedelta(days=1), interval="30m", progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.empty:
        raise ValueError(f"No 30-minute prices for {symbol} in {start}..{end} (Yahoo keeps them for 60 days)")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df.tz_convert(NY).dropna(subset=["Close"])
    rows = {}
    for day, g in df.groupby(df.index.date):
        slot = pd.Timestamp(datetime.combine(day, OPEN_SLOT), tz=NY)
        before, after = g[g.index + HALF_HOUR <= slot], g[g.index >= slot]  # bars are indexed by their start
        if before.empty:
            continue
        price = float(before["Close"].iloc[-1])
        rows[day] = {"price": price, "high_before": float(before["High"].max()), "low_before": float(before["Low"].min()),
                     "high_after": float(after["High"].max()) if len(after) else price,
                     "low_after": float(after["Low"].min()) if len(after) else price}
    return pd.DataFrame.from_dict(rows, orient="index")


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


async def _load_prices(symbol: str, start: date, end: date, fetch=None) -> pd.DataFrame:
    """_fetch_prices (or another fetcher of the same shape) through a short-lived cache. The per-key lock makes
    simultaneous runs wait for the first download instead of each hitting Yahoo (which rate-limits bursts)."""
    fetch = fetch or _fetch_prices
    key = (symbol.upper(), start, end, fetch.__name__)
    async with _price_locks.setdefault(key, asyncio.Lock()):
        now = time.monotonic()
        hit = _price_cache.get(key)
        if hit and now - hit[0] < PRICE_TTL_S:
            return hit[1]
        prices = await asyncio.to_thread(fetch, symbol, start, end)
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
            open_slots = await _load_prices(cfg.symbol, cfg.start, cfg.end, _fetch_open_slots) if cfg.decide_at != "close" else None

            # 2) One shared HTTP client for all decision-service calls during this run
            async with httpx.AsyncClient() as client:
                async def decide(symbol: str, as_of: date, slot: str = "close") -> dict:
                    # The close decision uses the finished day (news up to 15:30), as before; the open one
                    # replays 10:00 (prices and news as they stood then)
                    at = datetime.combine(as_of, OPEN_SLOT, tzinfo=NY) if slot == "open" else None
                    return await get_signal(client, symbol, as_of, cfg.strategy, cfg.stop_pct, cfg.target_pct, at)

                run.result = await SimplePortfolioEngine().run(cfg, bars["Close"], decide, run.emit, bars=bars, open_slots=open_slots)
            run.status = "done"
        except Exception as e:
            run.status = "error"
            await run.emit({"type": "error", "message": str(e)})
