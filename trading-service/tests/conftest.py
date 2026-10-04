"""Shared fixtures: a clean database for every test and a fully controllable fake broker.

Which database:
  TEST_DATABASE_URL set  -> that Postgres database (`make test-trading` uses trading_test on trading-db,
                            the same engine production runs on, so Postgres-only behaviour is tested)
  not set                -> a throwaway SQLite file (quick runs without Docker)

Environment is set BEFORE the app is imported, so no test ever touches the real trading database,
starts the scheduler, sees your broker keys, or calls a real broker or decision-service.
"""

import os
import tempfile
from datetime import datetime, timezone

from sqlalchemy import create_engine, make_url, text

_test_url = os.getenv("TEST_DATABASE_URL")
if _test_url:
    _url = make_url(_test_url)
    # Tests wipe every table before each test. Refuse anything that isn't clearly a test database.
    if not (_url.database or "").endswith("_test"):
        raise RuntimeError(f"TEST_DATABASE_URL must point at a database whose name ends in _test, got {_url.database!r}")
    # Create the test database on first use (connect to the server's maintenance db to do it)
    _admin = create_engine(_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with _admin.connect() as conn:
        if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": _url.database}):
            conn.execute(text(f'CREATE DATABASE "{_url.database}"'))
    _admin.dispose()
    os.environ["DATABASE_URL"] = _test_url
else:
    os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["SCHEDULER_ENABLED"] = "false"
# `make test-trading` runs inside the service container, which loads trading-service/.env. Blank out the
# broker keys so no test can ever reach a real account, and results don't depend on what's in your .env.
for _key in ("ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY", "ALPACA_LIVE_KEY_ID", "ALPACA_LIVE_SECRET_KEY"):
    os.environ[_key] = ""
os.environ["ALLOW_LIVE_TRADING"] = "false"
os.environ["SENTRY_DSN"] = ""  # the tests' deliberate failures must not land in GlitchTip as real errors
os.environ["JWT_SECRET"] = "test-secret-" + "x" * 32  # fixed, so tests never depend on the one in your .env
os.environ["COOKIE_SECURE"] = "false"  # the test client talks plain http

import pytest  # noqa: E402

from app.brokers.base import Broker, BrokerError, BrokerOrder, Quote  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Bot  # noqa: E402

IS_POSTGRES = engine.dialect.name == "postgresql"

# 2025-06-02 is a Monday. New York is UTC-4 in June: open 13:30 UTC, close 20:00 UTC.
MON = datetime(2025, 6, 2, tzinfo=timezone.utc)


def at(hour: int, minute: int = 0, day: int = 2) -> datetime:
    """A UTC moment on June <day> 2025. at(19, 30) = 15:30 New York = decision time."""
    return datetime(2025, 6, day, hour, minute, tzinfo=timezone.utc)


class FakeBroker(Broker):
    """Fills instantly at `price` (with optional slippage/fee), unless told to misbehave.
    With stops=True it also holds stop orders, like Alpaca: they rest until the test calls trigger_stop().
    `prices` gives some symbols their own price (rotation bots trade several)."""

    name = "fake"

    def __init__(self, price: float = 100.0, now: datetime | None = None, quote_at: datetime | None = None,
                 slippage_pct: float = 0.0, fee_pct: float = 0.0, stops: bool = False, prices: dict | None = None):
        self.supports_stop_orders = stops
        self.stop_mode = "rest"  # rest | reject | raise
        self.cancel_mode = "cancel"  # cancel | filled (the stop executed first) | pending (not confirmed) | raise
        self.stops: list[tuple[int, float]] = []  # (qty, stop_price) of every stop order received
        self.canceled: list[str] = []
        self.price = price
        self.prices = prices or {}
        self.now = now  # the test's "current time"; quotes are stamped with it (i.e. fresh) ...
        self.quote_at = quote_at  # ... unless a fixed quote time is given (to simulate stale data)
        self.slippage_pct = slippage_pct
        self.fee_pct = fee_pct
        self.submit_mode = "fill"  # fill | pending | reject | raise
        self.orders: dict[str, BrokerOrder] = {}
        self.submitted: list[tuple[str, int]] = []
        self.sent: list[tuple[str, str, int]] = []  # (side, symbol, qty)
        self.bp: float | None = None
        self.held: int | None = None
        self.held_by: dict[str, int] | None = None  # a real account's positions per symbol (rotation bots)

    def quote(self, symbol):
        return Quote(self.prices.get(symbol, self.price), self.quote_at or self.now or datetime.now(timezone.utc))

    def submit(self, symbol, side, qty, client_order_id):
        self.submitted.append((side, qty))
        self.sent.append((side, symbol, qty))
        if self.submit_mode == "raise":
            raise BrokerError("network down")
        if self.submit_mode == "reject":
            return BrokerOrder(status="rejected", error="insufficient buying power")
        if self.submit_mode == "pending":
            o = BrokerOrder(status="submitted", broker_order_id="b-" + client_order_id)
            self.orders[client_order_id] = o
            return o
        price = self.prices.get(symbol, self.price)
        fill = price * (1 + self.slippage_pct if side == "BUY" else 1 - self.slippage_pct)
        o = BrokerOrder(status="filled", filled_qty=qty, avg_price=fill, fee=round(qty * fill * self.fee_pct, 2),
                        broker_order_id="b-" + client_order_id)
        self.orders[client_order_id] = o
        if self.held is not None:  # tracking a real account's position
            self.held += qty if side == "BUY" else -qty
        if self.held_by is not None:
            self.held_by[symbol] = self.held_by.get(symbol, 0) + (qty if side == "BUY" else -qty)
        return o

    def lookup(self, client_order_id):
        return self.orders.get(client_order_id)

    def submit_stop(self, symbol, qty, stop_price, client_order_id):
        self.stops.append((qty, stop_price))
        if self.stop_mode == "raise":
            raise BrokerError("network down")
        if self.stop_mode == "reject":
            return BrokerOrder(status="rejected", error="stop price above market")
        o = BrokerOrder(status="submitted", broker_order_id="b-" + client_order_id)
        self.orders[client_order_id] = o
        return o

    def cancel(self, client_order_id):
        if self.cancel_mode == "raise":
            raise BrokerError("network down")
        o = self.orders.get(client_order_id)
        if o is None or o.status != "submitted":
            return o
        if self.cancel_mode == "filled":
            qty = next(q for q, _ in self.stops[::-1])
            return self.trigger_stop(client_order_id, qty, self.price)
        if self.cancel_mode == "pending":
            return o
        self.canceled.append(client_order_id)
        self.orders[client_order_id] = BrokerOrder(status="canceled", broker_order_id=o.broker_order_id)
        return self.orders[client_order_id]

    def trigger_stop(self, client_order_id, qty, price):
        """The price fell to the stop at the broker: the stop order fills there, whatever this service is doing."""
        self.orders[client_order_id] = BrokerOrder(status="filled", filled_qty=qty, avg_price=price,
                                                   broker_order_id="b-" + client_order_id)
        if self.held is not None:
            self.held -= qty
        return self.orders[client_order_id]

    def buying_power(self):
        return self.bp

    def position_qty(self, symbol):
        return self.held_by.get(symbol, 0) if self.held_by is not None else self.held


@pytest.fixture(scope="session", autouse=True)
def schema():
    """Build the schema from the models once per run (drop first, so model changes are always picked up)."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def fresh_db(schema):
    """Empty every table before each test; ids restart at 1 so tests are independent of run order."""
    if IS_POSTGRES:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    else:
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
                  fee_pct=0.0, slippage_pct=0.0, max_drawdown_pct=0.2, strategy="sma_rsi")
    params.update(kw)
    bot = Bot(**params)
    session.add(bot)
    session.commit()
    return bot


def signal(action="BUY", confidence=0.8, **extra):
    """A stand-in for decision-service: returns a fixed signal and counts calls."""
    calls = []

    def fn(bot, as_of, at=None):
        calls.append(as_of)
        fn.times.append(at)
        return {"action": action, "confidence": confidence, "sentiment": "bullish", "reasoning": "test", "steps": ["s"], **extra}

    fn.calls = calls
    fn.times = []  # the decision moment passed with each call (the news cutoff)
    return fn
