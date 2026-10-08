import asyncio
import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import yfinance as yf

from .decision_client import BASE as DECISION_BASE, get_signal
from .engines import DipEngine, IntradayEngine, RotationEngine, SimplePortfolioEngine
from .engines.dip import daily_moves
from .models import INTRADAY_STRATEGIES, DipConfig, RotationConfig, RunConfig, is_crypto


class Run:
    """In-memory record of one backtest, plus a tiny pub/sub so multiple monitors
    (WebSocket clients) can watch the same run live. Not persisted — restarting the
    service clears all runs; swap in a DB later if you need history."""

    def __init__(self, cfg: RunConfig, keep_steps: bool = True):
        self.id = uuid.uuid4().hex[:12]
        self.cfg = cfg
        self.status = "pending"  # pending | running | done | error | stopped
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.events: list[dict] = []  # full history, so late joiners can catch up
        self.result: dict | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self._seq = 0
        # A scan starts hundreds of runs nobody watches live: their day-by-day "step" events (each with the decision
        # text) would fill the memory, so they keep only the latest step, for the progress
        self.keep_steps = keep_steps
        self.last_step: dict | None = None
        # A dip-buyer run's prices (one column per symbol, one row per check), for its charts (GET /runs/{id}/prices)
        self.prices: pd.DataFrame | None = None

    async def emit(self, ev: dict) -> None:
        """Record an event and fan it out to every connected monitor."""
        ev = {**ev, "seq": self._seq}
        self._seq += 1
        if ev["type"] == "step":
            self.last_step = ev
        if ev["type"] != "step" or self.keep_steps:
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
CLOSE_SLOT = dtime(15, 30)  # DECISION_MINUTES_BEFORE_CLOSE = 30
HALF_HOUR = pd.Timedelta(minutes=30)


def _yahoo_symbol(symbol: str) -> str:
    """Yahoo writes share classes with a dash (BRK-B); Alpaca, the bots and this app with a dot (BRK.B)."""
    return symbol.replace(".", "-")


