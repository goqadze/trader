"""Alpaca broker (https://alpaca.markets): commission-free US stocks with a free paper-trading account
that behaves like the real one. Paper and live use the same code; only the URL and keys differ.

Docs: https://docs.alpaca.markets/reference (Trading API v2 + Market Data v2).

Rate limits: 200 requests a minute for each account's trading API (the paper account and the live one each have their
own), and 200 a minute for the market data on the free plan, ONE budget for the paper and the live keys (it belongs to
the Alpaca user). Over it Alpaca answers 429. So: every request waits and goes again on a 429 (_send), the paper keys
leave the last DATA_KEEP_FOR_LIVE data requests of a minute to the live bots, and a scheduler pass reads the prices and
each account's positions once for all its bots (shared_round) instead of one request per bot and symbol.

The paper account carries 130+ dip bots, and a check where many of them buy at once sends a few hundred orders. So on
that account: an asset's "fractionable" is read once a day, a pass reads the account once and takes each buy it sends
off what the bots may spend, a scheduled dip check's orders don't wait for their fills (the next pass books them, from
one read of the account's recent orders), and its trading requests wait for the next minute below PAPER_KEEP_FREE.
A buy then costs about one request instead of four, and doesn't hold the account for a second. The live account keeps
its fresh reads and waits for its fills: a few orders a day.
"""

import contextvars
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import httpx

from ..config import settings
from ..market import ny_date, to_utc
from .base import Broker, BrokerError, BrokerOrder, Quote

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
BARS_MAX_PAGES = 400  # of about 1,000 bars
RATE_KEEP_FREE = 60  # of Alpaca's 200 requests a minute, left for the bots' quotes and orders while reading bars
MAX_QUOTE_SPREAD = 0.01  # a bid/ask wider than 1% of its midpoint isn't a price to trade on (UGA's is usually ~0.3%)
RETRIES_429 = 2  # a request Alpaca refused for the rate goes again this many times, each once its minute starts over
DATA_KEEP_FOR_LIVE = 40  # of the market data's 200 a minute (shared with the live keys): the paper keys wait below this
MAX_RATE_WAIT = 60.0  # seconds; Alpaca's budgets start over every minute
SNAPSHOT_CHUNK = 200  # symbols per snapshots request (220 came back in one answer of ~130 KB, 2026-10-14)
ROUND_TTL = 30.0  # seconds a pass's prices and positions are used; a longer pass (a check waiting on Yahoo) asks again
PAPER_KEEP_FREE = 10  # of the paper account's 200 trading requests a minute: below this its requests wait for the next one
BUY_COST_MARGIN = 0.02  # a buy by shares may fill this much above the pass's price (what it takes off the buying power)
ORDERS_PAGE = 500  # orders per request when a pass reads the paper account's recent orders (Alpaca's most)
FEES_PAGE = 100  # fee lines per request (fees.py: two a trading day at most)
ORDERS_MAX_PAGES = 4  # older ones are looked up one by one, as before

# Alpaca order states that mean "still working" vs "finished"
_TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "stopped", "suspended"}


class _Budget:
    """What Alpaca's latest answer said about one rate budget: requests left this minute, and when it starts over."""

    def __init__(self) -> None:
        self.left: int | None = None
        self.reset = 0.0  # unix time


# "data" (paper and live keys together), "paper" and "live" (each account's trading API)
_budgets = {"data": _Budget(), "paper": _Budget(), "live": _Budget()}
_budgets_lock = threading.Lock()


def _note(name: str, headers: httpx.Headers) -> None:
    left, reset = headers.get("x-ratelimit-remaining"), headers.get("x-ratelimit-reset")
    try:
        left_n, reset_t = int(left), float(reset) if reset else time.time() + 60
    except (TypeError, ValueError):
        return  # an answer without the headers (or garbled ones) tells nothing
    with _budgets_lock:
        _budgets[name].left, _budgets[name].reset = left_n, reset_t


