from datetime import date

import pandas as pd

from ..models import RotationConfig
from ..verdict import judge
from .base import EmitFn
from .dip import MIN_FRACTIONAL_ORDER, affordable
from .simple import SimplePortfolioEngine

MIN_TRADE_OF_SLOT = 0.02  # a pick that stays is only trimmed or topped up by at least this much of its slot (the bot's rule)


def momentum(closes: pd.DataFrame, day: date, lookback_months: int, skip_months: int) -> pd.Series:
    """Each symbol's return from (lookback + skip) months before `day` to `skip` months before it, using only prices
    up to then (NaN without prices at both ends, or none on `day` itself). `closes` is forward-filled."""
    t = pd.Timestamp(day)
    upto = closes.loc[:t]
    if upto.empty:
        return pd.Series(dtype=float)
    recent = upto.loc[:t - pd.DateOffset(months=skip_months)]
    past = upto.loc[:t - pd.DateOffset(months=lookback_months + skip_months)]
    if recent.empty or past.empty:
        return pd.Series(float("nan"), index=closes.columns)
    mom = recent.iloc[-1] / past.iloc[-1] - 1
    return mom.where(upto.iloc[-1].notna())


class RotationEngine:
    """A portfolio of several symbols, rotated by momentum.

    Rules:
      - On the window's first day and on each month's last trading day (not the window's last day), rank the
        universe by momentum (see `momentum`) and pick the top `top_n`; with `abs_filter`, only symbols that rose.
      - Hold each pick at 1/top_n of the account (slots without a pick stay in cash): sell what dropped out, trim
        or top up what stays, buy what came in, all at that day's close with slippage and fees. In whole shares, or
        with `fractional` fractions of one ($1 or more); a pick that stays is only trimmed or topped up when the
        change is worth MIN_TRADE_OF_SLOT of its slot ($1 at least). The bot's sizing (trading-service/app/rotation.py):
        keep the two the same.
      - A round trip is a symbol's whole stay in the portfolio: its P&L includes the trims and top-ups on the way.
      - The benchmark: the whole universe bought in equal parts on the first day and held (symbols without a
        price yet on that day can't be part of it).
    Results go through the daily metrics (the benchmark's value stands in for the price) and the same recommendation."""

    name = "rotation"

    async def run(self, cfg: RotationConfig, closes: pd.DataFrame, emit: EmitFn) -> dict:
        closes = closes.sort_index().ffill()
        closes.index = pd.DatetimeIndex(closes.index)
        window = closes.loc[pd.Timestamp(cfg.start):pd.Timestamp(cfg.end)]
        days = [t.date() for t in window.index]
        if len(days) < 2:
            raise ValueError(f"No prices for the universe in {cfg.start}..{cfg.end}")
        months = pd.Series(window.index, index=window.index).groupby([window.index.year, window.index.month]).max()
        rebalance_days = ({days[0]} | {t.date() for t in months}) - {days[-1]}

        first = window.iloc[0]
        bh_symbols = [s for s in window.columns if pd.notna(first[s])]
        bh_shares = {s: cfg.initial_cash / len(bh_symbols) / float(first[s]) for s in bh_symbols}

        cash = cfg.initial_cash
        held: dict[str, dict] = {}  # symbol -> shares, cost (open cost basis), invested, realized, entry_i
        trades: list[dict] = []
        closed: list[dict] = []
        curve: list[dict] = []
        rebalances: list[dict] = []
        peak, paused_on = cfg.initial_cash, None

        def sell(sym: str, qty: float, price: float, day: date, i: int, reason: str | None) -> None:
            nonlocal cash
            p = held[sym]
            fill = price * (1 - cfg.slippage_pct)
            fee = qty * fill * cfg.fee_pct
            part = p["cost"] * qty / p["shares"]
            p["realized"] += qty * fill - fee - part
            p["cost"] -= part
            p["shares"] -= qty
            cash += qty * fill - fee
            leg = {"side": "SELL", "symbol": sym, "date": day.isoformat(), "price": round(fill, 4), "shares": round(qty, 4), "fee": round(fee, 2)}
            if reason:  # the whole position is gone: the round trip closes
                pnl = p["realized"]
                leg |= {"pnl": round(pnl, 2), "pnl_pct": round(pnl / p["invested"] * 100, 2), "hold_days": i - p["entry_i"], "reason": reason}
                closed.append(leg)
                del held[sym]
            trades.append(leg)

        def buy(sym: str, qty: float, price: float, day: date, i: int) -> None:
            nonlocal cash
            fill = price * (1 + cfg.slippage_pct)
            qty = min(qty, affordable(cash, fill * (1 + cfg.fee_pct), cfg.fractional))  # never more than the cash
            if qty <= 1e-9:
                return
            fee = qty * fill * cfg.fee_pct
            p = held.setdefault(sym, {"shares": 0.0, "cost": 0.0, "invested": 0.0, "realized": 0.0, "entry_i": i})
            p["shares"] += qty
            p["cost"] += qty * fill + fee
            p["invested"] += qty * fill + fee
            cash -= qty * fill + fee
            trades.append({"side": "BUY", "symbol": sym, "date": day.isoformat(), "price": round(fill, 4), "shares": round(qty, 4), "fee": round(fee, 2)})

        def target(sym: str, slot: float, price: float) -> float:
            """The shares of a pick to hold: what a slot buys with slippage and fees. One already held is left alone
            when the change is too small to be worth its slippage."""
            want = affordable(slot, price * (1 + cfg.slippage_pct) * (1 + cfg.fee_pct), cfg.fractional)
            have = held[sym]["shares"] if sym in held else 0.0
            if have and abs(want - have) * price < max(MIN_TRADE_OF_SLOT * slot, MIN_FRACTIONAL_ORDER):
                return have
            return want

        await emit({"type": "start", "symbol": f"{len(cfg.symbols)} symbols", "total": len(days), "initial_cash": cfg.initial_cash,
                    "first_price": cfg.initial_cash})
        for i, (t, row) in enumerate(window.iterrows()):
            day = t.date()
            px = {s: float(v) for s, v in row.items()}  # plain floats: numpy values don't serialize to JSON
            decisions = []
            if day in rebalance_days and paused_on is None:
                mom = momentum(closes, day, cfg.lookback_months, cfg.skip_months).dropna().sort_values(ascending=False)
                picks = [s for s in mom.index if not cfg.abs_filter or mom[s] > 0][:cfg.top_n]
                before = set(held)
                for sym in [s for s in held if s not in picks]:
                    sell(sym, held[sym]["shares"], px[sym], day, i, "rotated out")
                equity = cash + sum(p["shares"] * px[s] for s, p in held.items())
                slot = equity / cfg.top_n
                want = {s: target(s, slot, px[s]) for s in picks}
                too_small = [s for s in picks if not want[s]]  # whole shares: a slot under one share's price
                for sym in picks:  # trims first, so their cash pays for the top-ups and new buys
                    if sym in held and held[sym]["shares"] > want[sym]:
                        if want[sym]:
                            sell(sym, held[sym]["shares"] - want[sym], px[sym], day, i, None)
                        else:
                            sell(sym, held[sym]["shares"], px[sym], day, i, "slot under one share")
                for sym in picks:
                    have = held[sym]["shares"] if sym in held else 0.0
                    if want[sym] > have:
                        buy(sym, want[sym] - have, px[sym], day, i)
                top = [{"symbol": s, "momentum_pct": round(float(mom[s]) * 100, 1)} for s in mom.index[:max(cfg.top_n + 3, 6)]]
                kept = [s for s in picks if s in held]
                rebalances.append({"date": day.isoformat(), "held": kept, "ranking": top})
                bought, sold = sorted(set(held) - before), sorted(before - set(held))
                small = ", ".join(too_small)
                text = (f"Held: {', '.join(f'{s} {mom[s] * 100:+.1f}%' for s in kept) or 'nothing (no symbol rose)'}"
                        f"{'; bought ' + ', '.join(bought) if bought else ''}{'; sold ' + ', '.join(sold) if sold else ''}"
                        f"{f'; {cfg.top_n - len(picks)} slot(s) in cash' if len(picks) < cfg.top_n else ''}"
                        f"{f'; a slot (${slot:,.0f}) is less than one share of {small}: that money stays in cash' if small else ''}")
                decisions.append({"slot": "close", "action": "BUY" if bought else "HOLD", "confidence": 1.0, "price": 0.0,
                                  "equity": equity, "sentiment": "", "reasoning": text})
            equity = float(cash + sum(p["shares"] * px[s] for s, p in held.items()))
            bh = float(sum(n * px[s] for s, n in bh_shares.items()))
            peak = max(peak, equity)
            if paused_on is None and cfg.max_drawdown_pct and equity <= peak * (1 - cfg.max_drawdown_pct):
                paused_on = day
            curve.append({"date": day.isoformat(), "equity": equity, "price": bh, "position": len(held)})
            await emit({"type": "step", "i": i + 1, "total": len(days), "date": day.isoformat(), "action": "hold", "confidence": 0.0,
                        "price": bh, "cash": cash, "position": len(held), "equity": equity, "sentiment": "",
                        "reasoning": decisions[0]["reasoning"] if decisions else "", "decisions": decisions})

        prices = pd.Series([pt["price"] for pt in curve], index=days)  # the benchmark's value: buy & hold = the universe held
        result = SimplePortfolioEngine()._metrics(cfg, prices, curve[-1]["equity"], 0.0, 0.0, trades, curve, closed=closed)
        result.update({"rebalances": rebalances, "held_at_end": sorted(held), "benchmark_symbols": bh_symbols,
                       "missing_symbols": [s for s in cfg.symbols if s not in closes.columns or closes[s].isna().all()],
                       "signals": {"BUY": 0, "SELL": 0, "HOLD": len(rebalances)}, "decision_errors": 0, "no_news_decisions": 0,
                       "breaker_tripped_on": paused_on.isoformat() if paused_on else None})
        result["verdict"] = judge(result, cfg)
        await emit({"type": "done", "result": result})
        return result
