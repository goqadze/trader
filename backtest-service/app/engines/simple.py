import math
from datetime import date

import pandas as pd

from ..models import RunConfig
from .base import BacktestEngine, DecideFn, EmitFn


class SimplePortfolioEngine(BacktestEngine):
    """A small, honest long/flat portfolio simulator.

    Rules:
      - Every `rebalance_days` trading days we ask decision-service for a signal.
      - BUY  (confidence >= min_confidence) and currently flat  -> buy with position_pct of cash.
      - SELL (confidence >= min_confidence) and currently long  -> sell the whole position.
      - Otherwise hold.
    All fills happen at that day's closing price. No leverage, no shorting, no fees/slippage
    (add those later for realism)."""

    name = "simple"

    async def run(self, cfg: RunConfig, prices: pd.Series, decide: DecideFn, emit: EmitFn) -> dict:
        days: list[date] = list(prices.index)
        cash = cfg.initial_cash
        position = 0.0  # shares held
        entry_price = 0.0  # price we bought at (includes buy slippage), shown on the trade
        cost_basis = 0.0  # total cash spent to open the position (fill + buy fee), for honest PnL
        entry_i = 0  # index of the entry day, for the holding period
        stop_price = 0.0  # sell if price falls to/below this (set from the agent's sizing at entry)
        target_price = math.inf  # sell if price rises to/above this
        trades: list[dict] = []
        equity_curve: list[dict] = []
        last_signal = {"action": "HOLD", "confidence": 0.0, "reasoning": "", "steps": []}
        signals = {"BUY": 0, "SELL": 0, "HOLD": 0}  # what the strategy said on decision days (before the confidence filter)
        decision_errors = 0  # decision days where decision-service failed (counted as HOLD)
        no_news = 0  # decision days decided without news sentiment (news off, or its LLM call failed)

        async def close_position(i, day, price, reason: str) -> None:
            """Sell the whole position at today's close (with slippage + fee) and record why.
            Shared by signal SELLs and the daily stop-loss/target exits so the accounting is identical."""
            nonlocal cash, position, entry_price, cost_basis, stop_price, target_price
            fill = price * (1 - cfg.slippage_pct)  # a sell fills a touch BELOW the close (worse)
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
            entry_price = 0.0
            cost_basis = 0.0
            stop_price = 0.0
            target_price = math.inf

        # Tell monitors the run is starting (frontend uses this to size the progress bar / charts)
        await emit({"type": "start", "symbol": cfg.symbol, "total": len(days),
                    "initial_cash": cfg.initial_cash, "first_price": float(prices.iloc[0])})

        for i, day in enumerate(days):
            price = float(prices.iloc[i])

            # --- Risk exits run EVERY day, before any new signal, while we hold a position ---
            # We only have daily closes, so a stop/target is checked at each day's close (the honest
            # limit of daily-bar data). This is what actually enforces the risk plan the agent sized.
            if position > 0 and (price <= stop_price or price >= target_price):
                await close_position(i, day, price, "stop-loss" if price <= stop_price else "target")

            # Only spend an LLM call every rebalance_days; other days we simply carry the position.
            is_decision_day = i % cfg.rebalance_days == 0
            if is_decision_day:
                last_signal = await decide(cfg.symbol, day)
                signals[last_signal["action"]] = signals.get(last_signal["action"], 0) + 1
                decision_errors += bool(last_signal.get("error"))
                no_news += not last_signal.get("error") and last_signal.get("sentiment") == "unavailable"
            action = last_signal["action"] if is_decision_day else "HOLD"
            conf = float(last_signal.get("confidence", 0.0))

            # --- Apply the trading rule ---
            if action == "BUY" and conf >= cfg.min_confidence and position == 0 and cash > 0:
                spend = cash * cfg.position_pct
                fill = price * (1 + cfg.slippage_pct)  # slippage: a buy fills a touch ABOVE the close (worse)
                # Size so the shares plus their fee fit the budget: shares * fill * (1 + fee_pct) <= spend
                shares = math.floor(spend / (fill * (1 + cfg.fee_pct)))  # whole shares only
                if shares > 0:
                    cost = shares * fill
                    fee = cost * cfg.fee_pct  # commission on the trade value
                    cash -= cost + fee
                    position = shares
                    entry_price = fill
                    entry_i = i
                    cost_basis = cost + fee  # what it truly cost us to get in
                    # Enforce the stop/target the agent sized for THIS entry (from decision-service).
                    # Falls back to no stop / no target if the signal didn't include a position block.
                    pos = last_signal.get("position") or {}
                    stop_price = float(pos.get("stop_loss") or 0.0)
                    target_price = float(pos.get("target") or math.inf)
                    trades.append({"side": "BUY", "date": day.isoformat(), "price": round(fill, 4),
                                   "shares": shares, "fee": round(fee, 2)})
                    await emit({"type": "trade", "side": "BUY", "date": day.isoformat(),
                                "price": round(fill, 4), "shares": shares, "fee": round(fee, 2)})

            elif action == "SELL" and conf >= cfg.min_confidence and position > 0:
                await close_position(i, day, price, "signal")

            # Mark-to-market equity for every day so the chart is smooth
            equity = cash + position * price
            equity_curve.append({"date": day.isoformat(), "equity": equity, "price": price, "position": position})
            await emit({
                "type": "step", "i": i + 1, "total": len(days), "date": day.isoformat(),
                "action": action if is_decision_day else "hold", "confidence": conf,
                "price": price, "cash": cash, "position": position, "equity": equity,
                "sentiment": last_signal.get("sentiment", "") if is_decision_day else "",
                "reasoning": last_signal.get("reasoning", "") if is_decision_day else "",
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
