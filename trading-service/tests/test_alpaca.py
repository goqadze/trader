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


@pytest.mark.parametrize("account, spendable", [
    ({"cash": "3000", "non_marginable_buying_power": "3000", "buying_power": "6000"}, 3000),  # margin: 2x, not borrowed
    ({"cash": "3000", "non_marginable_buying_power": "2600", "buying_power": "5200"}, 2600),  # $400 of buys still open
    ({"cash": "1500", "non_marginable_buying_power": "1500", "buying_power": "1500"}, 1500),  # under $2,000: no margin
    ({"cash": "-250", "non_marginable_buying_power": "0", "buying_power": "4000"}, -250),  # a loan already: buys nothing
    ({"buying_power": "800"}, 800),  # an older answer without the cash fields
])
def test_the_bots_spend_the_accounts_own_money_never_a_margin_loan(account, spendable):
    assert _broker(lambda req: httpx.Response(200, json=account)).buying_power() == spendable


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


def test_a_buy_by_dollars_sends_the_notional_and_a_sale_stays_shares():
    import json

    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json=_order("filled", "0.115094", "260.81"))

    o = _broker(handler).submit("BE", "BUY", 0.115, "bot14-a", notional=30.0)
    assert sent[0] == {"symbol": "BE", "notional": "30.00", "side": "buy", "type": "market", "time_in_force": "day",
                       "client_order_id": "bot14-a"}  # no qty: Alpaca works the shares out at the fill
    assert (o.filled_qty, o.avg_price) == (0.115094, 260.81)
    _broker(handler).submit("BE", "SELL", 0.115094, "bot14-b", notional=30.0)
    assert "notional" not in sent[1] and sent[1]["qty"] == "0.115094"
    assert _broker(handler).supports_notional


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


def _snapshot(trade=None, bid=0.0, ask=0.0, quoted="2025-06-02T19:30:01Z"):
    """Alpaca's IEX snapshot: the latest trade (p, t) and the latest bid/ask (bp, ap; 0 = that side is empty)."""
    return {"symbol": "X", "latestTrade": {"p": trade[0], "t": trade[1]} if trade else None,
            "latestQuote": {"bp": bid, "ap": ask, "t": quoted}}


def test_quote_is_the_latest_iex_trade_while_it_is_recent():
    seen = []

    def handler(req):
        seen.append((req.url.path, req.url.params.get("feed")))
        return httpx.Response(200, json=_snapshot(("201.25", "2025-06-02T19:29:57.307205784Z"), 201.24, 201.27))

    q = _broker(handler).quote("AAPL")
    assert seen == [("/v2/stocks/AAPL/snapshot", "iex")]
    assert q.price == 201.25 and q.at.isoformat() == "2025-06-02T19:29:57.307205+00:00"  # not the midpoint 201.255


def test_a_thin_etf_without_recent_iex_trades_is_priced_at_its_bid_ask_midpoint():
    # UGA, Oct 12 2026: last IEX trade four days earlier at $150.54, while IEX quoted it all day ~5% lower
    snap = _snapshot(("150.54", "2026-10-08T16:17:11Z"), 142.40, 142.86, quoted="2026-10-12T19:44:58Z")
    q = _broker(lambda req: httpx.Response(200, json=snap)).quote("UGA")
    assert q.price == 142.63 and q.at.isoformat() == "2026-10-12T19:44:58+00:00"
    no_trade_yet = _snapshot(None, 142.40, 142.86, quoted="2026-10-12T19:44:58Z")
    assert _broker(lambda req: httpx.Response(200, json=no_trade_yet)).quote("UGA").price == 142.63


@pytest.mark.parametrize("bid, ask", [
    (140.0, 143.0),  # a 2.1% spread: not a price to trade on
    (142.40, 0),  # one side empty, as after the close
    (0.01, 0),  # a placeholder bid
    (0, 0),  # an empty book
    (0, 142.86),  # an ask alone
    (143.0, 142.0),  # crossed
])
def test_without_a_usable_bid_ask_the_old_trade_stays_and_the_caller_sees_its_age(bid, ask):
    snap = _snapshot(("150.54", "2026-10-08T16:17:11Z"), bid, ask, quoted="2026-10-12T19:44:58Z")
    q = _broker(lambda req: httpx.Response(200, json=snap)).quote("UGA")
    assert q.price == 150.54 and q.at.isoformat() == "2026-10-08T16:17:11+00:00"


