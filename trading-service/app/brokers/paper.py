"""Built-in paper broker: real live prices, simulated fills. No account or API key needed.

Fills use the same cost model as the backtest engine (slippage against you + a percentage fee), so
paper results are directly comparable to the backtest that justified the strategy.
"""

from collections.abc import Callable

import yfinance as yf

from ..market import to_utc
from .base import Broker, BrokerError, BrokerOrder, Quote


def yahoo_quote(symbol: str) -> Quote:
    """Last 1-minute bar from Yahoo Finance (free; can lag a little and has no uptime guarantee)."""
    try:
        # Yahoo writes share classes with a dash (BRK-B); Alpaca and the bots with a dot (BRK.B)
        df = yf.Ticker(symbol.replace(".", "-")).history(period="1d", interval="1m", auto_adjust=True)
    except Exception as e:  # yfinance raises many different things on network trouble
        raise BrokerError(f"quote failed for {symbol}: {e}") from e
    if df is None or df.empty:
        raise BrokerError(f"no price data for {symbol} (unknown symbol or data feed down)")
    last = df["Close"].dropna()
    if last.empty:
        raise BrokerError(f"no price data for {symbol}")
    return Quote(price=float(last.iloc[-1]), at=to_utc(last.index[-1]))


class PaperBroker(Broker):
    name = "paper"
    live = False

    def __init__(self, slippage_pct: float = 0.0005, fee_pct: float = 0.0,
                 price_source: Callable[[str], Quote] = yahoo_quote):
        self.slippage_pct = slippage_pct
        self.fee_pct = fee_pct
        self.price_source = price_source  # swappable so tests don't hit the network
        self._orders: dict[str, BrokerOrder] = {}  # in-memory only; paper orders fill instantly anyway

    def quote(self, symbol: str) -> Quote:
        return self.price_source(symbol)

    def submit(self, symbol: str, side: str, qty: int, client_order_id: str) -> BrokerOrder:
        q = self.quote(symbol)
        # Slippage always hurts: buys fill a touch above the price, sells a touch below (same as the backtest)
        fill = q.price * (1 + self.slippage_pct) if side == "BUY" else q.price * (1 - self.slippage_pct)
        fee = qty * fill * self.fee_pct
        order = BrokerOrder(status="filled", filled_qty=qty, avg_price=round(fill, 4), fee=round(fee, 2),
                            broker_order_id=f"paper-{client_order_id}")
        self._orders[client_order_id] = order
        return order

    def lookup(self, client_order_id: str) -> BrokerOrder | None:
        # Paper orders fill inside submit(), so an order left "new" in the DB never reached us
        return self._orders.get(client_order_id)

