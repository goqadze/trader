"""Shared fixtures: a throwaway SQLite database per test and a fully controllable fake broker.

Environment is set BEFORE the app is imported, so no test ever touches /data/trading.db, starts the
scheduler, or calls a real broker or decision-service.
"""

import os
import tempfile
from datetime import datetime, timezone

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SCHEDULER_ENABLED"] = "false"

import pytest  # noqa: E402

from app.brokers.base import Broker, BrokerError, BrokerOrder, Quote  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Bot  # noqa: E402

# 2025-06-02 is a Monday. New York is UTC-4 in June: open 13:30 UTC, close 20:00 UTC.
MON = datetime(2025, 6, 2, tzinfo=timezone.utc)


def at(hour: int, minute: int = 0, day: int = 2) -> datetime:
    """A UTC moment on June <day> 2025. at(19, 30) = 15:30 New York = decision time."""
    return datetime(2025, 6, day, hour, minute, tzinfo=timezone.utc)


class FakeBroker(Broker):
    """Fills instantly at `price` (with optional slippage/fee), unless told to misbehave."""

    name = "fake"

    def __init__(self, price: float = 100.0, now: datetime | None = None, quote_at: datetime | None = None,
                 slippage_pct: float = 0.0, fee_pct: float = 0.0):
        self.price = price
        self.now = now  # the test's "current time"; quotes are stamped with it (i.e. fresh) ...
        self.quote_at = quote_at  # ... unless a fixed quote time is given (to simulate stale data)
        self.slippage_pct = slippage_pct
        self.fee_pct = fee_pct
        self.submit_mode = "fill"  # fill | pending | reject | raise
        self.orders: dict[str, BrokerOrder] = {}
        self.submitted: list[tuple[str, int]] = []
        self.bp: float | None = None
        self.held: int | None = None

    def quote(self, symbol):
        return Quote(self.price, self.quote_at or self.now or datetime.now(timezone.utc))

    def submit(self, symbol, side, qty, client_order_id):
        self.submitted.append((side, qty))
        if self.submit_mode == "raise":
            raise BrokerError("network down")
        if self.submit_mode == "reject":
            return BrokerOrder(status="rejected", error="insufficient buying power")
        if self.submit_mode == "pending":
            o = BrokerOrder(status="submitted", broker_order_id="b-" + client_order_id)
            self.orders[client_order_id] = o
            return o
        fill = self.price * (1 + self.slippage_pct if side == "BUY" else 1 - self.slippage_pct)
        o = BrokerOrder(status="filled", filled_qty=qty, avg_price=fill, fee=round(qty * fill * self.fee_pct, 2),
                        broker_order_id="b-" + client_order_id)
        self.orders[client_order_id] = o
        return o

    def lookup(self, client_order_id):
        return self.orders.get(client_order_id)

    def buying_power(self):
        return self.bp

    def position_qty(self, symbol):
        return self.held


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def session():
    with SessionLocal() as s:
        yield s


def make_bot(session, **kw) -> Bot:
    params = dict(name="test", symbol="AAPL", broker="paper", status="active", allocated_cash=10_000.0,
                  cash=10_000.0, peak_equity=10_000.0, benchmark_price=100.0, last_price=100.0,
                  min_confidence=0.6, rebalance_days=1, position_pct=1.0, stop_pct=0.04, target_pct=0.08,
                  fee_pct=0.0, slippage_pct=0.0, max_drawdown_pct=0.2, mode="rules")
    params.update(kw)
    bot = Bot(**params)
    session.add(bot)
    session.commit()
    return bot


def signal(action="BUY", confidence=0.8, **extra):
    """A stand-in for decision-service: returns a fixed signal and counts calls."""
    calls = []

    def fn(bot, as_of):
        calls.append(as_of)
        return {"action": action, "confidence": confidence, "sentiment": "bullish", "reasoning": "test", "steps": ["s"], **extra}

    fn.calls = calls
    return fn
