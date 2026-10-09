import math
from datetime import date, datetime
from typing import Awaitable, Callable

import pandas as pd

from ..models import MOVE_DAYS, DipConfig
from ..verdict import judge
from .base import EmitFn
from .simple import SimplePortfolioEngine

# Before a buy with news on: (symbol, day, the check's moment or None for the close) -> {"sentiment", "error"?}
NewsFn = Callable[[str, date, datetime | None], Awaitable[dict]]

MAX_EVENTS = 3000  # the activity list kept in the result (a 5-minute run over years can blacklist and veto a lot)
QTY_DECIMALS = 6  # fractional buys are cut to a millionth of a share
MIN_FRACTIONAL_ORDER = 1.0  # Alpaca's smallest fractional order, in dollars


def affordable(budget: float, unit: float, fractional: bool = False) -> float:
    """How many shares `budget` buys at `unit` dollars each: whole shares, or with `fractional` down to a millionth of
    one. 0 when that isn't even one share (in fractions: under the $1 minimum). The rotation backtest uses it too; the
    trading-service's bots size their buys with the same rule (trading-service/app/shares.py): keep the two the same."""
    if unit <= 0 or budget <= 0:
        return 0
    if not fractional:
        return int(budget // unit)
    qty = math.floor(budget / unit * 10**QTY_DECIMALS) / 10**QTY_DECIMALS
    return qty if qty * unit >= MIN_FRACTIONAL_ORDER else 0


def references(closes: pd.DataFrame, cfg: DipConfig) -> pd.DataFrame:
    """The price each check measures the fall from, using only the checks before it: the highest close of the last
    `window` checks ("high"), or the close `window` checks ago ("start"). NaN until there are that many. The
    trading-service's dip bot computes the same from its live bars: keep the two the same."""
    window = cfg.window_bars()
    if cfg.drop_from == "high":
        return closes.rolling(window, min_periods=window).max().shift(1)
    return closes.shift(window)


def daily_moves(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, days: int = MOVE_DAYS) -> pd.DataFrame:
    """Each symbol's usual daily move as known before each day: the average of its last `days` daily ranges (high to
    low, stretched to the previous close when the price gapped past it), each as a fraction of that day's close. NaN
    until there are that many. drop_mode "volatility" buys a fall of drop_atr times this. The trading-service's dip
    bot computes the same from its daily bars (app/dip.py daily_move): keep the two the same."""
    prev = close.shift(1)
    ranges = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], keys=range(3)).groupby(level=1).max()
    return (ranges / close).rolling(days, min_periods=days).mean().shift(1)