def _make_room(name: str, keep: int) -> None:
    """Wait for the next minute while the last answer left fewer than `keep` requests of this budget."""
    with _budgets_lock:
        b = _budgets[name]
        wait = b.reset - time.time() if b.left is not None and b.left < keep else 0.0
    if wait > 0:
        time.sleep(min(MAX_RATE_WAIT, wait))


def _rate_wait(headers: httpx.Headers) -> float:
    """How long to wait after a 429: until the minute starts over (Alpaca says when), at least a second."""
    try:
        wait = float(headers["x-ratelimit-reset"]) - time.time()
    except (KeyError, ValueError):
        wait = 10.0
    return max(1.0, min(MAX_RATE_WAIT, wait))


def reset_rate_state() -> None:
    """Forget what Alpaca said about the budgets and the assets (tests start from a clean slate)."""
    with _budgets_lock:
        for b in _budgets.values():
            b.left, b.reset = None, 0.0
    _assets.clear()


_assets: dict[str, tuple[date, bool]] = {}  # the paper account: symbol -> (the New York day it was read, fractionable?)
_asset_locks: dict[str, threading.Lock] = {}  # one per symbol: bots buying it at the same moment wait for one answer
_asset_locks_guard = threading.Lock()


def _asset_lock(symbol: str) -> threading.Lock:
    with _asset_locks_guard:
        return _asset_locks.setdefault(symbol, threading.Lock())


def _today() -> date:
    return ny_date(datetime.now(timezone.utc))


_scheduled: contextvars.ContextVar[bool] = contextvars.ContextVar("alpaca_scheduled", default=False)


@contextmanager
def scheduled() -> Iterator[None]:
    """Inside (the scheduler's dip check of one bot, in its own thread): the paper account's orders don't wait for their
    fills. The order is saved as sent; the next pass books its fill (trader.sync_pending). Orders sent from the API, and
    the live account's, still wait, so the dashboard shows the fill at once."""
    token = _scheduled.set(True)
    try:
        yield
    finally:
        _scheduled.reset(token)


