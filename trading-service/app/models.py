"""Database tables: the trading history that survives restarts.

    Bot            one strategy running on one symbol with its own parameters and its own capital -- or a momentum
                   rotation across a universe of symbols (strategy == ROTATION, see rotation.py) -- or a dip buyer
                   watching a list of symbols (strategy == DIP, see dip.py)
    Holding        a rotation or dip bot's positions, one row per symbol it holds
    WatchItem      a dip bot's watchlist, one row per symbol: watching or blacklisted, and where it stood at the last check
    Signal         a dip bot's recommendations: a symbol fell into the buy zone, is back up, hit its stop
    DipPreset      a dip buyer setup saved under a name (rules, watchlist, backtest periods), to load into a form later
    DipStudy       a batch of dip buyer backtests run together (a parameter sweep), and DipTest: each setup it tested
                   and how it did in each window
    Notification   an email alert: queued with the event it tells about, then sent (or retried) by notify.py
    Setting        small settings changed on the dashboard (the email alerts' addresses and choices); secrets stay in .env
    Decision       every time a bot asked decision-service for a signal, and what it did about it
    Order          every order sent to a broker (write-ahead: saved BEFORE it is sent, see trader.submit_order)
    EquitySnapshot one row per bot per trading day, for the equity chart
    Event          audit log: created, paused, parameters changed, stop-loss hit, errors, ...
    User           who may sign in to the dashboard (see auth.py)

Money is stored as float and rounded to cents at each step, same as the backtest engine. That keeps the
two in agreement; switch to Decimal (Numeric columns) if you ever do real accounting on top of this.
"""

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator, UniqueConstraint
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

# Bot.strategy of a momentum rotation bot: one bot holding the strongest few of a universe (see rotation.py). Its
# symbol column just says ROTATION; what it holds lives in Holding, and benchmark_price / last_price track the value
# of the universe bought in equal parts at the start (beginning at allocated_cash) instead of one symbol's price.
ROTATION = "momentum_rotation"
# Bot.strategy of a dip buyer: one bot watching a list of symbols (WatchItem), buying any that fell drop_pct during
# the last `lookback` days and selling it back at the price the fall started from (see dip.py). Like a rotation bot
# its symbol column just says DIP, its positions live in Holding, and benchmark_price / last_price track the starting
# watchlist held in equal parts (`universe` keeps that starting list; the watchlist itself can change).
DIP = "dip_buyer"
HOLDINGS_STRATEGIES = (ROTATION, DIP)  # the bots that hold several symbols at once (Holding rows)


