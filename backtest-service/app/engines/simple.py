import math
from datetime import date

import pandas as pd

from ..models import RunConfig
from .base import BacktestEngine, DecideFn, EmitFn


class SimplePortfolioEngine(BacktestEngine):
    """A small, honest long/flat portfolio simulator.

    Rules:
      - Every `rebalance_days` trading days we ask decision-service for a signal, at the day's slot(s) like a
        trading bot's "Check at": before the close (on the close), after the open (at 10:00, on the 10:00
        price), or both.
      - BUY  (confidence >= min_confidence) and currently flat  -> buy with position_pct of cash.
      - SELL (confidence >= min_confidence) and currently long  -> sell the whole position.
      - Otherwise hold.
      - All day while holding: the stop-loss and take-profit are checked against the price range, like the
        live bots (Alpaca holds the stop at the broker; targets are checked every few minutes). A 10:00 entry
        is checked against the rest of that day.
    Signal trades fill at the slot's price; stop/target exits at their level, or at the open when the price
    gaps past it overnight. Slippage and fees apply to every fill. No leverage, no shorting."""

    name = "simple"

    async def run(self, cfg: RunConfig, prices: pd.Series, decide: DecideFn, emit: EmitFn,
                  bars: pd.DataFrame | None = None, open_slots: pd.DataFrame | None = None) -> dict:
        days: list[date] = list(prices.index)
        cash = cfg.initial_cash
        position = 0.0  # shares held
        cost_basis = 0.0  # total cash spent to open the position (fill + buy fee), for honest PnL
        entry_i = 0  # index of the entry day, for the holding period
        stop_price = 0.0  # sell if price falls to/below this (set from the agent's sizing at entry)
        target_price = math.inf  # sell if price rises to/above this
        trades: list[dict] = []
        equity_curve: list[dict] = []
        signals = {"BUY": 0, "SELL": 0, "HOLD": 0}  # what the strategy said, before the confidence filter
        decision_errors = 0  # decisions where decision-service failed (counted as HOLD)
        no_news = 0  # decisions made without news sentiment (news off, or its LLM call failed)

        async def close_position(i, day, price, reason: str) -> None:
            """Sell the whole position at `price` (with slippage + fee) and record why.
            Shared by signal SELLs and the stop-loss/target exits so the accounting is identical."""
            nonlocal cash, position, cost_basis, stop_price, target_price
            fill = price * (1 - cfg.slippage_pct)  # a sell fills a touch BELOW the price (worse)
            proceeds = position * fill
            fee = proceeds * cfg.fee_pct  # commission on the way out too
            net_proceeds = proceeds - fee
            pnl = net_proceeds - cost_basis  # true round-trip profit: net of both fees and both slippages
            cash += net_proceeds
            rec = {"side": "SELL", "date": day.isoformat(), "price": round(fill, 4),
                   "shares": position, "fee": round(fee, 2), "pnl": round(pnl, 2),
                   "pnl_pct": round(pnl / cost_basis * 100, 2), "hold_days": i - entry_i, "reason": reason}
            trades.append(rec)
            await emit({"type": "trade", **rec})
            position = 0.0
            cost_basis = 0.0
            stop_price = 0.0
            target_price = math.inf

        async def risk_check(i, day, open_: float, high: float, low: float, last: float) -> None:
            """Exit on the stop-loss or target if this stretch of the day reached it."""
            if position > 0:
                hit = _risk_exit(open_, high, low, last, stop_price, target_price)
                if hit:
                    await close_position(i, day, hit[1], hit[0])

        async def decide_and_trade(i, day, slot: str, price: float) -> dict:
            """Ask for a signal at this slot and apply the trading rule at `price`. Returns the activity-log record."""
            nonlocal cash, position, cost_basis, entry_i, stop_price, target_price, decision_errors, no_news
            sig = await decide(cfg.symbol, day, slot)
            action = sig.get("action", "HOLD")
            conf = float(sig.get("confidence", 0.0))
            signals[action] = signals.get(action, 0) + 1
            decision_errors += bool(sig.get("error"))
            no_news += not sig.get("error") and sig.get("sentiment") == "unavailable"

            if action == "BUY" and conf >= cfg.min_confidence and position == 0 and cash > 0:
                spend = cash * cfg.position_pct
                fill = price * (1 + cfg.slippage_pct)  # slippage: a buy fills a touch ABOVE the price (worse)
                # Size so the shares plus their fee fit the budget: shares * fill * (1 + fee_pct) <= spend
                shares = math.floor(spend / (fill * (1 + cfg.fee_pct)))  # whole shares only
                if shares > 0:
                    cost = shares * fill
                    fee = cost * cfg.fee_pct  # commission on the trade value
                    cash -= cost + fee
                    position = shares
                    entry_i = i
                    cost_basis = cost + fee  # what it truly cost us to get in
                    # Enforce the stop/target the agent sized for THIS entry (from decision-service).
                    # Falls back to no stop / no target if the signal didn't include a position block.
                    pos = sig.get("position") or {}
                    stop_price = float(pos.get("stop_loss") or 0.0)
                    target_price = float(pos.get("target") or math.inf)
                    rec = {"side": "BUY", "date": day.isoformat(), "price": round(fill, 4), "shares": shares, "fee": round(fee, 2)}
                    trades.append(rec)
                    await emit({"type": "trade", **rec})
            elif action == "SELL" and conf >= cfg.min_confidence and position > 0:
                await close_position(i, day, price, "signal")
            return {"slot": slot, "action": action, "confidence": conf, "price": price, "equity": cash + position * price,
                    "sentiment": sig.get("sentiment", ""), "reasoning": sig.get("reasoning", "")}

        # Tell monitors the run is starting (frontend uses this to size the progress bar / charts)
        await emit({"type": "start", "symbol": cfg.symbol, "total": len(days),
                    "initial_cash": cfg.initial_cash, "first_price": float(prices.iloc[0])})

        for i, day in enumerate(days):
            price = float(prices.iloc[i])
            # The day's range; without bars (closes only) it's just the close
            o, h, lo = (float(bars.iloc[i][k]) for k in ("Open", "High", "Low")) if bars is not None else (price,) * 3
            # Only spend an LLM call every rebalance_days; other days we simply carry the position.
            is_decision_day = i % cfg.rebalance_days == 0
            decisions: list[dict] = []

            if cfg.decide_at == "close":
                await risk_check(i, day, o, h, lo, price)  # the whole day passes before the close decision
            else:
                slot = open_slots.loc[day] if open_slots is not None and day in open_slots.index else None
                if slot is None:  # no 30-minute prices for this day: the 10:00 decision can't be replayed
                    await risk_check(i, day, o, h, lo, price)
                    if is_decision_day:
                        signals["HOLD"] += 1
                        decision_errors += 1
                        decisions.append({"slot": "open", "action": "HOLD", "confidence": 0.0, "price": price,
                                          "equity": cash + position * price, "sentiment": "",
                                          "reasoning": "No 10:00 price for this day (30-minute data missing): no decision"})
                else:
                    await risk_check(i, day, o, slot["high_before"], slot["low_before"], slot["price"])  # 9:30-10:00
                    if is_decision_day:
                        decisions.append(await decide_and_trade(i, day, "open", float(slot["price"])))
                    # 10:00 to the close: also covers a position bought at 10:00
                    await risk_check(i, day, slot["price"], slot["high_after"], slot["low_after"], price)
            if is_decision_day and cfg.decide_at in ("close", "both"):
                decisions.append(await decide_and_trade(i, day, "close", price))

            # Mark-to-market equity for every day so the chart is smooth
            equity = cash + position * price
            equity_curve.append({"date": day.isoformat(), "equity": equity, "price": price, "position": position})
            last = decisions[-1] if decisions else {}
            await emit({
                "type": "step", "i": i + 1, "total": len(days), "date": day.isoformat(),
                "action": last.get("action", "hold"), "confidence": last.get("confidence", 0.0),
                "price": price, "cash": cash, "position": position, "equity": equity,
                "sentiment": last.get("sentiment", ""), "reasoning": last.get("reasoning", ""),
                "decisions": decisions,  # every decision of the day (two when checking at both times)
            })

        result = self._metrics(cfg, prices, cash, position, cost_basis, trades, equity_curve)
        result["signals"] = signals
        result["decision_errors"] = decision_errors
        result["no_news_decisions"] = no_news
        await emit({"type": "done", "result": result})
        return result

    def _metrics(self, cfg, prices, cash, position, cost_basis, trades, equity_curve) -> dict:
        """Summary statistics the frontend shows in the results panel and the strategy comparison.
        Ratios are annualized from daily closes (252 trading days), with a 0% risk-free rate; on a
        window of a few weeks they are noisy, so read them next to the return and drawdown."""
        first_price = float(prices.iloc[0])
        last_price = float(prices.iloc[-1])
        final_equity = cash + position * last_price
        equity = [pt["equity"] for pt in equity_curve]
        closes = [pt["price"] for pt in equity_curve]

        # Buy & hold benchmark: what if you just bought on day 1 and did nothing?
        buy_hold_return = (last_price / first_price - 1) * 100

        closed = [t for t in trades if t["side"] == "SELL"]
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) <= 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in losses)
        trade_pcts = [t["pnl_pct"] for t in closed]
        strategy_return = (final_equity / cfg.initial_cash - 1) * 100
        max_dd = _max_drawdown(equity)
        # Alpha = how much the strategy beat (or lagged) simply buying and holding the stock.
        # If this is negative, the strategy isn't earning its complexity, fees, or risk.
        alpha = strategy_return - buy_hold_return
        return {
            "final_equity": round(final_equity, 2),
            "total_return_pct": round(strategy_return, 2),
            "buy_hold_return_pct": round(buy_hold_return, 2),
            "alpha_vs_buy_hold_pct": round(alpha, 2),  # strategy return minus buy & hold
            "beat_buy_hold": alpha > 0,  # the bar to clear: did the strategy add value?
            "max_drawdown_pct": max_dd,
            "buy_hold_max_drawdown_pct": _max_drawdown(closes),
            # Risk-adjusted: return per unit of volatility (Sharpe) or of downside volatility only (Sortino)
            **_risk_ratios(equity, prefix=""),
            **_risk_ratios(closes, prefix="buy_hold_"),
            # Return earned per % of worst drawdown; None when the equity never fell
            "return_over_drawdown": round(strategy_return / -max_dd, 2) if max_dd < 0 else None,
            "num_trades": len(closed),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            # Gross profit / gross loss: > 1 makes money. None when there were no losing trades to divide by.
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "avg_trade_pct": _mean(trade_pcts),  # expectancy: the average round trip, net of costs
            "avg_win_pct": _mean([t["pnl_pct"] for t in wins]),
            "avg_loss_pct": _mean([t["pnl_pct"] for t in losses]),
            "best_trade_pct": max(trade_pcts) if trade_pcts else None,
            "worst_trade_pct": min(trade_pcts) if trade_pcts else None,
            "avg_hold_days": _mean([t["hold_days"] for t in closed]),  # trading days per round trip
            # Share of days that ended holding the stock: low exposure = most of the time in cash
            "exposure_pct": round(sum(1 for pt in equity_curve if pt.get("position", 0) > 0) / len(equity_curve) * 100, 1)
            if equity_curve else 0.0,
            "stop_exits": sum(1 for t in closed if t.get("reason") == "stop-loss"),
            "target_exits": sum(1 for t in closed if t.get("reason") == "target"),
            "total_fees": round(sum(t.get("fee", 0) for t in trades), 2),
            # Still holding at the end: its gain/loss is in the return but not in the trade stats above
            "open_position": position,
            "unrealized_pnl": round(position * last_price - cost_basis, 2) if position else 0.0,
            "trades": trades,
            "equity_curve": equity_curve,
        }