class _Round:
    """One scheduler pass (scheduler.tick): every bot on Alpaca reads its holdings' prices and checks the account's
    positions every few minutes. One request per bot and symbol would be ~8 a bot (38 bots: ~260 at once, over the 200 a
    minute at ~100 bots). Within a round the first price asked reads ALL the symbols the bots need (`symbols`) in one
    snapshots request (a few for many symbols: SNAPSHOT_CHUNK), and the first cached position asked reads the account's
    whole positions list in one request; the rest are answered from those for ROUND_TTL seconds. An order seen filling
    (or ending) on an account drops that account's positions (asked again on the next read); prices aren't affected.
    An order still working doesn't: while it works, the account checks leave its symbol alone (trader.others_hold),
    and its fill is only booked through a lookup, which drops them.
    The paper account also gets (see the module's doc): one read of what the bots may spend, less each buy sent since,
    and one read of its orders since `orders_since` (the oldest one still working by our records) for the lookups."""

    def __init__(self, symbols: Iterable[str], orders_since: datetime | None = None) -> None:
        self.symbols = sorted(set(symbols))
        self.orders_since = orders_since
        self._prices: dict[str, tuple[float, Quote | None, bool]] = {}  # symbol -> (read at, its price, in the answer?)
        self._positions: dict[bool, tuple[float, dict[str, float]]] = {}  # live? -> (read at, shares per symbol)
        self._spendable: tuple[float, float] | None = None  # the paper account: (read at, what the bots may still spend)
        self._orders: tuple[float, dict[str, BrokerOrder]] | None = None  # the paper account: (read at, by our order id)
        self._prices_lock = threading.Lock()
        self._positions_locks = {False: threading.Lock(), True: threading.Lock()}  # one per account: neither waits for the other
        self._account_lock = threading.Lock()
        self._orders_lock = threading.Lock()

    @staticmethod
    def _fresh(at: float) -> bool:
        return time.monotonic() - at < ROUND_TTL

    def quote(self, broker: "AlpacaBroker", symbol: str) -> Quote:
        if not broker.live:  # a paper bot waits for room before the lock: a live bot asking meanwhile reads for both
            _make_room("data", DATA_KEEP_FOR_LIVE)
        with self._prices_lock:
            kept = self._prices.get(symbol)
            if kept is None or not self._fresh(kept[0]):
                stale = [s for s in self.symbols if s != symbol and not (s in self._prices and self._fresh(self._prices[s][0]))]
                need = [symbol, *stale]
                for i in range(0, len(need), SNAPSHOT_CHUNK):
                    chunk = need[i:i + SNAPSHOT_CHUNK]
                    try:
                        snaps = broker._snapshots(chunk)
                    except BrokerError:
                        if i == 0:
                            raise  # the one asked for: the caller sees why (nothing kept, the next ask tries again)
                        break  # the others are read when asked
                    at = time.monotonic()
                    for s in chunk:
                        self._prices[s] = (at, _snapshot_price(snaps.get(s) or {}), s in snaps)
                kept = self._prices[symbol]
        _, price, found = kept
        if price is None:
            raise BrokerError(f"Alpaca quote {symbol}: no IEX trade and no usable bid/ask" if found
                              else f"Alpaca quote {symbol}: no snapshot (not a symbol Alpaca has?)")
        return price

    def position(self, broker: "AlpacaBroker", symbol: str) -> float:
        with self._positions_locks[broker.live]:
            kept = self._positions.get(broker.live)
            if kept is None or not self._fresh(kept[0]):
                kept = self._positions[broker.live] = (time.monotonic(), broker._positions())
        return kept[1].get(symbol, 0.0)

    def forget_positions(self, live: bool) -> None:
        with self._positions_locks[live]:
            self._positions.pop(live, None)

    def join(self, symbols: Iterable[str], orders_since: datetime | None) -> None:
        """A second pass at the same time (scheduler.run_forever) uses this round too: its symbols are priced with the
        rest, and the paper orders are read from the earlier of the two moments."""
        with self._prices_lock:
            self.symbols = sorted(set(self.symbols) | set(symbols))
        with self._orders_lock:
            if orders_since is not None and (self.orders_since is None or orders_since < self.orders_since):
                self.orders_since, self._orders = orders_since, None

    def price_of(self, symbol: str) -> float | None:
        """The pass's price of `symbol`, if it read one."""
        kept = self._prices.get(symbol)
        return kept[1].price if kept and kept[1] is not None else None

    def spendable(self, broker: "AlpacaBroker") -> float:
        """The paper account's buying_power: read once, then less each buy sent since (spent), for ROUND_TTL seconds.
        It never says more than a fresh read would: Alpaca's own figure already counts the orders still open when it
        was read, each buy since comes off at what it may cost, and a sale's money only counts at the next read."""
        with self._account_lock:  # held while reading: a buy sent meanwhile comes off after the read, never before it
            if self._spendable is None or not self._fresh(self._spendable[0]):
                self._spendable = (time.monotonic(), broker._spendable(broker._account()))
            return self._spendable[1]

    def spent(self, cost: float | None) -> None:
        """A buy went out on the paper account: take what it may cost off. None: unknown (the account is read again)."""
        with self._account_lock:
            if self._spendable is not None:
                self._spendable = None if cost is None else (self._spendable[0], self._spendable[1] - cost)

    def order(self, broker: "AlpacaBroker", client_order_id: str) -> BrokerOrder | None:
        """A paper order's state from the pass's one read of the account's orders since `orders_since`, or None when it
        isn't there (older, sent after the read, or the read failed): the caller then looks that one up."""
        if self.orders_since is None:
            return None
        with self._orders_lock:
            if self._orders is None or not self._fresh(self._orders[0]):
                try:
                    found = broker._recent_orders(self.orders_since)
                except BrokerError:
                    found = {}  # each order is looked up on its own, as before; the list is tried again in half a minute
                self._orders = (time.monotonic(), found)
            return self._orders[1].get(client_order_id)


_round: _Round | None = None
_round_users = 0  # the passes inside it: the live bots' and the others' run side by side (scheduler.run_forever)
_round_guard = threading.Lock()


