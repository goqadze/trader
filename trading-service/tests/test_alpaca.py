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


def test_a_fraction_of_a_share_is_sent_and_read_back_as_a_fraction():
    import json

    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json=_order("filled", "0.728155", "103.01"))

    o = _broker(handler).submit("AAPL", "BUY", 0.728155, "x")
    assert sent["qty"] == "0.728155" and sent["time_in_force"] == "day"  # fractions: market orders good for the day
    assert o.filled_qty == 0.728155
    _broker(handler).submit("AAPL", "SELL", 0.1 + 0.2, "y")
    assert sent["qty"] == "0.3"  # never Python's 0.30000000000000004: Alpaca takes 9 decimals at most
    assert _broker(lambda req: httpx.Response(200, json={"qty": "0.5"})).position_qty("AAPL") == 0.5


def test_fractionable_asks_the_asset():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"symbol": "BRK.A", "fractionable": req.url.path.endswith("/AAPL")})

    assert _broker(handler).fractionable("AAPL") is True and _broker(handler).fractionable("BRK.A") is False
    assert seen == ["/v2/assets/AAPL", "/v2/assets/BRK.A"]
    with pytest.raises(BrokerError):
        _broker(lambda req: httpx.Response(500, text="oops")).fractionable("AAPL")


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


# --- Stop orders held at the broker ----------------------------------------------------------------

def test_submit_stop_rests_a_good_till_canceled_sell_stop():
    import json

    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json=_order("accepted"))

    o = _broker(handler).submit_stop("AAPL", 10, 95.2, "bot1-stop")
    assert sent == {"symbol": "AAPL", "qty": "10", "side": "sell", "type": "stop", "stop_price": "95.20",
                    "time_in_force": "gtc", "client_order_id": "bot1-stop"}
    assert o.status == "submitted"  # resting: no waiting for a fill


def test_cancel_deletes_by_alpacas_id_then_reads_the_final_state_back():
    calls, state = [], {"status": "accepted"}

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "DELETE":
            state["status"] = "canceled"
            return httpx.Response(204)
        return httpx.Response(200, json=_order(state["status"]))

    o = _broker(handler).cancel("bot1-stop")
    assert calls == [("GET", "/v2/orders:by_client_order_id"), ("DELETE", "/v2/orders/alp-1"),
                     ("GET", "/v2/orders:by_client_order_id")]
    assert o.status == "canceled"


def test_cancel_of_an_order_that_already_filled_returns_the_fill():
    calls = []

    def handler(req):
        calls.append(req.method)
        return httpx.Response(200, json=_order("filled", "10", "95.1"))

    o = _broker(handler).cancel("x")
    assert calls == ["GET"]  # nothing to cancel
    assert (o.status, o.filled_qty, o.avg_price) == ("filled", 10, 95.1)


def test_cancel_refused_because_the_stop_is_filling_reports_the_fill():
    reads = []

    def handler(req):
        if req.method == "DELETE":
            return httpx.Response(422, json={"message": "order is not cancelable"})
        reads.append(1)
        return httpx.Response(200, json=_order("accepted") if len(reads) == 1 else _order("filled", "10", "95.1"))

    assert _broker(handler).cancel("x").status == "filled"
