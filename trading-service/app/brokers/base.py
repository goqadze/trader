"""The broker interface. The trader only talks to this, so the built-in paper simulator, Alpaca and
(later) Interactive Brokers are interchangeable per bot. To add a broker: subclass Broker, implement
the five methods, register it in brokers/__init__.py."""

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
    filled_qty: int = 0
    avg_price: float | None = None
    fee: float = 0.0
    broker_order_id: str | None = None
    error: str | None = None


class BrokerError(Exception):
    """The broker refused or could not be reached. The message is shown in the bot's event log."""


class Broker(ABC):
    name: str = "base"
    live: bool = False  # True = real money

    @abstractmethod
    def quote(self, symbol: str) -> Quote:
        """Latest trade price. Raises BrokerError when unavailable."""

    @abstractmethod
    def submit(self, symbol: str, side: str, qty: int, client_order_id: str) -> BrokerOrder:
        """Send a market order. May return 'submitted' (not filled yet); the scheduler then polls lookup()."""

    @abstractmethod
    def lookup(self, client_order_id: str) -> BrokerOrder | None:
        """Current state of an order we sent, by OUR id. None = the broker never received it."""

    def buying_power(self) -> float | None:
        """Cash the account can spend right now. None = no real account (simulator): the bot's own cash is the limit."""
        return None

    def position_qty(self, symbol: str) -> int | None:
        """Shares the real account holds. None = nothing to reconcile against (simulator)."""
        return None