def test_no_trade_and_no_usable_bid_ask_or_an_unknown_symbol_is_an_error():
    with pytest.raises(BrokerError, match="no IEX trade and no usable bid/ask"):
        _broker(lambda req: httpx.Response(200, json=_snapshot(None, 142.40, 0))).quote("UGA")
    with pytest.raises(BrokerError, match="404"):
        _broker(lambda req: httpx.Response(404, json={"message": "no snapshot found for ZZZZQ"})).quote("ZZZZQ")


def test_quotes_price_many_symbols_in_one_call_like_quote():
    seen = []

    def handler(req):
        seen.append((req.url.path, req.url.params.get("symbols"), req.url.params.get("feed")))
        return httpx.Response(200, json={
            "AAPL": _snapshot(("201.25", "2025-06-02T19:29:57Z"), 201.24, 201.27, quoted="2025-06-02T19:30:01Z"),
            "UGA": _snapshot(("150.54", "2025-05-29T16:17:11Z"), 142.40, 142.86, quoted="2025-06-02T19:29:58Z"),
            "DBE": _snapshot(None, 30.0, 0)})  # no trade, one-sided book: no price

    got = _broker(handler).quotes(["AAPL", "UGA", "DBE"])
    assert seen == [("/v2/stocks/snapshots", "AAPL,UGA,DBE", "iex")]
    assert {s: q.price for s, q in got.items()} == {"AAPL": 201.25, "UGA": 142.63}


def test_bars_read_every_page_of_adjusted_sip_bars():
    from datetime import date, datetime, timezone

    seen = []
    pages = [{"bars": {"A": [{"t": "2025-06-02T13:30:00Z", "c": 10.5}]}, "next_page_token": "p2"},
             {"bars": {"A": [{"t": "2025-06-02T13:45:00Z", "c": 10.75}], "BRK.B": [{"t": "2025-06-02T13:30:00Z", "c": 480}]},
              "next_page_token": None}]

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=pages[len(seen) - 1])

    got = _broker(handler).bars(["A", "BRK.B"], "15Min", date(2025, 5, 20), datetime(2025, 6, 2, 14, 44, 20, tzinfo=timezone.utc))
    assert seen[0] == {"symbols": "A,BRK.B", "timeframe": "15Min", "start": "2025-05-20", "end": "2025-06-02T14:44:20Z",
                       "feed": "sip", "adjustment": "all", "limit": "10000"}  # splits and dividends unless asked
    assert seen[1]["page_token"] == "p2"
    assert {s: [(t.isoformat(), c) for t, c in rows] for s, rows in got.items()} == {
        "A": [("2025-06-02T13:30:00+00:00", 10.5), ("2025-06-02T13:45:00+00:00", 10.75)],
        "BRK.B": [("2025-06-02T13:30:00+00:00", 480.0)]}
    with pytest.raises(BrokerError, match="403"):
        _broker(lambda req: httpx.Response(403, json={"message": "subscription does not permit querying recent SIP data"})
                ).bars(["A"], "15Min", date(2025, 5, 20), datetime(2025, 6, 2, 14, 59, tzinfo=timezone.utc))


def test_bars_leave_requests_for_the_bots_when_the_minutes_budget_runs_low(monkeypatch):
    from datetime import date, datetime, timezone

    import app.brokers.alpaca as alpaca

    waits = []
    monkeypatch.setattr(alpaca.time, "sleep", waits.append)
    monkeypatch.setattr(alpaca.time, "time", lambda: 1000.0)
    pages = iter([({"bars": {"A": []}, "next_page_token": "p2"}, "150"), ({"bars": {"A": []}, "next_page_token": "p3"}, "59"),
                  ({"bars": {"A": []}, "next_page_token": None}, "58")])

    def handler(req):
        body, left = next(pages)
        return httpx.Response(200, json=body, headers={"x-ratelimit-remaining": left, "x-ratelimit-reset": "1012"})

    _broker(handler).bars(["A"], "15Min", datetime(2025, 6, 2, 14, 0, tzinfo=timezone.utc),
                          datetime(2025, 6, 2, 14, 44, tzinfo=timezone.utc))
    assert waits == [12.0, 12.0]  # 59 and 58 left of 200: wait for the next minute


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
