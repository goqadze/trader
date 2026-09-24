"""Database tables: the trading history that survives restarts.

    Bot            one strategy running on one symbol with its own parameters and its own capital
    Decision       every time a bot asked decision-service for a signal, and what it did about it
    Order          every order sent to a broker (write-ahead: saved BEFORE it is sent, see trader.submit_order)
    EquitySnapshot one row per bot per trading day, for the equity chart
    Event          audit log: created, paused, parameters changed, stop-loss hit, errors, ...

Money is stored as float and rounded to cents at each step, same as the backtest engine. That keeps the
two in agreement; switch to Decimal (Numeric columns) if you ever do real accounting on top of this.
"""

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


class UTCDateTime(TypeDecorator):
    """Timestamps that are always timezone-aware UTC in Python.

    On Postgres this is TIMESTAMPTZ, a real point in time. SQLite has no timezone type and hands values
    back 'naive', so there we re-attach UTC on the way out. Either way, code comparing `now` with a
    stored time never mixes aware and naive datetimes (which would raise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("naive datetime; use timezone-aware UTC (db.utcnow())")
            value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# Bot lifecycle:
#   active   -> decides on schedule, enters and exits positions
#   paused   -> no new decisions or entries; stop-loss / target exits still protect an open position
#   archived -> retired (only allowed when flat); hidden from the dashboard, history kept forever
BOT_STATUSES = ("active", "paused", "archived")


class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    broker: Mapped[str] = mapped_column(String(32))  # paper | alpaca-paper | alpaca-live (see brokers/)
    status: Mapped[str] = mapped_column(String(16), default="active")

    # --- Strategy parameters: the same knobs as the backtest RunConfig, so a tested setup deploys 1:1 ---
    mode: Mapped[str] = mapped_column(String(8), default="rules")  # rules | llm (passed to decision-service)
    min_confidence: Mapped[float] = mapped_column(Float, default=0.6)  # ignore weaker signals
    rebalance_days: Mapped[int] = mapped_column(Integer, default=5)  # decide every N trading days
    # When, on a decision day: close (30 min before the close, like the backtest) | open (30 min after the
    # open) | both. Live-only: the backtest always decides at the close. server_default: see db.init_db
    decide_at: Mapped[str] = mapped_column(String(8), default="close", server_default="close")
    position_pct: Mapped[float] = mapped_column(Float, default=1.0)  # fraction of the bot's cash per BUY
    stop_pct: Mapped[float] = mapped_column(Float, default=0.04)  # stop-loss this far below the entry fill
    target_pct: Mapped[float] = mapped_column(Float, default=0.08)  # take profit this far above the entry fill
    fee_pct: Mapped[float] = mapped_column(Float, default=0.0)  # paper broker only: simulated commission
    slippage_pct: Mapped[float] = mapped_column(Float, default=0.0005)  # paper broker only: simulated slippage
    # Circuit breaker: auto-pause when equity falls this far below its best level (0.2 = 20%)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.2)

    # --- Capital and the current position (the bot's own "sub-account") ---
    allocated_cash: Mapped[float] = mapped_column(Float)  # starting capital, never changes
    cash: Mapped[float] = mapped_column(Float)  # uninvested cash right now
    shares: Mapped[int] = mapped_column(Integer, default=0)  # long-only, whole shares
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # average fill of the open position
    cost_basis: Mapped[float] = mapped_column(Float, default=0.0)  # cash spent to open it (fill + fee)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # sum of closed trades' profit/loss
    peak_equity: Mapped[float] = mapped_column(Float)  # best equity so far, for the drawdown breaker

    # --- Market data bookkeeping ---
    benchmark_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # price at creation: buy & hold baseline
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_decision_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # trading day of the last real decision
    # The exact moment of that decision: tells the scheduler which of the day's slots are already done
    last_decision_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    session_date: Mapped[date] = mapped_column(Date)  # the trading day this decision belongs to (New York date)
    kind: Mapped[str] = mapped_column(String(16))  # scheduled | manual (Run now, market open) | preview (market closed: no trading)
    action: Mapped[str] = mapped_column(String(8))  # BUY | SELL | HOLD (HOLD also when the signal call failed)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    sentiment: Mapped[str] = mapped_column(String(32), default="")  # clipped in trader.evaluate (LLM output)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list] = mapped_column(JSON, default=list)  # the agent's full "show your work" trail
    price: Mapped[float | None] = mapped_column(Float, nullable=True)  # quote when the decision was made
    outcome: Mapped[str] = mapped_column(Text, default="")  # what the bot DID, e.g. "BUY 12 sh filled @ 201.3"


# Order status flow:  new -> submitted -> filled | partially_filled | canceled | rejected
#                     new -> failed (broker never received it)
OPEN_ORDER_STATUSES = ("new", "submitted")

# Order types:
#   market  buy/sell now; the position is "in flux" until it finishes (a few seconds)
#   stop    a protective SELL resting at the broker (Alpaca) at the stop-loss price, good-till-canceled.
#           It stays "submitted" for as long as the position is held, and fills by itself if the price
#           falls to it -- even while this service is down. See trader.protect().
ORDER_TYPES = ("market", "stop")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    side: Mapped[str] = mapped_column(String(4))  # BUY | SELL
    # server_default: lets db.init_db add these columns to an existing orders table (old rows = market)
    order_type: Mapped[str] = mapped_column(String(8), default="market", server_default="market")
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # stop orders only: the trigger price
    qty: Mapped[int] = mapped_column(Integer)  # requested shares
    reason: Mapped[str] = mapped_column(String(16))  # signal | stop-loss | target | manual
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    # Our own unique id, sent to the broker. If the network drops mid-submit we can ask the broker
    # "did you get order X?" instead of guessing -- and a retry can never create a duplicate order.
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)  # SELLs only: round-trip profit/loss
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (UniqueConstraint("bot_id", "day"),)  # one point per bot per trading day (updated intraday)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    shares: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), nullable=True, index=True)  # None = system-wide
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")  # info | warning | error
    kind: Mapped[str] = mapped_column(String(24))  # created | paused | resumed | params | order | risk | reconcile | error | halt
    message: Mapped[str] = mapped_column(Text)
