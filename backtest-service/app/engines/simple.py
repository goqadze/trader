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
        entry_price = 0.0  # price we bought at, for per-trade PnL
        trades: list[dict] = []
        equity_curve: list[dict] = []
        last_signal = {"action": "HOLD", "confidence": 0.0, "reasoning": "", "steps": []}

        # Tell monitors the run is starting (frontend uses this to size the progress bar / charts)
        await emit({"type": "start", "symbol": cfg.symbol, "total": len(days),
                    "initial_cash": cfg.initial_cash, "first_price": float(prices.iloc[0])})

        for i, day in enumerate(days):
            price = float(prices.iloc[i])

            # Only spend an LLM call every rebalance_days; other days we simply carry the position.
            is_decision_day = i % cfg.rebalance_days == 0
            if is_decision_day:
                last_signal = await decide(cfg.symbol, day)
            action = last_signal["action"] if is_decision_day else "HOLD"
            conf = float(last_signal.get("confidence", 0.0))

            # --- Apply the trading rule ---
            if action == "BUY" and conf >= cfg.min_confidence and position == 0 and cash > 0:
                spend = cash * cfg.position_pct
                shares = math.floor(spend / price)  # whole shares only
                if shares > 0:
                    cash -= shares * price
                    position = shares
                    entry_price = price
                    trades.append({"side": "BUY", "date": day.isoformat(), "price": price, "shares": shares})
                    await emit({"type": "trade", "side": "BUY", "date": day.isoformat(),
                                "price": price, "shares": shares})

            elif action == "SELL" and conf >= cfg.min_confidence and position > 0:
                proceeds = position * price
                pnl = (price - entry_price) * position  # profit/loss on this round trip
                cash += proceeds
                trades.append({"side": "SELL", "date": day.isoformat(), "price": price,
                               "shares": position, "pnl": pnl})
                await emit({"type": "trade", "side": "SELL", "date": day.isoformat(),
                            "price": price, "shares": position, "pnl": pnl})
                position = 0.0
                entry_price = 0.0

            # Mark-to-market equity for every day so the chart is smooth
            equity = cash + position * price
            equity_curve.append({"date": day.isoformat(), "equity": equity, "price": price})
            await emit({
                "type": "step", "i": i + 1, "total": len(days), "date": day.isoformat(),
                "action": action if is_decision_day else "hold", "confidence": conf,
                "price": price, "cash": cash, "position": position, "equity": equity,
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
        return {
            "final_equity": round(final_equity, 2),
            "total_return_pct": round((final_equity / cfg.initial_cash - 1) * 100, 2),
            "buy_hold_return_pct": round(buy_hold_return, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "num_trades": len(closed),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "trades": trades,
            "equity_curve": equity_curve,
        }