class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    broker: Mapped[str] = mapped_column(String(32))  # paper | alpaca-paper | alpaca-live (see brokers/)
    status: Mapped[str] = mapped_column(String(16), default="active")

    # --- Strategy parameters: the same knobs as the backtest RunConfig, so a tested setup deploys 1:1 ---
    # Which decision-service strategy decides (see schemas.Strategy). server_default: existing bots, which
    # used the original rules, get the same rules under their new name. (Replaces the old `mode` column.)
    strategy: Mapped[str] = mapped_column(String(32), default="sma_rsi", server_default="sma_rsi")
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
    allocated_cash: Mapped[float] = mapped_column(Float)  # starting capital, never changes (added money: added_cash)
    cash: Mapped[float] = mapped_column(Float)  # uninvested cash right now
    shares: Mapped[int] = mapped_column(Integer, default=0)  # long-only, whole shares
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # average fill of the open position
    cost_basis: Mapped[float] = mapped_column(Float, default=0.0)  # cash spent to open it (fill + fee)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # sum of closed trades' profit/loss
    peak_equity: Mapped[float] = mapped_column(Float)  # best equity so far, for the drawdown breaker
    # Money you added while it runs (POST /bots/{id}/capital; NULL = none): what you put in is allocated_cash + this,
    # which the return is measured against. The buy & hold baseline "buys" its basket with each addition at that
    # day's price, benchmark_added more units of it: see trader.benchmark_value
    added_cash: Mapped[float | None] = mapped_column(Float, nullable=True)
    benchmark_added: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Market data bookkeeping ---
    benchmark_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # price at creation: buy & hold baseline
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_decision_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # trading day of the last real decision
    # The exact moment of that decision: tells the scheduler which of the day's slots are already done
    last_decision_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # --- Momentum rotation bots only (NULL on the others): same names as the backtest's RotationConfig ---
    universe: Mapped[list | None] = mapped_column(JSON, nullable=True)  # the symbols it chooses from
    top_n: Mapped[int | None] = mapped_column(Integer, nullable=True)  # how many of the strongest it holds
    lookback_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skip_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    abs_filter: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # only hold what rose, else cash
    benchmark_prices: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # the universe's prices at creation
    # The rebalance in progress: {"decision_id", "targets": [[symbol, shares], ...], "rounds"}; NULL when done
    rotation_plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- Dip buyers only (NULL on the others): same names as the backtest's DipConfig. stop_pct is the shared column ---
    interval: Mapped[str | None] = mapped_column(String(4), nullable=True)  # how often it checks: 5m | 15m | 30m | 1h | 1d
    drop_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # buy after a fall this big ...
    # ... for every symbol ("percent"; NULL on bots from before = that), scaled by its price tier ("price": price_tiers,
    # NULL = the default tiers), or drop_atr times its usual daily move ("volatility"). See dip.buy_fall
    drop_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    price_tiers: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{"up_to": 100, "share": 1.0}, ...]
    drop_atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    lookback: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ... during the last `lookback` ...
    lookback_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)  # ... days | hours
    drop_from: Mapped[str | None] = mapped_column(String(8), nullable=True)  # high (the window's top) | start (its first close)
    target_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)  # reference (back to it) | percent (+rise_pct)
    rise_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_positions: Mapped[int | None] = mapped_column(Integer, nullable=True)  # slots: each buy gets 1/max_positions of equity
    max_hold_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # sell after this many trading days; 0 = never
    news: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # ask the news before a buy: bearish = not today
    trend_filter: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # only buy dips in an uptrend (SMA50 > SMA200)
    # Wait for the turn: buy a fall only once it is back up rebound_pct from its low (NULL on bots from before = off)
    rebound: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rebound_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Buy fractions of a share (where the broker can split the symbol) so small slots still buy; NULL = whole shares
    fractional: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # After a stop-loss, buy that symbol again this many trading days later (the backtest's "Blacklist lasts");
    # 0 / NULL = only once you re-enable it
    reenable_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Capital was added with "top up": the next check that can buy brings each holding up to a full slot (dip.top_up)
    top_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Holding(Base):
    """One symbol a rotation or dip bot holds right now (the row goes once it's sold). Same accounting as Bot's position:
    cost_basis is what the shares still held cost, fees included; each sale books its slice as P&L. A dip bot's holding
    also carries its exit levels, set from the buy's fill (see trader._book_holding_fill)."""

    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("bot_id", "symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    shares: Mapped[float] = mapped_column(Float, default=0.0)  # a fraction of a share on a dip or rotation bot with `fractional`
    cost_basis: Mapped[float] = mapped_column(Float, default=0.0)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # --- Dip bots only ---
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # average buy fill
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # the price the fall started from
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # sell at or above (back at the reference)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # sell at or below, and blacklist the symbol
    # The ex-date of the last stock split applied to it (splits.apply); NULL = none since the day it was opened
    splits_through: Mapped[date | None] = mapped_column(Date, nullable=True)


# A watched symbol's status: watching -> blacklisted (a stop-loss sold it; it stays out until you re-enable it)
WATCH_STATUSES = ("watching", "blacklisted")


class WatchItem(Base):
    """One symbol on a dip bot's watchlist. You add and remove them while the bot runs. The last-check columns are only
    for the dashboard and the signals: a check recomputes everything from fresh prices."""

    __tablename__ = "watch_items"
    __table_args__ = (UniqueConstraint("bot_id", "symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12), default="watching")
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    blacklisted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    blacklist_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reenable_on: Mapped[date | None] = mapped_column(Date, nullable=True)  # a stop's blacklist ends that day; NULL = you
    # --- The last check ---
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # the window's high (or start)
    drop: Mapped[float | None] = mapped_column(Float, nullable=True)  # how far under it: 0.06 = 6% below
    in_zone: Mapped[bool] = mapped_column(Boolean, default=False)  # at least drop_pct under: a new dip only when it enters
    buy_drop: Mapped[float | None] = mapped_column(Float, nullable=True)  # the fall that was the buy zone (dip.buy_fall)
    # The fall being followed (from the check it fell into the buy zone until it is bought or back up): the reference
    # it fell from (the target to get back to; back there first = the dip is over, an "up" signal), its lowest price
    # since, and when it first turned up rebound_pct from that low. NULL = no fall being followed.
    dip_reference: Mapped[float | None] = mapped_column(Float, nullable=True)
    armed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    trough_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trough_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    turned_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    sold_on: Mapped[date | None] = mapped_column(Date, nullable=True)  # sold today: not bought again until tomorrow
    news_sentiment: Mapped[str | None] = mapped_column(String(16), nullable=True)  # the last news verdict asked for
    news_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    news_blocked_on: Mapped[date | None] = mapped_column(Date, nullable=True)  # bearish news: no buy on this day
    trend_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # the trend filter's last answer


# Signal kinds -- a dip bot's recommendations, for your information whether or not it traded on them:
#   down  the price fell into the buy zone (drop_pct under its reference): a buy -- or, waiting for the turn, a "get
#         ready". Says whether the bot bought.
#   rebound  waiting for the turn: back up rebound_pct from its low, bearish turned bullish: a buy
#   up    back at the price the fall started from (or the target): a sell. Sold if the bot held it.
#   stop  fell stop_pct under the buy: sold and blacklisted
#   time  held max_hold_days: sold
#   news  bearish news: the dip was not bought today
SIGNAL_KINDS = ("down", "rebound", "up", "stop", "time", "news")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(8))
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # vs the reference (down) or the buy (exits)
    message: Mapped[str] = mapped_column(Text, default="")  # the recommendation in words
    outcome: Mapped[str] = mapped_column(Text, default="")  # what the bot did about it


class DipPreset(Base):
    """A dip buyer setup you saved under a name: schemas.DipPresetConfig as JSON. The dashboard loads it into the
    backtest form or a new (or running) dip bot's rules."""

    __tablename__ = "dip_presets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    config: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DipStudy(Base):
    """A batch of dip buyer backtests run together (a parameter sweep): what was varied, over which windows, and what
    came out of it. Its tests are DipTest rows; the dashboard's Test results tab shows them as a grid."""

    __tablename__ = "dip_studies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")  # what was varied, and the findings in words
    periods: Mapped[dict] = mapped_column(JSON)  # {"practice": [start, end], "exam": [...], ...}, in the grid's order
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DipTest(Base):
    """One tested setup of a study: its name, the setup as a saved setup keeps it (schemas.DipPresetConfig, so it can
    be saved or loaded into the backtest form as is), and how it did in each of the study's windows."""

    __tablename__ = "dip_tests"

    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("dip_studies.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(20))  # short id within the study: "T0123"
    name: Mapped[str] = mapped_column(String(60))  # the name it gets when saved as a setup
    watchlist: Mapped[str] = mapped_column(String(60), default="")  # the watchlist's name
    config: Mapped[dict] = mapped_column(JSON)
    results: Mapped[dict] = mapped_column(JSON)  # period -> the grid's numbers (return, buy & hold, drawdown, trades ...)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)  # period -> its equity curve and per-symbol rows
    score: Mapped[float | None] = mapped_column(Float, nullable=True)  # the study's ranking number (higher = better)
    pick: Mapped[int | None] = mapped_column(Integer, nullable=True)  # recommended: 1 = the best pick; None = not picked
    note: Mapped[str] = mapped_column(Text, default="")
    # What the test adds beyond the setup's rules: a filter the bots don't have yet ("skip dips right after earnings").
    # Saving such a test as a setup keeps its rules only
    extra: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Twelve separate 3-month tests back to back: {"won", "of", "worst", "returns"} (run for the best setups only)
    quarters: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# What an email alert is about (notify.CATEGORIES describes each); you choose which ones you get
NOTIFY_CATEGORIES = ("trades", "risk", "problems", "signals")
# pending -> sent | failed (gave up after the retries) | skipped (alerts were turned off before it went out)
NOTIFY_STATUSES = ("pending", "sent", "failed", "skipped")


class Notification(Base):
    """An email alert. Queued in the same transaction as the event it tells about (so a rolled-back event never
    emails), then sent by notify.py's background loop: a slow or broken mail server never holds up trading."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    bot_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # None: not about one bot (HALT ALL, a test)
    category: Mapped[str] = mapped_column(String(16))  # NOTIFY_CATEGORIES, or "test"
    kind: Mapped[str] = mapped_column(String(32))  # buy | sell | the event's kind | signal-<kind> | test
    subject: Mapped[str] = mapped_column(String(200))  # one line: "SELL AAPL +$345.60 (+7.0%)"
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # after a failed send
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)  # the last failed send's reason


class Setting(Base):
    """A setting changed on the dashboard, as JSON under a key (notify.KEY: the email alerts)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    session_date: Mapped[date] = mapped_column(Date)  # the trading day this decision belongs to (New York date)
    kind: Mapped[str] = mapped_column(String(16))  # scheduled | manual (Run now, market open) | preview (market closed: no trading)
    # BUY | SELL | HOLD (HOLD also when the signal call failed); rotation bots: ROTATE (the holdings change) | HOLD;
    # dip bots: one decision per check that traded (or Run now): BUY | SELL | TRADE (both) | HOLD
    action: Mapped[str] = mapped_column(String(8))
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
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)  # NULL on orders from before: the bot's symbol
    # server_default: lets db.init_db add these columns to an existing orders table (old rows = market)
    order_type: Mapped[str] = mapped_column(String(8), default="market", server_default="market")
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # stop orders only: the trigger price
    # A dip bot's BUY: the price the fall started from, which becomes the holding's target once it fills
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[float] = mapped_column(Float)  # requested shares (fractions only from a dip or rotation bot with `fractional`)
    reason: Mapped[str] = mapped_column(String(16))  # signal | stop-loss | target | manual | rotation | rebalance | dip | time
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    # Our own unique id, sent to the broker. If the network drops mid-submit we can ask the broker
    # "did you get order X?" instead of guessing -- and a retry can never create a duplicate order.
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
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
    # The buy & hold baseline's value that day, the money added since the start included (NULL on days from before:
    # allocated_cash x price / benchmark_price then)
    benchmark: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), nullable=True, index=True)  # None = system-wide
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")  # info | warning | error
    kind: Mapped[str] = mapped_column(String(24))  # created | paused | resumed | params | order | risk | reconcile | error | halt
    message: Mapped[str] = mapped_column(Text)


# User lifecycle:
#   pending  -> signed up, waiting for an admin; can't sign in yet
#   active   -> approved by an admin; can sign in
#   rejected -> an admin said no; the username stays taken so the same person can't just sign up again
#   disabled -> was active, switched off by an admin; signed out everywhere at once
USER_STATUSES = ("pending", "active", "rejected", "disabled")
USER_ROLES = ("admin", "user")  # admin = everything a user can do + approve and manage users


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)  # stored lowercase, so sign-in ignores case
    name: Mapped[str] = mapped_column(String(80))  # so the admin knows who is asking to join
    password_hash: Mapped[str] = mapped_column(String(255))  # Argon2id, never the password itself
    role: Mapped[str] = mapped_column(String(8), default="user")
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    # Copied into every session token. Bumping it (password change, disable) makes all earlier tokens invalid.
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the admin's username
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