def _fetch_prices(symbol: str, start: date, end: date, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Download daily bars (Open, High, Low, Close) for the backtest window (runs in a thread: yfinance blocks).
    Decisions and equity use the close; the high and low tell whether a stop or target was hit during the day."""
    df = yf.download(_yahoo_symbol(symbol), start=start, end=end + timedelta(days=1), progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    bars = df[["Open", "High", "Low", "Close"]].dropna()
    bars.index = [d.date() for d in bars.index]  # use plain dates as the index
    return _finished_sessions(bars, now or pd.Timestamp.now(tz=NEW_YORK))


def _intraday_bars(symbol: str, start: date, end: date, timeframe: str = "30Min") -> pd.DataFrame:
    """Regular-hours 30- or 5-minute bars from decision-service (which holds the market-data keys: Alpaca, years of
    history; Yahoo's last 60 days without them), indexed by each bar's start in New York time."""
    r = httpx.get(f"{DECISION_BASE}/bars/intraday", params={"symbol": symbol, "start": str(start), "end": str(end),
                                                             "timeframe": timeframe}, timeout=600)
    if r.status_code != 200:
        raise ValueError(f"{timeframe} prices for {symbol} unavailable: decision-service {r.status_code}: {r.text[:200]}")
    rows = r.json()
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    df = pd.DataFrame(rows).rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    df.index = pd.DatetimeIndex(pd.to_datetime(df["t"], utc=True)).tz_convert(NY)  # a year mixes -04:00 and -05:00
    return df[["Open", "High", "Low", "Close"]]


def _fetch_slots(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Per trading day, from 30-minute bars, what a trading bot sees at its two decision moments: 10:00 ("after the
    open") and the close slot (15:30; 30 minutes before an early close), with the price ranges in between:
    a = 9:30 to 10:00, b = 10:00 to the close slot, c = the close slot to the close. The engine decides at a slot
    on that moment's price and checks the stop and target on each stretch, in order. A day missing here (no
    30-minute prices) falls back to deciding on the day's close."""
    df = _intraday_bars(symbol, start, end)
    if df.empty:
        raise ValueError(f"No 30-minute prices for {symbol} in {start}..{end} (without Alpaca keys, Yahoo keeps only 60 days)")
    rows = {}
    for day, g in df.groupby(df.index.date):
        open_at = pd.Timestamp(datetime.combine(day, OPEN_SLOT), tz=NY)
        # The last 30 minutes before the close: 15:30, or 12:30 on a half day (whose last bar starts then)
        close_at = min(pd.Timestamp(datetime.combine(day, CLOSE_SLOT), tz=NY), g.index.max())
        done = g.index + HALF_HOUR  # when each bar finished (bars are indexed by their start)
        a, b, c = g[done <= open_at], g[(g.index >= open_at) & (done <= close_at)], g[g.index >= close_at]
        if a.empty:
            continue
        at_open = float(a["Close"].iloc[-1])
        at_close = float(b["Close"].iloc[-1]) if len(b) else at_open

        def span(part, fallback):
            return (float(part["High"].max()), float(part["Low"].min())) if len(part) else (fallback, fallback)

        (a_high, a_low), (b_high, b_low), (c_high, c_low) = span(a, at_open), span(b, at_close), span(c, at_close)
        rows[day] = {"open_price": at_open, "close_price": at_close, "close_at": close_at.strftime("%H:%M"),
                     "a_high": a_high, "a_low": a_low, "b_high": b_high, "b_low": b_low, "c_high": c_high, "c_low": c_low}
    return pd.DataFrame.from_dict(rows, orient="index")


def _five_minute_bars(symbol: str, start: date, end: date) -> pd.DataFrame:
    """The intraday strategies' bars (a function of its own: the price cache is keyed by the fetcher's name)."""
    return _intraday_bars(symbol, start, end, "5Min")


def _intraday_plans(cfg: RunConfig) -> list[dict]:
    """Each day's order plan from decision-service, which holds the intraday strategies' rules."""
    r = httpx.get(f"{DECISION_BASE}/intraday/plans", timeout=600, params={
        "symbol": cfg.symbol, "start": str(cfg.start), "end": str(cfg.end), "strategy": cfg.strategy, "sides": cfg.sides,
        "entry": cfg.entry, "htf": cfg.htf})
    if r.status_code != 200:
        raise ValueError(f"intraday plans for {cfg.symbol} unavailable: decision-service {r.status_code}: {r.text[:200]}")
    return r.json()


def _universe_closes(symbols_key: str, start: date, end: date) -> pd.DataFrame:
    """Daily closes of several symbols, one column each (a rotation's universe; `symbols_key` = "A,B,C" so the price
    cache can key on it). A symbol Yahoo doesn't know comes back as an empty column."""
    symbols = symbols_key.split(",")
    df = yf.download([_yahoo_symbol(s) for s in symbols], start=start, end=end + timedelta(days=1), progress=False, auto_adjust=True)
    close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": _yahoo_symbol(symbols[0])})
    close = close.rename(columns={_yahoo_symbol(s): s for s in symbols}).reindex(columns=symbols)
    close.index = [d.date() for d in close.index]
    return _finished_sessions(close, pd.Timestamp.now(tz=NEW_YORK))


def _daily_ranges(symbols_key: str, start: date, end: date) -> pd.DataFrame:
    """Daily highs, lows and closes of several symbols (columns: (High|Low|Close, symbol)), for drop_mode
    "volatility"'s daily moves. `symbols_key` = "A,B,C", like _universe_closes."""
    symbols = symbols_key.split(",")
    yahoo = [_yahoo_symbol(s) for s in symbols]
    df = yf.download(yahoo, start=start, end=end + timedelta(days=1), progress=False, auto_adjust=True)
    if not isinstance(df.columns, pd.MultiIndex):  # one symbol: plain columns
        df.columns = pd.MultiIndex.from_product([df.columns, yahoo[:1]])
    out = pd.concat({f: df[f].rename(columns={_yahoo_symbol(s): s for s in symbols}).reindex(columns=symbols)
                     for f in ("High", "Low", "Close")}, axis=1)
    out.index = [d.date() for d in out.index]
    return _finished_sessions(out, pd.Timestamp.now(tz=NEW_YORK))


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


def start_rotation_run(cfg: RotationConfig) -> Run:
    """Create a momentum-rotation run and execute it in the background."""
    run = Run(cfg)
    RUNS[run.id] = run
    asyncio.create_task(_execute_rotation(run))
    return run


async def _execute_rotation(run: Run) -> None:
    cfg: RotationConfig = run.cfg
    async with _slots:
        run.status = "running"
        try:
            # The momentum on the first day looks back lookback + skip months: download that much before the start
            first = cfg.start - pd.DateOffset(months=cfg.lookback_months + cfg.skip_months) - timedelta(days=10)
            closes = await _load_prices(",".join(cfg.symbols), first.date(), cfg.end, _universe_closes)
            run.result = await RotationEngine().run(cfg, closes, run.emit)
            run.status = "done"
        except Exception as e:
            run.status = "error"
            await run.emit({"type": "error", "message": str(e)})


# The dip buyer's check intervals -> the bars decision-service serves for them. Hourly checks are built from 30-minute
# bars (a session's hours start at 9:30, Alpaca's hourly bars on the hour).
DIP_TIMEFRAMES = {"5m": "5Min", "15m": "15Min", "30m": "30Min", "1h": "30Min"}
DIP_DOWNLOADS = 4  # symbols downloaded at once (each is one request per month to decision-service)
TREND_HISTORY_DAYS = 320  # calendar days before the start: 200+ trading days for the trend filter's 200-day average
MOVE_HISTORY_DAYS = 30  # calendar days before the first check: 15+ trading days for the daily moves


def _bar_closes(df: pd.DataFrame, minutes: int, step: int) -> pd.Series:
    """Closes of `step`-minute bars (each indexed by its start), regrouped into bars of `minutes` from the 9:30 open,
    each indexed by the moment it CLOSED: when a bot checking every `minutes` sees that price."""
    if df.empty:
        return pd.Series(dtype=float)
    starts = df.index
    if minutes == step:
        return pd.Series(df["Close"].values, index=starts + pd.Timedelta(minutes=step))
    opens = starts.normalize() + pd.Timedelta(hours=9, minutes=30)
    bucket = opens + ((starts - opens) // pd.Timedelta(minutes=minutes)) * pd.Timedelta(minutes=minutes)
    grouped = df.assign(bucket=bucket, done=starts + pd.Timedelta(minutes=step)).groupby("bucket")
    last = grouped.agg(close=("Close", "last"), done=("done", "max"))
    return pd.Series(last["close"].values, index=pd.DatetimeIndex(last["done"]))


def _dip_closes(key: str, start: date, end: date) -> pd.DataFrame:
    """The dip buyer's prices: one column per symbol, one row per check. key = "<interval>|A,B,C" (so the price cache
    keys on the interval too). "1d": daily closes from Yahoo, indexed by day. Otherwise the closes of each bar of the
    interval, indexed by the moment the bar closed (New York time), from decision-service's intraday bars."""
    interval, symbols_key = key.split("|", 1)
    symbols = symbols_key.split(",")
    if interval == "1d":
        close = _universe_closes(symbols_key, start, end)
        close.index = pd.DatetimeIndex(close.index)
        return close
    minutes = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}[interval]
    timeframe = DIP_TIMEFRAMES[interval]
    step = int(timeframe.removesuffix("Min"))

    def one(sym: str) -> pd.Series:
        return _bar_closes(_intraday_bars(sym, start, end, timeframe), minutes, step).rename(sym)

    with ThreadPoolExecutor(DIP_DOWNLOADS) as pool:
        columns = list(pool.map(one, symbols))
    close = pd.concat(columns, axis=1).reindex(columns=symbols)
    if close.dropna(how="all").empty:
        raise ValueError(f"No {timeframe} prices for {', '.join(symbols)} in {start}..{end}")
    return close.sort_index()


def _dip_history_start(cfg: DipConfig) -> date:
    """How far before the start to download: the first check needs a full window behind it (and the trend filter 200
    days of daily closes)."""
    days = cfg.lookback if cfg.lookback_unit == "days" else math.ceil(cfg.lookback / 6.5)
    return cfg.start - timedelta(days=int(days * 7 / 5) + 10)


def start_dip_run(cfg: DipConfig) -> Run:
    """Create a dip-buyer run and execute it in the background. Its result comes from GET /runs/{id}."""
    run = Run(cfg)
    RUNS[run.id] = run
    asyncio.create_task(_execute_dip(run))
    return run


NEWS_RETRY_DELAYS = (2.0, 5.0)


async def _news(client: httpx.AsyncClient, symbol: str, day: date, at: datetime | None) -> dict:
    """decision-service's news verdict for a symbol at a past moment (None: 15:30 on that day). Never raises: a
    failure is {"sentiment": "unavailable", "error": True} and the dip is bought on the prices alone."""
    params = {"symbol": symbol, "as_of": day.isoformat()}
    if at is not None:
        params["decided_at"] = at.isoformat()
    for delay in (*NEWS_RETRY_DELAYS, None):
        try:
            r = await client.get(f"{DECISION_BASE}/news/sentiment", params=params, timeout=90)
        except Exception:
            r = None
        if r is not None and r.status_code == 200:
            return r.json()
        if (r is not None and r.status_code < 500 and r.status_code != 429) or delay is None:
            break
        await asyncio.sleep(delay)
    return {"sentiment": "unavailable", "error": True}


async def _execute_dip(run: Run) -> None:
    cfg: DipConfig = run.cfg
    async with _slots:
        run.status = "running"
        try:
            key = f"{cfg.interval}|{','.join(cfg.symbols)}"
            trend_start = cfg.start - timedelta(days=TREND_HISTORY_DAYS)  # the 200-day average needs closes from way back
            first = min(_dip_history_start(cfg), trend_start) if cfg.trend_filter and cfg.interval == "1d" else _dip_history_start(cfg)
            closes = await _load_prices(key, first, cfg.end, _dip_closes)
            run.prices = closes
            daily = None
            if cfg.trend_filter and cfg.interval != "1d":  # intraday checks: the trend filter's daily closes come apart
                daily = (await _load_prices(",".join(cfg.symbols), trend_start, cfg.end, _universe_closes)).copy()
                daily.index = pd.DatetimeIndex(daily.index)
            moves = None
            if cfg.drop_mode == "volatility":  # each symbol's usual daily move, from daily bars a few weeks back
                ranges = await _load_prices(",".join(cfg.symbols), first - timedelta(days=MOVE_HISTORY_DAYS), cfg.end,
                                            _daily_ranges)
                moves = daily_moves(ranges["High"], ranges["Low"], ranges["Close"])
                moves.index = pd.DatetimeIndex(moves.index)
            async with httpx.AsyncClient() as client:
                async def news(symbol: str, day: date, at: datetime | None) -> dict:
                    return await _news(client, symbol, day, at)

                run.result = await DipEngine().run(cfg, closes, run.emit, daily=daily, news_fn=news, moves=moves)
            run.status = "done"
        except Exception as e:
            run.status = "error"
            await run.emit({"type": "error", "message": str(e)})


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
            if cfg.strategy in INTRADAY_STRATEGIES:  # 5-minute bars + the day's order plans, no per-day decisions
                bars5 = await _load_prices(cfg.symbol, cfg.start, cfg.end, _five_minute_bars)
                plans = await asyncio.to_thread(_intraday_plans, cfg)
                run.result = await IntradayEngine().run(cfg, bars5, plans, run.emit)
                run.status = "done"
                return
            # 1) Load prices for the window (the download runs in a thread)
            bars = await _load_prices(cfg.symbol, cfg.start, cfg.end)
            if bars is None or len(bars) < 2:
                raise ValueError(f"No price data for {cfg.symbol} in {cfg.start}..{cfg.end}")
            # Each decision moment as the bot sees it (10:00 / 15:30). Without them a close decision falls back to the
            # finished day; a 10:00 decision can't be replayed at all
            try:
                # Crypto trades around the clock: no 10:00 or 15:30 moments, it decides on the daily close
                slots = pd.DataFrame() if is_crypto(cfg.symbol) else await _load_prices(cfg.symbol, cfg.start, cfg.end, _fetch_slots)
            except ValueError:
                if cfg.decide_at != "close":
                    raise
                slots = pd.DataFrame()

            # 2) One shared HTTP client for all decision-service calls during this run
            async with httpx.AsyncClient() as client:
                async def decide(symbol: str, as_of: date, slot: str = "close") -> dict:
                    # Replay the slot's moment: prices and news as they stood then, like a live bot
                    if slot == "open":
                        at = datetime.combine(as_of, OPEN_SLOT, tzinfo=NY)
                    elif as_of in slots.index:
                        at = datetime.combine(as_of, dtime.fromisoformat(slots.loc[as_of, "close_at"]), tzinfo=NY)
                    else:
                        at = None  # no 30-minute prices for this day: the finished day, news up to 15:30
                    return await get_signal(client, symbol, as_of, cfg.strategy, cfg.stop_pct, cfg.target_pct, at, cfg.news)

                run.result = await SimplePortfolioEngine().run(cfg, bars["Close"], decide, run.emit, bars=bars, slots=slots)
            run.status = "done"
        except asyncio.CancelledError:  # a scan was stopped
            run.status = "stopped"
            await run.emit({"type": "error", "message": "stopped"})
            raise
        except Exception as e:
            run.status = "error"
            await run.emit({"type": "error", "message": str(e)})