@contextmanager
def shared_round(symbols: Iterable[str], orders_since: datetime | None = None) -> Iterator[None]:
    """Inside: the bots' prices, and the positions they ask for with cached=True, come from one read for all (_Round).
    `symbols`: every symbol the bots may ask a price of (their holdings and watchlists); another one is read on its own.
    `orders_since`: when the paper account's oldest order still working by our records was made (None: none is), so
    the lookups with cached=True come from one read of its orders since then.
    A pass that starts while another is inside joins its round (_Round.join); the last one out ends it. Every read in
    it is re-done after ROUND_TTL anyway, however long the passes together keep it."""
    global _round, _round_users
    with _round_guard:
        if _round is None:
            _round = _Round(symbols, orders_since)
        else:
            _round.join(symbols, orders_since)
        _round_users += 1
    try:
        yield
    finally:
        with _round_guard:
            _round_users -= 1
            if _round_users == 0:
                _round = None


def _forget_positions(live: bool) -> None:
    if (r := _round) is not None:
        r.forget_positions(live)


def _qty(qty: float) -> str:
    """A quantity as Alpaca reads it: "12", or "0.142857" (it takes up to 9 decimals)."""
    return f"{qty:.9f}".rstrip("0").rstrip(".")


def _snapshot_price(snap: dict) -> Quote | None:
    """A snapshot's price (see AlpacaBroker.quote): the latest trade, or the bid/ask midpoint once that is
    MAX_QUOTE_AGE_MINUTES newer than the trade. None: no trade and no usable bid/ask."""
    t, q = snap.get("latestTrade") or {}, snap.get("latestQuote") or {}
    trade = Quote(price=float(t["p"]), at=to_utc(t["t"])) if t.get("p") else None
    bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    mid = None
    if bid > 0 and ask >= bid and (ask - bid) / ((ask + bid) / 2) <= MAX_QUOTE_SPREAD:
        mid = Quote(price=round((bid + ask) / 2, 4), at=to_utc(q["t"]))
    if mid and (trade is None or mid.at - trade.at > timedelta(minutes=settings.max_quote_age_minutes)):
        return mid
    return trade


