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
        stop_price = 0.0  # sell if price falls to/below this (set from the agent's sizing at entry)
        target_price = math.inf  # sell if price rises to/above this
        trades: list[dict] = []
        equity_curve: list[dict] = []
        last_signal = {"action": "HOLD", "confidence": 0.0, "reasoning": "", "steps": []}

        async def close_position(day, price, reason: str) -> None:
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
                   "shares": position, "fee": round(fee, 2), "pnl": round(pnl, 2), "reason": reason}
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
                await close_position(day, price, "stop-loss" if price <= stop_price else "target")

            # Only spend an LLM call every rebalance_days; other days we simply carry the position.
            is_decision_day = i % cfg.rebalance_days == 0
            if is_decision_day:
                last_signal = await decide(cfg.symbol, day)
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
                await close_position(day, price, "signal")

            # Mark-to-market equity for every day so the chart is smooth
            equity = cash + position * price
            equity_curve.append({"date": day.isoformat(), "equity": equity, "price": price})
            await emit({
                "type": "step", "i": i + 1, "total": len(days), "date": day.isoformat(),
                "action": action if is_decision_day else "hold", "confidence": conf,
                "price": price, "cash": cash, "position": position, "equity": equity,
                "sentiment": last_signal.get("sentiment", "") if is_decision_day else "",
                "reasoning": last_signal.get("reasoning", "") if is_decision_day else "",
            })

        result = self._metrics(cfg, prices, cash, position, trades, equity_curve)
        await emit({"type": "done", "result": result})
        return result

    def _metrics(self, cfg, prices, cash, position, trades, equity_curve) -> dict:
        """Summary statistics the frontend shows in the results panel."""
        first_price = float(prices.iloc[0])
        last_price = float(prices.iloc[-1])
        final_equity = cash + position * last_price

        # Buy & hold benchmark: what if you just bought on day 1 and did nothing?
        buy_hold_return = (last_price / first_price - 1) * 100

        # Max drawdown: largest peak-to-trough drop of the equity curve, in percent
        peak = -math.inf
        max_dd = 0.0
        for pt in equity_curve:
            peak = max(peak, pt["equity"])
            if peak > 0:
                max_dd = min(max_dd, (pt["equity"] - peak) / peak * 100)

        closed = [t for t in trades if t["side"] == "SELL"]
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        strategy_return = (final_equity / cfg.initial_cash - 1) * 100
        # Alpha = how much the strategy beat (or lagged) simply buying and holding the stock.
        # If this is negative, the strategy isn't earning its complexity, fees, or risk.
        alpha = strategy_return - buy_hold_return
        return {
            "final_equity": round(final_equity, 2),
            "total_return_pct": round(strategy_return, 2),
            "buy_hold_return_pct": round(buy_hold_return, 2),
            "alpha_vs_buy_hold_pct": round(alpha, 2),  # strategy return minus buy & hold
            "beat_buy_hold": alpha > 0,  # the bar to clear: did the strategy add value?
            "max_drawdown_pct": round(max_dd, 2),
            "num_trades": len(closed),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "trades": trades,
            "equity_curve": equity_curve,
        }
