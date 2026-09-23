"""Alpaca adapter against a mocked HTTP API (httpx.MockTransport): no network, no keys, no real orders."""

import httpx
import pytest

from app.brokers.alpaca import AlpacaBroker
from app.brokers.base import BrokerError


def _broker(handler, **kw):
    return AlpacaBroker("key", "secret", transport=httpx.MockTransport(handler), fill_wait_seconds=0, **kw)


def _order(status, filled_qty="0", avg=None):
    return {"id": "alp-1", "status": status, "filled_qty": filled_qty, "filled_avg_price": avg}


def test_paper_and_live_hit_different_hosts():
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(200, json={"buying_power": "1000"})

    _broker(handler).buying_power()
    _broker(handler, live=True).buying_power()
    assert seen == ["paper-api.alpaca.markets", "api.alpaca.markets"]


def test_submit_sends_a_day_market_order_with_our_client_id():
    sent = {}

    def handler(req):
        import json

        sent.update(json.loads(req.content))
        return httpx.Response(200, json=_order("filled", "10", "101.5"))

    o = _broker(handler).submit("AAPL", "BUY", 10, "bot1-abc")
    assert sent == {"symbol": "AAPL", "qty": "10", "side": "buy", "type": "market", "time_in_force": "day",
                    "client_order_id": "bot1-abc"}
    assert (o.status, o.filled_qty, o.avg_price) == ("filled", 10, 101.5)


def test_rejection_is_returned_not_raised():
    o = _broker(lambda req: httpx.Response(403, text="insufficient buying power")).submit("AAPL", "BUY", 10, "x")
    assert o.status == "rejected" and "insufficient" in o.error


def test_network_error_on_submit_raises_so_the_order_gets_reconciled():
    def handler(req):
        raise httpx.ConnectError("down")

    with pytest.raises(BrokerError):
        _broker(handler).submit("AAPL", "BUY", 10, "x")


@pytest.mark.parametrize("alpaca,filled,ours", [
    ("new", "0", "submitted"),
    ("accepted", "0", "submitted"),
    ("partially_filled", "3", "submitted"),  # still working
    ("filled", "10", "filled"),
    ("canceled", "0", "canceled"),
    ("expired", "4", "partially_filled"),  # the 4 shares that filled are real
    ("rejected", "0", "rejected"),
])
def test_status_mapping(alpaca, filled, ours):
    assert AlpacaBroker._parse(_order(alpaca, filled, "100")).status == ours


def test_lookup_unknown_order_returns_none():
    assert _broker(lambda req: httpx.Response(404)).lookup("x") is None


def test_position_qty_404_means_flat():
    assert _broker(lambda req: httpx.Response(404)).position_qty("AAPL") == 0
    assert _broker(lambda req: httpx.Response(200, json={"qty": "12"})).position_qty("AAPL") == 12


def test_quote_parses_latest_trade():
    q = _broker(lambda req: httpx.Response(200, json={"trade": {"p": 201.25, "t": "2025-06-02T19:30:00Z"}})).quote("AAPL")
    assert q.price == 201.25 and q.at.isoformat() == "2025-06-02T19:30:00+00:00"