class AlpacaBroker(Broker):
    supports_stop_orders = True  # the stop-loss rests at Alpaca, so it works while this service is down
    supports_notional = True  # a fractional BUY can be a dollar amount ("notional"): it never costs more than that

    def __init__(self, key_id: str, secret_key: str, live: bool = False,
                 transport: httpx.BaseTransport | None = None, fill_wait_seconds: float = 10.0):
        self.live = live
        self.name = "alpaca-live" if live else "alpaca-paper"
        headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        # transport is only set by tests (httpx.MockTransport) so no real request is ever made there
        self._api = httpx.Client(base_url=LIVE_URL if live else PAPER_URL, headers=headers, timeout=15, transport=transport)
        self._data = httpx.Client(base_url=DATA_URL, headers=headers, timeout=15, transport=transport)
        self.fill_wait_seconds = fill_wait_seconds

    def _send(self, client: httpx.Client, method: str, path: str, **kw) -> httpx.Response:
        """Every request to Alpaca. A 429 (over the rate) waits for the next minute and goes again, RETRIES_429 times;
        the paper keys' market data requests wait while fewer than DATA_KEEP_FOR_LIVE are left, and the paper account's
        trading requests while fewer than PAPER_KEEP_FREE are (see the module's doc): a burst of orders slows down to the
        rate instead of running into 429s. Network errors (httpx.HTTPError) are the caller's to word."""
        name = "data" if client is self._data else "live" if self.live else "paper"
        retried = 0
        while True:
            if name == "data" and not self.live:
                _make_room("data", DATA_KEEP_FOR_LIVE)
            elif name == "paper":
                _make_room("paper", PAPER_KEEP_FREE)
            r = client.request(method, path, **kw)
            _note(name, r.headers)
            if r.status_code != 429 or retried == RETRIES_429:
                return r
            retried += 1
            time.sleep(_rate_wait(r.headers))

    def _get(self, client: httpx.Client, path: str, **params) -> httpx.Response:
        try:
            return self._send(client, "GET", path, params=params or None)
        except httpx.HTTPError as e:
            raise BrokerError(f"Alpaca unreachable: {e}") from e

    def quote(self, symbol: str) -> Quote:
        """The latest trade, or the bid/ask midpoint once the bid/ask is MAX_QUOTE_AGE_MINUTES newer than that trade.
        In a scheduler pass (shared_round) it comes from the pass's one read of all the bots' symbols.

        feed=iex is the free real-time feed: one exchange, a few % of the volume. A thinly traded ETF (UGA, DBE) can go
        days without an IEX trade while IEX quotes it all day, and its last trade can be far from today's price. The
        midpoint counts only with both sides there and a spread of at most MAX_QUOTE_SPREAD (after the close the book is
        one-sided: then the trade stays, and the caller sees how old it is)."""
        if (r := _round) is not None:
            return r.quote(self, symbol)
        r = self._get(self._data, f"/v2/stocks/{symbol}/snapshot", feed="iex")  # the latest trade and bid/ask, one call
        if r.status_code != 200:
            raise BrokerError(f"Alpaca quote {symbol}: {r.status_code} {r.text[:200]}")
        price = _snapshot_price(r.json())
        if price is None:
            raise BrokerError(f"Alpaca quote {symbol}: no IEX trade and no usable bid/ask")
        return price

    def _snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """The latest trade and bid/ask of many symbols in one call: {symbol: snapshot}; an unknown symbol isn't there."""
        r = self._get(self._data, "/v2/stocks/snapshots", symbols=",".join(symbols), feed="iex")
        if r.status_code != 200:
            raise BrokerError(f"Alpaca quotes: {r.status_code} {r.text[:200]}")
        return {s: snap for s, snap in (r.json() or {}).items() if snap}

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """`quote` for many symbols in one call; a symbol without a usable price is left out."""
        out = {}
        for s, snap in self._snapshots(symbols).items():
            price = _snapshot_price(snap)
            if price is not None:
                out[s] = price
        return out

    def bars(self, symbols: list[str], timeframe: str, start: date | datetime, end: datetime, feed: str = "sip",
             adjustment: str = "all") -> dict[str, list[tuple[datetime, float]]]:
        """Each symbol's bar closes from `start` (a day, or a moment) to `end`: {symbol: [(the bar's start, UTC; close)]},
        extended hours included, adjusted for splits and dividends ("all") or splits only ("split"). feed="sip" = every US exchange; the free
        plan has it only for bars at least 15 minutes old (end earlier than that). Alpaca gives about 1,000 bars a page
        and 200 requests a minute per account: when fewer than RATE_KEEP_FREE are left, it waits for the next minute,
        so the bots' quotes and orders on the same keys still go through."""
        out: dict[str, list[tuple[datetime, float]]] = {}
        token = None
        utc = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for _ in range(BARS_MAX_PAGES):
            params = dict(symbols=",".join(symbols), timeframe=timeframe,
                          start=utc(start) if isinstance(start, datetime) else start.isoformat(), end=utc(end), feed=feed,
                          adjustment=adjustment, limit=10_000)
            if token:
                params["page_token"] = token
            r = self._get(self._data, "/v2/stocks/bars", **params)
            if r.status_code != 200:
                raise BrokerError(f"Alpaca bars: {r.status_code} {r.text[:200]}")
            j = r.json()
            for s, rows in (j.get("bars") or {}).items():
                out.setdefault(s, []).extend((to_utc(b["t"]), float(b["c"])) for b in rows)
            left, reset = r.headers.get("x-ratelimit-remaining"), r.headers.get("x-ratelimit-reset")
            if left is not None and int(left) < RATE_KEEP_FREE:
                time.sleep(max(0.0, min(60.0, float(reset) - time.time() if reset else 60.0)))
            token = j.get("next_page_token")
            if not token:
                return out
        raise BrokerError(f"Alpaca bars: more than {BARS_MAX_PAGES} pages")

    def submit(self, symbol: str, side: str, qty: float, client_order_id: str, notional: float | None = None) -> BrokerOrder:
        # A dollar amount, or shares (a fraction too: Alpaca takes both on market orders good for the day, like this
        # one). By dollars the fill can't cost more than that even when the price moved since the quote
        notional = notional if notional is not None and side == "BUY" else None
        size = {"notional": f"{notional:.2f}"} if notional is not None else {"qty": _qty(qty)}
        try:
            order = self._post_order({
                "symbol": symbol,
                **size,
                "side": side.lower(),
                "type": "market",
                "time_in_force": "day",  # an unfilled order expires at the close instead of lingering overnight
                "client_order_id": client_order_id,
            })
        except BrokerError:
            if side == "BUY":
                self._spent(symbol, qty, notional, None)  # it may have gone out: the account is read again
            raise
        if side == "BUY" and order.status != "rejected":
            self._spent(symbol, qty, notional, order)
        if not self.live and _scheduled.get():
            return order  # a scheduled dip check on the paper account: the next pass books the fill (see scheduled)
        # Market orders usually fill within a second or two; wait briefly so the UI shows the fill at once.
        # Anything still open after that is picked up by the scheduler's pending-order sync.
        return self._wait(client_order_id, order)

    def _spent(self, symbol: str, qty: float, notional: float | None, order: BrokerOrder | None) -> None:
        """A paper buy went out in a pass: what it may cost comes off the pass's buying power (_Round.spendable): the
        dollars, or the shares at the pass's price plus BUY_COST_MARGIN, or what it did cost once filled. Unknown
        (sent without a price, or maybe not sent at all): the account is read again."""
        if self.live or (rd := _round) is None:
            return
        price = rd.price_of(symbol)
        if order is not None and order.filled_qty > 0 and order.avg_price:
            cost = order.filled_qty * order.avg_price
        elif order is None:
            cost = None
        elif notional is not None:
            cost = notional
        else:
            cost = qty * price * (1 + BUY_COST_MARGIN) if price else None
        rd.spent(cost)

    def submit_stop(self, symbol: str, qty: int, stop_price: float, client_order_id: str) -> BrokerOrder:
        # A stop (market) order: once a trade prints at or below stop_price it becomes a market sell.
        # gtc = stays until filled or canceled (Alpaca cancels GTC orders after 90 days; trader.protect
        # then simply places a new one). Only triggers in regular hours, like the service's own check.
        return self._post_order({
            "symbol": symbol,
            "qty": _qty(qty),
            "side": "sell",
            "type": "stop",
            "stop_price": f"{stop_price:.2f}" if stop_price >= 1 else f"{stop_price:.4f}",  # Alpaca's price increments
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        })

    def cancel(self, client_order_id: str) -> BrokerOrder | None:
        order = self.lookup(client_order_id)
        if order is None or order.status != "submitted":
            return order  # never got it, or already finished (filled / canceled): nothing to cancel
        try:
            r = self._send(self._api, "DELETE", f"/v2/orders/{order.broker_order_id}")
        except httpx.HTTPError as e:
            raise BrokerError(f"Alpaca unreachable while canceling: {e}") from e
        _forget_positions(self.live)
        # 204 = cancel accepted (it completes asynchronously). 422 = no longer cancelable, typically because it
        # is filling right now. Either way the order's real state is read back below.
        if r.status_code not in (200, 204, 422):
            raise BrokerError(f"Alpaca cancel {r.status_code}: {r.text[:200]}")
        return self._wait(client_order_id, self.lookup(client_order_id) or order)

    def _post_order(self, body: dict) -> BrokerOrder:
        try:
            # A 429 is sent again by _send: Alpaca refused it before taking it, so that is no blind resend
            r = self._send(self._api, "POST", "/v2/orders", json=body)
        except httpx.HTTPError as e:
            # We don't know whether Alpaca got it. Raise: the order stays "new" and the scheduler
            # later asks lookup(client_order_id) -- never blindly resend.
            raise BrokerError(f"Alpaca unreachable while submitting: {e}") from e
        if r.status_code in (403, 422):  # e.g. insufficient buying power, market closed, bad symbol
            return BrokerOrder(status="rejected", error=f"Alpaca {r.status_code}: {r.text[:300]}")
        if r.status_code >= 300:
            _forget_positions(self.live)  # it may have gone out after all
            raise BrokerError(f"Alpaca submit {r.status_code}: {r.text[:300]}")
        order = self._parse(r.json())
        if order.status != "submitted" or order.filled_qty > 0:
            _forget_positions(self.live)  # filled at once: the account holds something else (still working: see _Round)
        return order

    def _wait(self, client_order_id: str, order: BrokerOrder) -> BrokerOrder:
        """Poll for up to fill_wait_seconds while the order is still working; return the latest state."""
        deadline = time.monotonic() + self.fill_wait_seconds
        while order.status == "submitted" and time.monotonic() < deadline:
            time.sleep(1)
            order = self.lookup(client_order_id) or order
        return order

    def lookup(self, client_order_id: str, cached: bool = False) -> BrokerOrder | None:
        order = None
        if cached and not self.live and (rd := _round) is not None:
            order = rd.order(self, client_order_id)  # the pass's one read of the paper account's recent orders
        if order is None:
            r = self._get(self._api, "/v2/orders:by_client_order_id", client_order_id=client_order_id)
            if r.status_code == 404:
                return None  # Alpaca never got it (only this request says so: a list without it doesn't)
            if r.status_code != 200:
                raise BrokerError(f"Alpaca order lookup {r.status_code}: {r.text[:200]}")
            order = self._parse(r.json())
        if order.status != "submitted" or order.filled_qty > 0:
            _forget_positions(self.live)  # it filled (or ended): the round's read of the account may be from before
        return order

    def _recent_orders(self, since: datetime) -> dict[str, BrokerOrder]:
        """The account's orders submitted after `since`, by our client_order_id, oldest first, ORDERS_PAGE a request
        (at most ORDERS_MAX_PAGES: the rest are looked up one by one)."""
        out: dict[str, BrokerOrder] = {}
        after = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for _ in range(ORDERS_MAX_PAGES):
            r = self._get(self._api, "/v2/orders", status="all", after=after, direction="asc", limit=ORDERS_PAGE)
            if r.status_code != 200:
                raise BrokerError(f"Alpaca orders {r.status_code}: {r.text[:200]}")
            page = r.json() or []
            for o in page:
                if o.get("client_order_id"):
                    out[o["client_order_id"]] = self._parse(o)
            if len(page) < ORDERS_PAGE or not page[-1].get("submitted_at"):
                break
            after = page[-1]["submitted_at"]  # the next page: after the last one (Alpaca's own timestamp, as it gave it)
        return out

    def _account(self) -> dict:
        r = self._get(self._api, "/v2/account")
        if r.status_code != 200:
            raise BrokerError(f"Alpaca account {r.status_code}: {r.text[:200]}")
        return r.json()

    @staticmethod
    def _spendable(a: dict) -> float:
        """See buying_power: the smallest of the cash, the non-margin buying power and the buying power."""
        found = [float(a[k]) for k in ("cash", "non_marginable_buying_power", "buying_power") if a.get(k) is not None]
        if not found:
            raise BrokerError("Alpaca account: no cash or buying power in the answer")
        return min(found)

    def buying_power(self) -> float | None:
        """The account's own money only, never a margin loan. Every Alpaca account is a margin account, and from $2,000
        of equity its buying_power counts borrowed money (2x, more intraday). So: the smallest of the cash, the
        non-margin buying power and the buying power (open orders and settlement rules included). Negative cash (a loan
        already taken) buys nothing. After the close the non-margin buying power leaves out the day's sales until they
        settle (T+1), so it can sit well under the cash overnight.
        In a scheduler pass the paper account is read once, less each buy sent since (_Round.spendable)."""
        if not self.live and (rd := _round) is not None:
            return rd.spendable(self)
        return self._spendable(self._account())

    def funds(self) -> dict | None:
        """One read of the account for the dashboard's free cash: its cash, Alpaca's buying power and non-margin buying
        power as it gives them (None when missing), and what the bots may spend of it (`spendable`, as buying_power)."""
        a = self._account()
        num = {k: float(a[k]) if a.get(k) is not None else None for k in ("cash", "buying_power", "non_marginable_buying_power")}
        return num | {"spendable": self._spendable(a)}

    def fees(self, since: date) -> list[dict]:
        """The fees Alpaca took from the account from the day `since` on (its activity list, type FEE), oldest first:
        {"id", "day", "amount" (what it took: 0.01; a refund is negative), "description"}. Alpaca charges them per day
        for the whole account ("CAT fee for proceed of 25 trades on 2026-10-20", "REG fee for proceed of $452.6 on
        ..."); orders come back without them (fee=0.0 in _parse). Canceled ones are left out."""
        out, token = [], None
        while True:
            # `after` leaves that day itself out: ask from the day before
            params = {"after": (since - timedelta(days=1)).isoformat(), "direction": "asc", "page_size": FEES_PAGE}
            r = self._get(self._api, "/v2/account/activities/FEE", **params, **({"page_token": token} if token else {}))
            if r.status_code != 200:
                raise BrokerError(f"Alpaca fees {r.status_code}: {r.text[:200]}")
            page = r.json()
            out += [{"id": a["id"], "day": date.fromisoformat(str(a["date"])[:10]), "amount": -float(a["net_amount"]),
                     "description": a.get("description") or ""} for a in page if a.get("status") != "canceled"]
            if len(page) < FEES_PAGE:
                return out
            token = page[-1]["id"]

    def position_qty(self, symbol: str, cached: bool = False) -> float | None:
        if cached and (rd := _round) is not None:
            return rd.position(self, symbol)
        r = self._get(self._api, f"/v2/positions/{symbol}")
        if r.status_code == 404:
            return 0  # Alpaca answers 404 when you hold none
        if r.status_code != 200:
            raise BrokerError(f"Alpaca position {r.status_code}: {r.text[:200]}")
        return float(r.json()["qty"])

    def _positions(self) -> dict[str, float]:
        """The account's shares of every symbol it holds, in one request (one it doesn't hold isn't there)."""
        r = self._get(self._api, "/v2/positions")
        if r.status_code != 200:
            raise BrokerError(f"Alpaca positions {r.status_code}: {r.text[:200]}")
        return {p["symbol"]: float(p["qty"]) for p in r.json()}

    def fractionable(self, symbol: str) -> bool:
        """Asked once a day per symbol on the paper account (a stale yes only gets a dollar order rejected); every time
        on the live one. Several bots buying one dip at the same moment (the scheduler's threads) wait for the first
        one's answer instead of each asking; bots asking about other symbols don't wait."""
        if self.live:
            return self._asset_fractionable(symbol)
        today = _today()
        with _asset_lock(symbol):
            if (kept := _assets.get(symbol)) is not None and kept[0] == today:
                return kept[1]
            split = self._asset_fractionable(symbol)
            _assets[symbol] = (today, split)
            return split

    def _asset_fractionable(self, symbol: str) -> bool:
        r = self._get(self._api, f"/v2/assets/{symbol}")
        if r.status_code != 200:
            raise BrokerError(f"Alpaca asset {symbol}: {r.status_code} {r.text[:200]}")
        return bool(r.json().get("fractionable"))

    @staticmethod
    def _parse(o: dict) -> BrokerOrder:
        """Map Alpaca's many order states onto our small set."""
        status = o["status"]
        filled = float(o.get("filled_qty") or 0)
        avg = float(o["filled_avg_price"]) if o.get("filled_avg_price") else None
        if status == "filled":
            ours = "filled"
        elif status == "rejected":
            ours = "rejected"
        elif status in _TERMINAL:
            # canceled / expired / done_for_day: whatever filled before that is real and must be booked
            ours = "partially_filled" if filled > 0 else "canceled"
        else:
            ours = "submitted"  # new, accepted, pending_new, partially_filled (still working), ...
        return BrokerOrder(status=ours, filled_qty=filled, avg_price=avg, fee=0.0, broker_order_id=o.get("id"))