def downsample(series: pd.Series, points: int) -> pd.Series:
    """At most about `points` points of a price series for a chart, keeping each stretch's lowest and highest close
    (a plain every-nth sample would cut off the very tops and bottoms the chart is about)."""
    s = series.dropna()
    if len(s) <= points:
        return s
    buckets = max(1, points // 2)
    keep = set()
    for chunk in (s.iloc[k:k + -(-len(s) // buckets)] for k in range(0, len(s), -(-len(s) // buckets))):
        keep.update((chunk.idxmin(), chunk.idxmax()))
    return s.loc[sorted(keep)]


def iso(t) -> str:
    """A check's moment as text: the full time for intraday bars (New York, with its offset), the date for daily ones."""
    t = pd.Timestamp(t)
    return t.isoformat() if t.tzinfo is not None else t.date().isoformat()


def uptrend(daily: pd.DataFrame) -> pd.DataFrame:
    """Per day and symbol: was it in a long-term uptrend as of the PREVIOUS close (50-day average above the 200-day)?
    False while there are fewer than 200 days."""
    trend = daily.rolling(50, min_periods=50).mean() > daily.rolling(200, min_periods=200).mean()
    return trend.shift(1, fill_value=False).astype(bool)


class DipEngine:
    """Buy the dip across several symbols, checking all of them every `interval` (the backtest of the live dip bot).

    At each check (the close of each bar of the interval; a session's last bar only marks the day's equity, since a
    live bot can't trade at 16:00), first the exits, then the buys:
      - Held and at or below its stop (stop_pct under the buy): sell, and blacklist the symbol (no more buys until
        re-enabled: never in a backtest unless reenable_days is set).
      - Held and at or above its target (the reference price it fell from, or rise_pct above the buy): sell.
      - Held for max_hold_days trading days (when set): sell.
      - Not held, not blacklisted, not sold today, and at least drop_pct below its reference (see `references`; with
        drop_mode "price" or "volatility" each symbol's own fall, see DipConfig.buy_fall):
        buy, deepest fall first, while slots are free. One slot = 1/max_positions of the equity (whole shares, or with
        `fractional` fractions of one, $1 or more; never more than the cash). With news on, bearish news blocks the buy for the rest of that day; with the trend
        filter, only symbols whose 50-day average is above their 200-day one are bought.
      - With `rebound` (on by default) a fall into the buy zone isn't bought yet: it waits, remembering the reference
        it fell from and its lowest close since, and is bought once the price is rebound_pct above that low (bearish
        turned bullish) while still under the reference. Back at the reference first = that dip is over.
    Every round trip carries what a chart needs: where the fall started (the window's high, or its start), the lowest
    close between there and the sale, the buy and the sale, each with its moment.
    Every fill pays slippage and the fee. Equity is marked at each day's last close. The benchmark: all the symbols
    bought in equal parts at the first check and held."""

    name = "dip"

    async def run(self, cfg: DipConfig, closes: pd.DataFrame, emit: EmitFn, daily: pd.DataFrame | None = None,
                  news_fn: NewsFn | None = None, moves: pd.DataFrame | None = None) -> dict:
        """`daily`: daily closes for the trend filter (intraday runs). `moves`: drop_mode "volatility"'s daily moves
        (see `daily_moves`, indexed by day): without them nothing is bought."""
        closes = closes.sort_index().ffill()
        intraday = cfg.interval != "1d"
        refs = references(closes, cfg)
        day_of = pd.Series([pd.Timestamp(t).date() for t in closes.index], index=closes.index)
        in_window = (day_of >= cfg.start) & (day_of <= cfg.end)
        rows, ref_rows, row_days = closes[in_window.values], refs[in_window.values], day_of[in_window.values]
        days = sorted(set(row_days))
        if len(days) < 2:
            raise ValueError(f"No prices for these symbols in {cfg.start}..{cfg.end}")
        # The trend filter looks at daily closes: the intraday runs bring them along (`daily`, indexed by day)
        trend = uptrend(daily if daily is not None else closes) if cfg.trend_filter else None

        first = rows.iloc[0]
        bh_symbols = [s for s in rows.columns if pd.notna(first[s])]
        if not bh_symbols:
            raise ValueError("None of the symbols has a price at the start")
        bh_shares = {s: cfg.initial_cash / len(bh_symbols) / float(first[s]) for s in bh_symbols}

        cash = cfg.initial_cash
        held: dict[str, dict] = {}
        armed: dict[str, dict] = {}  # rebound: falls waiting for their turn, symbol -> {ref, peak, low, low_t, turned, zone}
        blacklisted: dict[str, int] = {}  # symbol -> the day index it was blacklisted on
        window = cfg.window_bars()
        trades: list[dict] = []
        closed: list[dict] = []
        events: list[dict] = []
        curve: list[dict] = []
        stats = {"dips": 0, "bought": 0, "too_small": 0, "news_vetoes": 0, "rebounds": 0}
        # Dips not bought, as (symbol, day) pairs: a fall that lasts all day would otherwise count at every check
        skipped: dict[str, set] = {"no_slot": set(), "trend_skips": set(), "blacklisted_skips": set()}
        no_news = 0
        peak, paused_on = cfg.initial_cash, None

        def note(t, sym: str, kind: str, text: str) -> None:
            if len(events) < MAX_EVENTS:
                events.append({"date": t.date().isoformat(), "time": t.strftime("%H:%M") if intraday else "", "symbol": sym,
                               "kind": kind, "text": text})

        def peak_of(sym: str, t) -> tuple:
            """Where the fall measured at check t started: the highest close of the window before t, or its first one."""
            pos = closes.index.get_loc(t)
            past = closes[sym].iloc[max(0, pos - window):pos].dropna()
            if past.empty:
                return t, float(closes[sym].loc[t])
            at = past.idxmax() if cfg.drop_from == "high" else past.index[0]
            return at, float(past.loc[at])

        def low_between(sym: str, start, end) -> tuple:
            seg = closes[sym].loc[start:end].dropna()
            at = seg.idxmin()
            return at, float(seg.loc[at])

        def sell(sym: str, price: float, t, i: int, reason: str) -> None:
            nonlocal cash
            p = held.pop(sym)
            fill = price * (1 - cfg.slippage_pct)
            fee = p["shares"] * fill * cfg.fee_pct
            pnl = p["shares"] * fill - fee - p["cost"]
            cash += p["shares"] * fill - fee
            low_t, low = low_between(sym, p["peak_t"], t)
            leg = {"side": "SELL", "symbol": sym, "date": t.date().isoformat(), "time": t.strftime("%H:%M") if intraday else "",
                   "price": round(fill, 4), "shares": p["shares"], "fee": round(fee, 2), "pnl": round(pnl, 2),
                   "pnl_pct": round(pnl / p["cost"] * 100, 2), "hold_days": i - p["entry_i"], "reason": reason,
                   "entry": p["entry"], "target": p["target"], "stop": p["stop"],
                   # for the chart: the fall's start, its lowest close up to the sale, the buy and the sale
                   "t": iso(t), "buy_t": iso(p["buy_t"]), "peak_t": iso(p["peak_t"]), "peak_price": round(p["peak"], 4),
                   "low_t": iso(low_t), "low_price": round(low, 4)}
            trades.append(leg)
            closed.append(leg)

        def buy(sym: str, price: float, ref: float, drop: float, t, i: int, equity: float, peak: tuple, low: tuple | None,
                zone: float) -> bool:
            nonlocal cash
            fill = price * (1 + cfg.slippage_pct)
            budget = min(equity / cfg.max_positions, cash)
            shares = affordable(budget, fill * (1 + cfg.fee_pct), cfg.fractional)  # same rule as the bot
            if shares <= 0:
                stats["too_small"] += 1
                return False
            fee = shares * fill * cfg.fee_pct
            cash -= shares * fill + fee
            target = round(ref, 2) if cfg.target_mode == "reference" else round(fill * (1 + cfg.rise_pct), 2)
            held[sym] = {"shares": shares, "cost": shares * fill + fee, "entry": round(fill, 4), "ref": round(ref, 2),
                         "target": target, "stop": round(fill * (1 - cfg.stop_pct), 2), "entry_i": i,
                         "buy_t": t, "peak_t": peak[0], "peak": peak[1]}
            leg = {"side": "BUY", "symbol": sym, "date": t.date().isoformat(), "time": t.strftime("%H:%M") if intraday else "",
                   "price": round(fill, 4), "shares": shares, "fee": round(fee, 2), "drop_pct": round(-drop * 100, 2),
                   "reference": round(ref, 2), "target": target, "stop": held[sym]["stop"], "t": iso(t),
                   "zone_pct": round(zone * 100, 2),  # the fall that put it in the buy zone (its own with drop_mode)
                   "peak_t": iso(peak[0]), "peak_price": round(peak[1], 4)}
            if low is not None:  # waited for the turn: the low it turned up from
                leg |= {"low_t": iso(low[0]), "low_price": round(low[1], 4), "rebound_pct": round((price / low[1] - 1) * 100, 2)}
            trades.append(leg)
            stats["bought"] += 1
            return True

        await emit({"type": "start", "symbol": f"{len(cfg.symbols)} symbols", "total": len(days), "initial_cash": cfg.initial_cash,
                    "first_price": cfg.initial_cash})
        zone: set[str] = set()  # symbols in the buy zone at the previous check: a new dip is one that just entered it
        by_day = rows.groupby(row_days.values)
        for i, day in enumerate(days):
            day_rows = by_day.get_group(day)
            sold_today: set[str] = set()
            vetoed_today: set[str] = set()
            if cfg.reenable_days:
                for sym in [s for s, d in blacklisted.items() if i - d >= cfg.reenable_days]:
                    del blacklisted[sym]
                    note(day_rows.index[0], sym, "reenabled", f"{sym} re-enabled after {cfg.reenable_days} trading days")
            trend_today = None
            if trend is not None:  # daily rows, each from the closes before that day
                past = trend.loc[:pd.Timestamp(day)]
                trend_today = past.iloc[-1] if len(past) else pd.Series(False, index=trend.columns)
            moves_today = None
            if cfg.drop_mode == "volatility" and moves is not None:  # daily rows, each from the days before
                past = moves.loc[:pd.Timestamp(day)]
                moves_today = past.iloc[-1] if len(past) else None
            # A session's last bar closes at the bell: it only marks the day's equity (a live bot can't trade at 16:00)
            checks = day_rows.index[:-1] if intraday else day_rows.index
            for t in checks:
                px, ref = day_rows.loc[t], ref_rows.loc[t]
                for sym in list(held):
                    price = px.get(sym)
                    if pd.isna(price):
                        continue
                    price, p = float(price), held[sym]
                    if price <= p["stop"]:
                        sell(sym, price, t, i, "stop-loss")
                        blacklisted[sym] = i
                        note(t, sym, "blacklisted", f"{sym} hit its stop ${p['stop']:.2f} at ${price:.2f}: sold and blacklisted")
                    elif price >= p["target"]:
                        sell(sym, price, t, i, "target")
                    elif cfg.max_hold_days and i - p["entry_i"] >= cfg.max_hold_days:
                        sell(sym, price, t, i, "time")
                    else:
                        continue
                    sold_today.add(sym)
                drops = {s: 1 - float(px[s]) / float(ref[s]) for s in rows.columns
                         if pd.notna(px.get(s)) and pd.notna(ref.get(s)) and float(ref[s]) > 0}
                need = {s: cfg.buy_fall(float(ref[s]), None if moves_today is None else float(moves_today.get(s, math.nan)))
                        for s in drops}
                now_zone = {s for s, d in drops.items() if need[s] is not None and d >= need[s] - 1e-12}
                stats["dips"] += len(now_zone - zone)
                zone = now_zone
                if cfg.rebound:
                    # Falls already waiting: follow their low; back at their reference first = that dip is over
                    for sym in list(armed):
                        a, p_ = armed[sym], px.get(sym)
                        if pd.isna(p_):
                            continue
                        if float(p_) < a["low"]:
                            a["low"], a["low_t"] = float(p_), t
                        if float(p_) >= a["ref"]:
                            del armed[sym]
                    for sym in now_zone - set(armed) - set(held) - set(blacklisted):  # new falls start waiting
                        armed[sym] = {"ref": float(ref[sym]), "peak": peak_of(sym, t), "low": float(px[sym]), "low_t": t,
                                      "turned": False, "zone": need[sym]}
                    turned = {}
                    for sym, a in armed.items():
                        p_ = px.get(sym)
                        if pd.notna(p_) and float(p_) >= a["low"] * (1 + cfg.rebound_pct) - 1e-9 and float(p_) < a["ref"]:
                            turned[sym] = 1 - float(p_) / a["ref"]
                            if not a["turned"]:
                                a["turned"] = True
                                stats["rebounds"] += 1
                    candidates = {sym: (armed[sym]["ref"], d) for sym, d in turned.items()}
                else:
                    candidates = {sym: (float(ref[sym]), drops[sym]) for sym in now_zone}
                if paused_on is not None:
                    continue
                equity = cash + sum(p["shares"] * float(px[s]) for s, p in held.items() if pd.notna(px.get(s)))
                for sym in sorted(candidates, key=lambda s: -candidates[s][1]):
                    if sym in held or sym in sold_today or sym in vetoed_today:
                        continue
                    if sym in blacklisted:
                        skipped["blacklisted_skips"].add((sym, day))
                        continue
                    if len(held) >= cfg.max_positions:
                        skipped["no_slot"].add((sym, day))
                        continue
                    if trend_today is not None and not bool(trend_today.get(sym, False)):
                        skipped["trend_skips"].add((sym, day))
                        continue
                    if cfg.news and news_fn is not None:
                        verdict = await news_fn(sym, day, t.to_pydatetime() if intraday else None)
                        if verdict.get("sentiment") == "bearish":
                            vetoed_today.add(sym)
                            stats["news_vetoes"] += 1
                            note(t, sym, "news", f"{sym} fell {candidates[sym][1]:.1%} but the news is bearish: not bought today")
                            continue
                        if verdict.get("error") or verdict.get("sentiment") in (None, "unavailable"):
                            no_news += 1
                    a = armed.get(sym)
                    peak_at = a["peak"] if a else peak_of(sym, t)
                    low_at = (a["low_t"], a["low"]) if a else None
                    if buy(sym, float(px[sym]), candidates[sym][0], candidates[sym][1], t, i, equity, peak_at, low_at,
                           a["zone"] if a else need[sym]):
                        armed.pop(sym, None)
            last = day_rows.iloc[-1]
            equity = float(cash + sum(p["shares"] * float(last[s]) for s, p in held.items()))
            bh = float(sum(n * float(last[s]) for s, n in bh_shares.items() if pd.notna(last[s])))
            peak = max(peak, equity)
            if paused_on is None and cfg.max_drawdown_pct and equity <= peak * (1 - cfg.max_drawdown_pct):
                paused_on = day
                note(day_rows.index[-1], "", "breaker", f"Equity ${equity:,.0f} is {cfg.max_drawdown_pct:.0%} below its peak: no more buys")
            curve.append({"date": day.isoformat(), "equity": equity, "price": bh, "position": len(held)})
            await emit({"type": "step", "i": i + 1, "total": len(days), "date": day.isoformat(), "action": "hold", "confidence": 0.0,
                        "price": bh, "cash": cash, "position": len(held), "equity": equity, "sentiment": "", "reasoning": "",
                        "decisions": []})

        last = rows.iloc[-1]
        prices = pd.Series([pt["price"] for pt in curve], index=days)  # the benchmark's value: all symbols held equally
        result = SimplePortfolioEngine()._metrics(cfg, prices, curve[-1]["equity"], 0.0, 0.0, trades, curve, closed=closed)
        open_positions = [{"symbol": s, "shares": p["shares"], "entry": p["entry"], "target": p["target"], "stop": p["stop"],
                           "price": round(float(last[s]), 2), "unrealized_pnl": round(p["shares"] * float(last[s]) - p["cost"], 2),
                           "buy_t": iso(p["buy_t"]), "peak_t": iso(p["peak_t"]), "peak_price": round(p["peak"], 4)}
                          for s, p in held.items()]
        result.update({
            "open_position": len(open_positions),
            "open_positions": open_positions,
            "unrealized_pnl": round(sum(p["unrealized_pnl"] for p in open_positions), 2),
            "time_exits": sum(1 for t in closed if t["reason"] == "time"),
            "by_symbol": _by_symbol(cfg.symbols, closed, blacklisted, held),
            "blacklisted_at_end": sorted(blacklisted),
            "events": events,
            "dip_stats": stats | {k: len(v) for k, v in skipped.items()},
            "benchmark_symbols": bh_symbols,
            "missing_symbols": [s for s in cfg.symbols if s not in closes.columns or closes[s].isna().all()],
            "signals": {"BUY": stats["bought"], "SELL": len(closed), "HOLD": 0},
            "decision_errors": 0,
            "no_news_decisions": no_news,
            "breaker_tripped_on": paused_on.isoformat() if paused_on else None,
        })
        result["verdict"] = judge(result, cfg)
        await emit({"type": "done", "result": result})
        return result


def _by_symbol(symbols: list[str], closed: list[dict], blacklisted: dict[str, int], held: dict[str, dict]) -> list[dict]:
    """Each symbol's round trips: how often it was bought, won, what it made, and where it stands at the end."""
    out = []
    for s in symbols:
        mine = [t for t in closed if t["symbol"] == s]
        wins = sum(1 for t in mine if t["pnl"] > 0)
        out.append({"symbol": s, "trades": len(mine), "wins": wins, "pnl": round(sum(t["pnl"] for t in mine), 2),
                    "stops": sum(1 for t in mine if t["reason"] == "stop-loss"),
                    "status": "blacklisted" if s in blacklisted else "held" if s in held else "watching"})
    return out