TRADING_DAYS = 252


def _risk_exit(open_: float, high: float, low: float, close: float, stop: float, target: float) -> tuple[str, float] | None:
    """Did the day's range hit the stop or the target, and at what price does the exit fill?
    A gap past a level fills at the open (a stop order becomes a market order there). When the day touched both
    levels, daily bars can't tell which came first: assume the stop, the cautious answer."""
    if open_ <= stop:
        return "stop-loss", open_
    if open_ >= target:
        return "target", open_
    if min(low, close) <= stop:
        return "stop-loss", stop
    if max(high, close) >= target:
        return "target", target
    return None


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


def _max_drawdown(values: list[float]) -> float:
    """Largest peak-to-trough drop of a value series, in percent (0 or negative)."""
    peak = -math.inf
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, (v - peak) / peak * 100)
    return round(max_dd, 2)


def _risk_ratios(values: list[float], prefix: str) -> dict:
    """Annualized volatility, Sharpe and Sortino of a daily value series (risk-free rate 0).
    A ratio is None when it can't be computed: too few days, or no (downside) movement at all."""
    rets = [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]
    vol = sharpe = sortino = None
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        downside = math.sqrt(sum(min(r, 0) ** 2 for r in rets) / len(rets))
        vol = round(sd * math.sqrt(TRADING_DAYS) * 100, 2)
        sharpe = round(mean / sd * math.sqrt(TRADING_DAYS), 2) if sd > 1e-12 else None
        sortino = round(mean / downside * math.sqrt(TRADING_DAYS), 2) if downside > 1e-12 else None
    return {f"{prefix}volatility_pct": vol, f"{prefix}sharpe": sharpe, f"{prefix}sortino": sortino}
