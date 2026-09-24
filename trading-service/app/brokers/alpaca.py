"""Alpaca broker (https://alpaca.markets): commission-free US stocks with a free paper-trading account
that behaves like the real one. Paper and live use the same code; only the URL and keys differ.

Docs: https://docs.alpaca.markets/reference (Trading API v2 + Market Data v2).
"""

import time

import httpx

from ..market import to_utc
from .base import Broker, BrokerError, BrokerOrder, Quote

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

# Alpaca order states that mean "still working" vs "finished"
_TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "stopped", "suspended"}


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
        # feed=iex is the free real-time feed (a subset of exchanges; fine for a daily strategy)
        r = self._get(self._data, f"/v2/stocks/{symbol}/trades/latest", feed="iex")
        if r.status_code != 200:
            raise BrokerError(f"Alpaca quote {symbol}: {r.status_code} {r.text[:200]}")
        t = r.json()["trade"]
        return Quote(price=float(t["p"]), at=to_utc(t["t"]))

    def submit(self, symbol: str, side: str, qty: int, client_order_id: str) -> BrokerOrder:
        order = self._post_order({
            "symbol": symbol,
            "qty": str(qty),
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
            "qty": str(qty),
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
        r = self._get(self._api, "/v2/account")
        if r.status_code != 200:
            raise BrokerError(f"Alpaca account {r.status_code}: {r.text[:200]}")
        return float(r.json()["buying_power"])

    def position_qty(self, symbol: str) -> int | None:
        r = self._get(self._api, f"/v2/positions/{symbol}")
        if r.status_code == 404:
            return 0  # Alpaca answers 404 when you hold none
        if r.status_code != 200:
            raise BrokerError(f"Alpaca position {r.status_code}: {r.text[:200]}")
        return int(float(r.json()["qty"]))

    @staticmethod
    def _parse(o: dict) -> BrokerOrder:
        """Map Alpaca's many order states onto our small set."""
        status = o["status"]
        filled = int(float(o.get("filled_qty") or 0))
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
