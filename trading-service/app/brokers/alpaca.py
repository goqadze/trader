"""Alpaca broker (https://alpaca.markets): commission-free US stocks with a free paper-trading account
that behaves like the real one. Paper and live use the same code; only the URL and keys differ.

Docs: https://docs.alpaca.markets/reference (Trading API v2 + Market Data v2).
"""

import time
from datetime import date, datetime, timedelta, timezone

import httpx

from ..config import settings
from ..market import to_utc
from .base import Broker, BrokerError, BrokerOrder, Quote

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
BARS_MAX_PAGES = 400  # of about 1,000 bars
RATE_KEEP_FREE = 60  # of Alpaca's 200 requests a minute, left for the bots' quotes and orders while reading bars
MAX_QUOTE_SPREAD = 0.01  # a bid/ask wider than 1% of its midpoint isn't a price to trade on (UGA's is usually ~0.3%)

# Alpaca order states that mean "still working" vs "finished"
_TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "stopped", "suspended"}


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

    def __init__(self, key_id: str, secret_key: str, live: bool = False,
                 transport: httpx.BaseTransport | None = None, fill_wait_seconds: float = 10.0):
        self.live = live
        self.name = "alpaca-live" if live else "alpaca-paper"
        headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        # transport is only set by tests (httpx.MockTransport) so no real request is ever made there
        self._api = httpx.Client(base_url=LIVE_URL if live else PAPER_URL, headers=headers, timeout=15, transport=transport)
        self._data = httpx.Client(base_url=DATA_URL, headers=headers, timeout=15, transport=transport)
        self.fill_wait_seconds = fill_wait_seconds

    def _get(self, client: httpx.Client, path: str, **params) -> httpx.Response:
        try:
            return client.get(path, params=params or None)
        except httpx.HTTPError as e:
            raise BrokerError(f"Alpaca unreachable: {e}") from e

    def quote(self, symbol: str) -> Quote:
        """The latest trade, or the bid/ask midpoint once the bid/ask is MAX_QUOTE_AGE_MINUTES newer than that trade.

        feed=iex is the free real-time feed: one exchange, a few % of the volume. A thinly traded ETF (UGA, DBE) can go
        days without an IEX trade while IEX quotes it all day, and its last trade can be far from today's price. The
        midpoint counts only with both sides there and a spread of at most MAX_QUOTE_SPREAD (after the close the book is
        one-sided: then the trade stays, and the caller sees how old it is)."""
        r = self._get(self._data, f"/v2/stocks/{symbol}/snapshot", feed="iex")  # the latest trade and bid/ask, one call
        if r.status_code != 200:
            raise BrokerError(f"Alpaca quote {symbol}: {r.status_code} {r.text[:200]}")
        price = _snapshot_price(r.json())
        if price is None:
            raise BrokerError(f"Alpaca quote {symbol}: no IEX trade and no usable bid/ask")
        return price

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """`quote` for many symbols in one call; a symbol without a usable price is left out."""
        r = self._get(self._data, "/v2/stocks/snapshots", symbols=",".join(symbols), feed="iex")
        if r.status_code != 200:
            raise BrokerError(f"Alpaca quotes: {r.status_code} {r.text[:200]}")
        out = {}
        for s, snap in (r.json() or {}).items():
            price = _snapshot_price(snap or {})
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

    def submit(self, symbol: str, side: str, qty: float, client_order_id: str) -> BrokerOrder:
        order = self._post_order({
            "symbol": symbol,
            "qty": _qty(qty),  # may be a fraction: Alpaca takes those on market orders good for the day, like this one
            "side": side.lower(),
            "type": "market",
            "time_in_force": "day",  # an unfilled order expires at the close instead of lingering overnight
            "client_order_id": client_order_id,
        })
        # Market orders usually fill within a second or two; wait briefly so the UI shows the fill at once.
        # Anything still open after that is picked up by the scheduler's pending-order sync.
        return self._wait(client_order_id, order)

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
            r = self._api.delete(f"/v2/orders/{order.broker_order_id}")
        except httpx.HTTPError as e:
            raise BrokerError(f"Alpaca unreachable while canceling: {e}") from e
        # 204 = cancel accepted (it completes asynchronously). 422 = no longer cancelable, typically because it
        # is filling right now. Either way the order's real state is read back below.
        if r.status_code not in (200, 204, 422):
            raise BrokerError(f"Alpaca cancel {r.status_code}: {r.text[:200]}")
        return self._wait(client_order_id, self.lookup(client_order_id) or order)

    def _post_order(self, body: dict) -> BrokerOrder:
        try:
            r = self._api.post("/v2/orders", json=body)
        except httpx.HTTPError as e:
            # We don't know whether Alpaca got it. Raise: the order stays "new" and the scheduler
            # later asks lookup(client_order_id) -- never blindly resend.
            raise BrokerError(f"Alpaca unreachable while submitting: {e}") from e
        if r.status_code in (403, 422):  # e.g. insufficient buying power, market closed, bad symbol
            return BrokerOrder(status="rejected", error=f"Alpaca {r.status_code}: {r.text[:300]}")
        if r.status_code >= 300:
            raise BrokerError(f"Alpaca submit {r.status_code}: {r.text[:300]}")
        return self._parse(r.json())

    def _wait(self, client_order_id: str, order: BrokerOrder) -> BrokerOrder:
        """Poll for up to fill_wait_seconds while the order is still working; return the latest state."""
        deadline = time.monotonic() + self.fill_wait_seconds
        while order.status == "submitted" and time.monotonic() < deadline:
            time.sleep(1)
            order = self.lookup(client_order_id) or order
        return order

    def lookup(self, client_order_id: str) -> BrokerOrder | None:
        r = self._get(self._api, "/v2/orders:by_client_order_id", client_order_id=client_order_id)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise BrokerError(f"Alpaca order lookup {r.status_code}: {r.text[:200]}")
        return self._parse(r.json())

    def buying_power(self) -> float | None:
        """The account's own money only, never a margin loan. Every Alpaca account is a margin account, and from $2,000
        of equity its buying_power counts borrowed money (2x, more intraday). So: the smallest of the cash, the
        non-margin buying power and the buying power (open orders and settlement rules included). Negative cash (a loan
        already taken) buys nothing."""
        r = self._get(self._api, "/v2/account")
        if r.status_code != 200:
            raise BrokerError(f"Alpaca account {r.status_code}: {r.text[:200]}")
        a = r.json()
        found = [float(a[k]) for k in ("cash", "non_marginable_buying_power", "buying_power") if a.get(k) is not None]
        if not found:
            raise BrokerError("Alpaca account: no cash or buying power in the answer")
        return min(found)

    def position_qty(self, symbol: str) -> float | None:
        r = self._get(self._api, f"/v2/positions/{symbol}")
        if r.status_code == 404:
            return 0  # Alpaca answers 404 when you hold none
        if r.status_code != 200:
            raise BrokerError(f"Alpaca position {r.status_code}: {r.text[:200]}")
        return float(r.json()["qty"])

    def fractionable(self, symbol: str) -> bool:
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
