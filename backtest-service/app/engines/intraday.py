import math
from datetime import date

import pandas as pd

from ..models import RunConfig
from ..verdict import judge
from .base import EmitFn
from .simple import SimplePortfolioEngine

NOTIONAL_DAY_MINUTES = 390  # a regular session, for showing a hold in days like the daily engine


class IntradayEngine:
    """Replays an intraday strategy's daily order plans (decision-service /intraday/plans) on 5-minute bars.

    Rules, per day with a plan:
      - The order can fill from the bar that starts when the plan was placed (the setup's last bar has closed).
        A market order fills at that bar's open; a limit order when a bar trades through its price (at the bar's
        open if it gapped past it, the better price). Unfilled by `valid_until`, or the price reaches the target
        first, and the order is cancelled.
      - Size: risk `risk_pct` of equity between the fill and the stop, capped by the cash (no leverage).
      - Then bar by bar: the stop and the target, gaps through either filling at the bar's open; when a bar
        touches both, the stop (bars can't tell which came first: the cautious answer). On the fill bar itself only
        the stop counts, for the same reason. Whatever is still open is closed at `exit_by` (15:55).
      - Slippage on market fills (entries, stops, the 15:55 exit), none on limit fills (limit entries, targets);
        fees on both sides. Like the bots' breaker, no new trades once equity fell max_drawdown_pct below its peak.
    Results use the daily engine's metrics (end-of-day equity), so the recommendation reads them the same way."""

    name = "intraday"

    async def run(self, cfg: RunConfig, bars: pd.DataFrame, plans: list[dict], emit: EmitFn) -> dict:
        by_day = {d: g.sort_index() for d, g in bars.groupby(bars.index.date) if cfg.start <= d <= cfg.end}
        days: list[date] = sorted(by_day)
        if len(days) < 2:
            raise ValueError(f"No 5-minute prices for {cfg.symbol} in {cfg.start}..{cfg.end}")
        plan_for = {p["date"]: p for p in plans}
        equity = peak = cfg.initial_cash
        paused_on: date | None = None
        trades: list[dict] = []
        closed: list[dict] = []
        curve: list[dict] = []
        signals = {"BUY": 0, "SELL": 0, "HOLD": 0}  # long setups / short setups / days without one
        unfilled = 0

        await emit({"type": "start", "symbol": cfg.symbol, "total": len(days), "initial_cash": cfg.initial_cash,
                    "first_price": float(by_day[days[0]]["Close"].iloc[-1])})
        for i, day in enumerate(days):
            g = by_day[day]
            close = float(g["Close"].iloc[-1])
            plan = plan_for.get(day.isoformat()) if paused_on is None else None
            action, confidence, shares, reasoning = "hold", 0.0, 0, "No setup today"
            if paused_on is not None:
                reasoning = f"Paused: equity fell {cfg.max_drawdown_pct:.0%} below its peak on {paused_on}"
            if plan:
                signals["BUY" if plan["side"] == "long" else "SELL"] += 1
                done = self._trade(cfg, g, plan, equity)
                if "skip" in done:
                    unfilled += 1
                    action, confidence, reasoning = "HOLD", 1.0, f"{plan['note']}. Not traded: {done['skip']}"
                else:
                    entry_leg, exit_leg = done["entry"], done["exit"]
                    equity += exit_leg["pnl"]
                    trades += [entry_leg, exit_leg]
                    closed.append(exit_leg)
                    for leg in (entry_leg, exit_leg):
                        await emit({"type": "trade", **leg})
                    action, confidence, shares = entry_leg["side"], 1.0, entry_leg["shares"]
                    reasoning = (f"{plan['note']}. Filled {entry_leg['price']:.2f} at {entry_leg['date'][11:]}, out "
                                 f"{exit_leg['price']:.2f} at {exit_leg['date'][11:]} ({exit_leg['reason']}): "
                                 f"{'+' if exit_leg['pnl'] >= 0 else '-'}${abs(exit_leg['pnl']):,.2f}")
            else:
                signals["HOLD"] += 1
            peak = max(peak, equity)
            if paused_on is None and cfg.max_drawdown_pct and equity <= peak * (1 - cfg.max_drawdown_pct):
                paused_on = day
            curve.append({"date": day.isoformat(), "equity": equity, "price": close, "position": shares})
            # A day with a setup goes in the activity log (filled or not, and why); the rest only feed the chart
            decisions = [{"slot": "open", "action": action, "confidence": confidence, "price": close, "equity": equity,
                          "sentiment": "", "reasoning": reasoning}] if plan else []
            await emit({"type": "step", "i": i + 1, "total": len(days), "date": day.isoformat(), "action": action,
                        "confidence": confidence, "price": close, "cash": equity, "position": 0, "equity": equity,
                        "sentiment": "", "reasoning": reasoning, "decisions": decisions})

        prices = pd.Series([pt["price"] for pt in curve], index=days)
        result = SimplePortfolioEngine()._metrics(cfg, prices, equity, 0.0, 0.0, trades, curve, closed=closed)
        result.update({"signals": signals, "setups": signals["BUY"] + signals["SELL"], "unfilled": unfilled,
                       "decision_errors": 0, "no_news_decisions": 0,
                       "breaker_tripped_on": paused_on.isoformat() if paused_on else None})
        result["verdict"] = judge(result, cfg)
        await emit({"type": "done", "result": result})
        return result

    def _trade(self, cfg: RunConfig, g: pd.DataFrame, plan: dict, equity: float) -> dict:
        """One day's plan played out: {"entry": leg, "exit": leg}, or {"skip": why it didn't trade}."""
        long = plan["side"] == "long"
        sgn = 1 if long else -1
        placed, valid, exit_by = (pd.Timestamp(plan[k]) for k in ("placed_at", "valid_until", "exit_by"))
        slip = cfg.slippage_pct
        fill = fill_t = stop = target = None
        shares = 0

        def leg_out(t, price: float, reason: str, market: bool) -> dict:
            price = price * (1 - sgn * slip) if market else price  # a market exit fills a touch worse
            fee_in, fee_out = fill * shares * cfg.fee_pct, price * shares * cfg.fee_pct
            pnl = sgn * (price - fill) * shares - fee_in - fee_out
            minutes = max(0, int((t - fill_t).total_seconds() // 60))
            return {
                "entry": {"side": "BUY" if long else "SELL", "date": fill_t.strftime("%Y-%m-%d %H:%M"), "price": round(fill, 4),
                          "shares": shares, "fee": round(fee_in, 2)},
                "exit": {"side": "SELL" if long else "BUY", "date": t.strftime("%Y-%m-%d %H:%M"), "price": round(price, 4),
                         "shares": shares, "fee": round(fee_out, 2), "pnl": round(pnl, 2),
                         "pnl_pct": round(pnl / (fill * shares) * 100, 2), "hold_minutes": minutes,
                         "hold_days": round(minutes / NOTIONAL_DAY_MINUTES, 2), "reason": reason, "direction": plan["side"]},
            }

        for t, b in g[g.index >= placed].iterrows():
            o, h, lo = float(b["Open"]), float(b["High"]), float(b["Low"])
            if fill is None:
                if t >= valid:
                    return {"skip": f"not filled by {valid:%H:%M}, cancelled"}
                if plan["order"] == "market":
                    fill = o * (1 + sgn * slip)
                else:
                    entry, target = plan["entry"], plan["target"]
                    if (lo <= entry) if long else (h >= entry):
                        fill = min(o, entry) if long else max(o, entry)  # gapped past the limit: the open, a better price
                    elif target is not None and ((h >= target) if long else (lo <= target)):
                        return {"skip": "the price reached the target before the entry, cancelled"}
                if fill is None:
                    continue
                stop = plan["stop"]
                target = plan["target"] if plan["target"] is not None else fill + sgn * plan["target_r"] * abs(fill - stop)
                risk = sgn * (fill - stop)
                if risk <= 0:
                    return {"skip": "it opened beyond the stop"}
                shares = math.floor(min(equity * cfg.risk_pct / risk, equity * cfg.position_pct / fill))
                if shares < 1:
                    return {"skip": "too small for the account"}
                fill_t = t
                if (lo <= stop) if long else (h >= stop):  # the fill bar: only the stop counts
                    return leg_out(t, stop, "stop-loss", market=True)
                continue
            if t >= exit_by:
                return leg_out(t, o, "close", market=True)
            if (o <= stop) if long else (o >= stop):
                return leg_out(t, o, "stop-loss", market=True)
            if (o >= target) if long else (o <= target):
                return leg_out(t, o, "target", market=False)
            if (lo <= stop) if long else (h >= stop):
                return leg_out(t, stop, "stop-loss", market=True)
            if (h >= target) if long else (lo <= target):
                return leg_out(t, target, "target", market=False)
        if fill is None:
            return {"skip": "never reached the entry"}
        last_t = g.index[-1]  # a half day ends before 15:55: out at the last bar's close
        return leg_out(last_t, float(g["Close"].iloc[-1]), "close", market=True)
