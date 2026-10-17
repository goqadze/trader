"""The broker interface. The trader only talks to this, so the built-in paper simulator, Alpaca and
(later) Interactive Brokers are interchangeable per bot. To add a broker: subclass Broker, implement
the abstract methods, register it in brokers/__init__.py. A broker that can hold a stop-loss order for
us sets supports_stop_orders and implements submit_stop() and cancel() too."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Quote:
    price: float
    at: datetime  # when that price traded (UTC) -- lets the trader refuse to act on stale data


@dataclass
class BrokerOrder:
    """The broker's view of one order."""

    status: str  # submitted | filled | partially_filled | canceled | rejected
    filled_qty: float = 0  # a fraction of a share on a fractional order
    avg_price: float | None = None
    fee: float = 0.0
    broker_order_id: str | None = None
    error: str | None = None


class BrokerError(Exception):
    """The broker refused or could not be reached. The message is shown in the bot's event log."""


class Broker(ABC):
    name: str = "base"
    live: bool = False  # True = real money
    # True = the stop-loss rests AT THE BROKER as a real stop order, so it fires even while this service
    # is down (see trader.protect). False = the service checks the stop itself every few minutes.
    supports_stop_orders: bool = False
    # Takes a BUY as a dollar amount (submit's `notional`): the bots then send their fractional buys that way
    supports_notional: bool = False

    @abstractmethod
    def quote(self, symbol: str) -> Quote:
        """Latest trade price. Raises BrokerError when unavailable."""

    @abstractmethod
    def submit(self, symbol: str, side: str, qty: float, client_order_id: str, notional: float | None = None) -> BrokerOrder:
        """Send a market order (`qty` may be a fraction of a share where fractionable() says so). With `notional`
        (brokers with supports_notional, a BUY of a fractionable symbol): buy that many dollars' worth instead, the
        shares worked out at the fill; `qty` is only the estimate. May return 'submitted' (not filled yet); the scheduler
        then polls lookup()."""

    @abstractmethod
    def lookup(self, client_order_id: str, cached: bool = False) -> BrokerOrder | None:
        """Current state of an order we sent, by OUR id. None = the broker never received it. cached=True: may come
        from a scheduler pass's one read of the account's recent orders (Alpaca's shared_round), up to half a minute
        old; None is still only ever the broker's own answer for that order."""

    def buying_power(self) -> float | None:
        """The account's own money it can spend right now, never borrowed (no margin). None = no real account
        (simulator): the bot's own cash is the limit."""
        return None

    def funds(self) -> dict | None:
        """A real account's cash, buying power, non-margin buying power and `spendable` (= buying_power()), from one
        read, for the dashboard. None = no real account (simulator)."""
        return None

    def position_qty(self, symbol: str, cached: bool = False) -> float | None:
        """Shares the real account holds. None = nothing to reconcile against (simulator). cached=True: may come from a
        scheduler pass's one read of the whole account (Alpaca's shared_round), so a caller that would act on a
        difference asks again without it first."""
        return None

    def fractionable(self, symbol: str) -> bool:
        """Can this symbol be bought in fractions of a share? The simulator splits anything."""
        return True

    def submit_stop(self, symbol: str, qty: int, stop_price: float, client_order_id: str) -> BrokerOrder:
        """Rest a good-till-canceled SELL stop order: it becomes a market sell once the price trades at or
        below stop_price. Normally returns 'submitted' (resting). Only for brokers with supports_stop_orders."""
        raise NotImplementedError(f"{self.name} can't hold stop orders")

    def cancel(self, client_order_id: str) -> BrokerOrder | None:
        """Cancel a working order and return its final state: 'canceled', 'filled'/'partially_filled' if it
        executed first (that fill is real and must be booked), or still 'submitted' if the broker hasn't
        confirmed yet. None = the broker never had it."""
        raise NotImplementedError(f"{self.name} can't cancel orders")
