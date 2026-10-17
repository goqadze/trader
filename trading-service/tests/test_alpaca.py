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


def test_funds_reads_the_account_once_for_its_cash_its_buying_powers_and_what_the_bots_may_spend():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        # After Friday's close: the cash counts the day's sales, the non-margin buying power not until they settle
        return httpx.Response(200, json={"cash": "484.25", "non_marginable_buying_power": "78.56", "buying_power": "484.25"})

    b = _broker(handler, live=True)
    assert b.funds() == {"cash": 484.25, "buying_power": 484.25, "non_marginable_buying_power": 78.56, "spendable": 78.56}
    assert seen == ["/v2/account"]
    assert b.buying_power() == 78.56  # the bots' limit stays the smallest of the three
    assert _broker(lambda req: httpx.Response(200, json={"buying_power": "800"})).funds() == {
        "cash": None, "buying_power": 800.0, "non_marginable_buying_power": None, "spendable": 800.0}


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


def test_fractionable_asks_the_asset_once_a_day_on_the_paper_account(monkeypatch):
    import datetime as dt

    import app.brokers.alpaca as alpaca

    seen, day = [], {"d": dt.date(2025, 6, 2)}
    monkeypatch.setattr(alpaca, "_today", lambda: day["d"])

    def handler(req):
        seen.append(f"{req.url.host.split('.')[0]} {req.url.path}")
        return httpx.Response(200, json={"symbol": "BRK.A", "fractionable": req.url.path.endswith("/AAPL")})

    paper, live = _broker(handler), _broker(handler, live=True)
    assert paper.fractionable("AAPL") is True and paper.fractionable("BRK.A") is False
    assert seen == ["paper-api /v2/assets/AAPL", "paper-api /v2/assets/BRK.A"]
    assert [paper.fractionable("AAPL"), _broker(handler).fractionable("BRK.A")] == [True, False]
    assert len(seen) == 2  # the rest of the day: from memory, for every paper bot
    assert live.fractionable("AAPL") and live.fractionable("AAPL") and seen[2:] == ["api /v2/assets/AAPL"] * 2
    day["d"] = dt.date(2025, 6, 3)
    assert paper.fractionable("AAPL") and seen[-1] == "paper-api /v2/assets/AAPL"  # the next day: asked again
    with pytest.raises(BrokerError):
        _broker(lambda req: httpx.Response(500, text="oops")).fractionable("MSFT")
    assert "MSFT" not in alpaca._assets  # a failed ask leaves nothing behind


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


# --- Rate limits, and a scheduler pass's shared reads ------------------------------------------------

def _priced(symbols):
    """A snapshots answer: $10 each; E has no trade and a one-sided book (no price); ZZZZQ isn't a symbol (left out)."""
    return {s: _snapshot(None, 30.0, 0) if s == "E" else _snapshot(("10.00", "2025-06-02T19:29:57Z"))
            for s in symbols if s != "ZZZZQ"}


def test_a_pass_prices_every_bots_symbols_with_one_request(monkeypatch):
    import app.brokers.alpaca as alpaca

    seen = []

    def handler(req):
        if req.url.path == "/v2/stocks/snapshots":
            seen.append(req.url.params["symbols"])
            return httpx.Response(200, json=_priced(req.url.params["symbols"].split(",")))
        seen.append(req.url.path)
        return httpx.Response(200, json=_snapshot(("10.00", "2025-06-02T19:29:57Z")))

    clock = {"t": 100.0}
    monkeypatch.setattr(alpaca.time, "monotonic", lambda: clock["t"])
    paper, live = _broker(handler), _broker(handler, live=True)
    with alpaca.shared_round(["B", "A", "C", "E", "ZZZZQ", "A"]):
        assert live.quote("C").price == 10.0  # the first ask reads them all (the live bots go first)
        assert [paper.quote(s).price for s in ("A", "B", "C")] == [10.0] * 3
        with pytest.raises(BrokerError, match="no IEX trade and no usable bid/ask"):
            paper.quote("E")
        with pytest.raises(BrokerError, match="no snapshot"):
            paper.quote("ZZZZQ")
        assert paper.quote("D").price == 10.0  # not one of the bots' symbols: read on its own
        assert seen == ["C,A,B,E,ZZZZQ", "D"]
        clock["t"] += alpaca.ROUND_TTL  # a long pass: half a minute later they are read again, together
        paper.quote("A")
        assert seen[-1] == "A,B,C,E,ZZZZQ"
    paper.quote("A")  # outside a pass: one request for one symbol, as always
    assert seen[-1] == "/v2/stocks/A/snapshot" and len(seen) == 4


def test_many_symbols_are_read_a_few_hundred_at_a_time():
    import app.brokers.alpaca as alpaca

    seen, down = [], {"chunk": None}

    def handler(req):
        asked = req.url.params["symbols"].split(",")
        seen.append(len(asked))
        if len(seen) == down["chunk"]:
            return httpx.Response(500, text="oops")
        return httpx.Response(200, json=_priced(asked))

    symbols = [f"S{i:03d}" for i in range(450)]
    b = _broker(handler)
    with alpaca.shared_round(symbols):
        b.quote("S000")
        b.quote("S449")
        assert seen == [200, 200, 50]
    seen.clear()
    down["chunk"] = 2  # the second request fails: the symbol asked for still has its price, the others wait for theirs
    with alpaca.shared_round(symbols):
        assert b.quote("S000").price == 10.0
        assert seen == [200, 200]
        b.quote("S300")  # never read: asked again (with the ones still missing)
        assert seen == [200, 200, 200, 50]


def test_a_pass_reads_each_accounts_positions_once_until_an_order_changes_them(monkeypatch):
    import app.brokers.alpaca as alpaca

    seen = []

    def handler(req):
        account = "live" if req.url.host == "api.alpaca.markets" else "paper"
        seen.append(f"{account} {req.method} {req.url.path} {req.url.params.get('client_order_id', '')}".strip())
        if req.url.path == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": "A", "qty": "2.5"}, {"symbol": "B", "qty": "10"}])
        if req.url.path.startswith("/v2/positions/"):
            return httpx.Response(200, json={"qty": "7"})
        if req.url.path == "/v2/orders":
            return httpx.Response(200, json=_order("filled", "1", "10"))
        working = req.url.params["client_order_id"] == "working"
        return httpx.Response(200, json=_order("accepted") if working else _order("filled", "1", "10"))

    clock = {"t": 100.0}
    monkeypatch.setattr(alpaca.time, "monotonic", lambda: clock["t"])
    paper, live = _broker(handler), _broker(handler, live=True)
    with alpaca.shared_round([]):
        assert [paper.position_qty(s, cached=True) for s in ("A", "B", "C")] == [2.5, 10, 0]
        assert live.position_qty("A", cached=True) == 2.5
        assert paper.position_qty("A") == 7  # without cached: the account itself, for that symbol
        assert seen == ["paper GET /v2/positions", "live GET /v2/positions", "paper GET /v2/positions/A"]
        seen.clear()
        live.submit("A", "BUY", 1, "x")  # an order on the live account drops its read; the paper one stays
        paper.position_qty("B", cached=True)
        live.position_qty("B", cached=True)
        assert seen == ["live POST /v2/orders", "live GET /v2/positions"]
        seen.clear()
        paper.lookup("working")  # still working, nothing filled: nothing changed
        paper.position_qty("A", cached=True)
        paper.lookup("done")  # a fill seen: the account holds something else now
        paper.position_qty("A", cached=True)
        assert seen == ["paper GET /v2/orders:by_client_order_id working", "paper GET /v2/orders:by_client_order_id done",
                        "paper GET /v2/positions"]
        seen.clear()
        clock["t"] += alpaca.ROUND_TTL
        paper.position_qty("A", cached=True)  # half a minute later: read again
        assert seen == ["paper GET /v2/positions"]
    seen.clear()
    paper.position_qty("A", cached=True)  # outside a pass, cached means nothing
    assert seen == ["paper GET /v2/positions/A"]


def test_over_the_rate_alpaca_is_asked_again_once_its_minute_starts_over(monkeypatch):
    import json

    import app.brokers.alpaca as alpaca

    waits, clock = [], {"t": 1000.0}
    monkeypatch.setattr(alpaca.time, "sleep", lambda s: (waits.append(s), clock.update(t=clock["t"] + s)))
    monkeypatch.setattr(alpaca.time, "time", lambda: clock["t"])
    too_many = lambda: httpx.Response(429, json={"message": "too many requests"},
                                      headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1012"})
    answers = iter([too_many(), httpx.Response(200, json={"qty": "3"})])
    assert _broker(lambda req: next(answers)).position_qty("A") == 3
    assert waits == [12.0]

    sent = []

    def orders(req):  # Alpaca refused the order before taking it: sending it again is no blind resend
        sent.append(json.loads(req.content)["client_order_id"])
        return too_many() if len(sent) == 1 else httpx.Response(200, json=_order("filled", "1", "10"))

    assert _broker(orders).submit("A", "BUY", 1, "bot1-x").status == "filled" and sent == ["bot1-x", "bot1-x"]

    asks = []

    def always(req):
        asks.append(1)
        return too_many()

    with pytest.raises(BrokerError, match="429"):  # not forever: after RETRIES_429 more the caller sees it
        _broker(always).quote("A")
    assert len(asks) == alpaca.RETRIES_429 + 1
    no_header = iter([httpx.Response(429), httpx.Response(200, json={"qty": "1"})])
    waits.clear()
    _broker(lambda req: next(no_header)).position_qty("A")
    assert waits == [10.0]  # Alpaca didn't say when: ten seconds


def test_the_paper_keys_leave_the_last_data_requests_of_a_minute_to_the_live_bots(monkeypatch):
    import app.brokers.alpaca as alpaca

    waits, left, clock = [], {"n": "40"}, {"t": 1000.0}
    monkeypatch.setattr(alpaca.time, "sleep", lambda s: (waits.append(s), clock.update(t=clock["t"] + s)))
    monkeypatch.setattr(alpaca.time, "time", lambda: clock["t"])

    def handler(req):  # each answer: requests left of this budget, and its minute ends in 20 s
        body = {"qty": "1"} if "positions" in req.url.path else _snapshot(("10.00", "2025-06-02T19:29:57Z"))
        return httpx.Response(200, json=body,
                              headers={"x-ratelimit-remaining": left["n"], "x-ratelimit-reset": str(clock["t"] + 20)})

    paper, live = _broker(handler), _broker(handler, live=True)
    live.quote("A")
    paper.quote("A")  # 40 of the market data's 200 left: the paper keys go on
    left["n"] = "39"
    paper.quote("A")
    live.quote("A")  # 39 left: the live keys never wait for the paper bots
    paper.position_qty("A")  # the paper account's own trading budget: no reason to wait
    assert waits == []
    paper.quote("A")  # market data with 39 left (the live bots' too): the paper keys wait for the next minute
    assert waits == [20.0] and clock["t"] == 1020.0


def test_a_paper_bot_waiting_for_room_never_holds_up_a_live_bots_prices_or_positions(monkeypatch):
    import app.brokers.alpaca as alpaca

    seen, clock = [], {"t": 1000.0}

    def handler(req):
        account = "live" if req.headers["APCA-API-KEY-ID"] == "live-key" else "paper"
        seen.append(f"{account} {req.url.path}")
        if req.url.path == "/v2/positions":  # reading one account never locks the other's
            assert not alpaca._round._positions_locks[account == "paper"].locked()
            return httpx.Response(200, json=[{"symbol": "A", "qty": "1"}])
        return httpx.Response(200, json=_priced(req.url.params["symbols"].split(",")),
                              headers={"x-ratelimit-remaining": "10", "x-ratelimit-reset": str(clock["t"] + 20)})

    paper = _broker(handler)
    live = AlpacaBroker("live-key", "secret", live=True, transport=httpx.MockTransport(handler), fill_wait_seconds=0)

    def sleep(s):  # while the paper bot waits for the next minute, a live bot asks (another thread, in the scheduler)
        assert not alpaca._round._prices_lock.locked()
        live.quote("B")
        clock["t"] += s

    monkeypatch.setattr(alpaca.time, "sleep", sleep)
    monkeypatch.setattr(alpaca.time, "time", lambda: clock["t"])
    with alpaca.shared_round(["A", "B"]):
        live.quote("A")  # 10 of the 200 left
        alpaca._round._prices.clear()  # (as if half a minute passed)
        assert paper.quote("A").price == 10.0  # waited outside the lock; the live bot's read priced it meanwhile
        assert seen == ["live /v2/stocks/snapshots", "live /v2/stocks/snapshots"]
        paper.position_qty("A", cached=True)
        live.position_qty("A", cached=True)


# --- The paper account in a scheduler pass: a burst of orders costs about one request each -------------------------

def _account_and_orders(seen, post):
    """An Alpaca that answers the account, the pass's prices ($10), positions, and orders (POST: `post(body)`)."""
    import json

    def handler(req):
        account = "live" if req.url.host == "api.alpaca.markets" else "paper"
        seen.append(f"{account} {req.method} {req.url.path}")
        if req.url.path == "/v2/account":
            return httpx.Response(200, json={"cash": "1000", "non_marginable_buying_power": "900", "buying_power": "2000"})
        if req.url.path == "/v2/stocks/snapshots":
            return httpx.Response(200, json=_priced(req.url.params["symbols"].split(",")))
        if req.url.path == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": "A", "qty": "1"}])
        if req.method == "POST":
            return post(json.loads(req.content))
        return httpx.Response(200, json=_order("filled", "1", "10"))

    return handler


def test_a_pass_reads_the_paper_account_once_and_takes_each_buy_it_sends_off_it(monkeypatch):
    import app.brokers.alpaca as alpaca

    seen, answer = [], {"post": lambda body: httpx.Response(200, json=_order("accepted"))}
    clock = {"t": 100.0}
    monkeypatch.setattr(alpaca.time, "monotonic", lambda: clock["t"])
    handler = _account_and_orders(seen, lambda body: answer["post"](body))
    paper, live = _broker(handler), _broker(handler, live=True)
    with alpaca.shared_round(["A"]):
        assert paper.buying_power() == 900 and _broker(handler).buying_power() == 900  # every paper bot: one read
        assert live.buying_power() == 900 and live.buying_power() == 900  # real money: the account itself, each time
        assert seen == ["paper GET /v2/account", "live GET /v2/account", "live GET /v2/account"]
        paper.submit("A", "BUY", 2.9, "n", notional=30.0)  # by dollars: never more than that
        assert paper.buying_power() == 870
        paper.quote("A")  # $10 in this pass
        paper.submit("A", "BUY", 5, "q")  # 5 shares: $50 at the pass's price, plus 2% in case the fill is higher
        assert paper.buying_power() == pytest.approx(819)
        paper.submit("A", "SELL", 5, "s")  # a sale's money counts at the next read, not before
        answer["post"] = lambda body: httpx.Response(200, json=_order("filled", "2", "10.5"))
        paper.submit("A", "BUY", 2, "f")  # filled at once: what it cost
        answer["post"] = lambda body: httpx.Response(403, text="insufficient buying power")
        paper.submit("A", "BUY", 1, "r")  # refused: nothing spent
        live.submit("A", "BUY", 1, "l")  # the live account's buys are its own
        assert paper.buying_power() == pytest.approx(798)
        assert [x for x in seen if x.endswith("/v2/account")].count("paper GET /v2/account") == 1
        answer["post"] = lambda body: httpx.Response(200, json=_order("accepted"))
        paper.submit("B", "BUY", 3, "b")  # by shares with no price in the pass: what it costs is unknown
        assert paper.buying_power() == 900 and seen[-1] == "paper GET /v2/account"  # so the account is read again

        def down(body):
            raise httpx.ConnectError("down")

        answer["post"] = down
        with pytest.raises(BrokerError):
            paper.submit("A", "BUY", 1, "x", notional=10.0)  # it may have gone out
        assert paper.buying_power() == 900 and seen[-1] == "paper GET /v2/account"
        seen.clear()
        paper.buying_power()
        clock["t"] += alpaca.ROUND_TTL  # half a minute later: read again
        paper.buying_power()
        assert seen == ["paper GET /v2/account"]
    paper.buying_power()  # outside a pass: the account itself
    assert seen[-1] == "paper GET /v2/account" and len(seen) == 2


def test_a_scheduled_dip_checks_paper_orders_dont_wait_for_their_fills(monkeypatch):
    import app.brokers.alpaca as alpaca

    seen, waits = [], []
    monkeypatch.setattr(alpaca.time, "sleep", waits.append)
    handler = _account_and_orders(seen, lambda body: httpx.Response(200, json=_order("accepted")))
    paper = AlpacaBroker("key", "secret", transport=httpx.MockTransport(handler), fill_wait_seconds=10)
    live = AlpacaBroker("key", "secret", live=True, transport=httpx.MockTransport(handler), fill_wait_seconds=10)
    with alpaca.scheduled():
        assert paper.submit("A", "BUY", 1, "p").status == "submitted"  # sent; the next pass books its fill
        assert seen == ["paper POST /v2/orders"] and waits == []
        assert live.submit("A", "BUY", 1, "l").status == "filled"  # real money waits for its fill, as before
        assert seen[1:] == ["live POST /v2/orders", "live GET /v2/orders:by_client_order_id"] and waits == [1]
    assert paper.submit("A", "BUY", 1, "q").status == "filled"  # from the API: the dashboard shows the fill at once
    assert seen[-1] == "paper GET /v2/orders:by_client_order_id" and waits == [1, 1]


def test_a_pass_looks_the_paper_orders_up_in_one_read_of_the_account(monkeypatch):
    import datetime as dt

    import app.brokers.alpaca as alpaca

    seen, lists, clock = [], [], {"t": 100.0}
    monkeypatch.setattr(alpaca.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(alpaca, "ORDERS_PAGE", 2)
    down = {"list": False}

    def mine(cid, status, at, filled="0"):
        return {"id": f"alp-{cid}", "client_order_id": cid, "status": status, "filled_qty": filled,
                "filled_avg_price": "10" if filled != "0" else None, "submitted_at": at}

    pages = {"2025-06-02T14:59:00Z": [mine("o1", "filled", "2025-06-02T15:00:01.5Z", "1"),
                                      mine("o2", "accepted", "2025-06-02T15:00:02.25Z")],
             "2025-06-02T15:00:02.25Z": [mine("o3", "canceled", "2025-06-02T15:00:03Z")]}

    def handler(req):
        account = "live" if req.url.host == "api.alpaca.markets" else "paper"
        if req.url.path == "/v2/orders":
            lists.append(dict(req.url.params))
            return httpx.Response(500, text="oops") if down["list"] else httpx.Response(200, json=pages[req.url.params["after"]])
        seen.append(f"{account} {req.url.path} {req.url.params.get('client_order_id', '')}".strip())
        if req.url.path == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": "A", "qty": "1"}])
        if req.url.params.get("client_order_id") == "lost":
            return httpx.Response(404, json={"message": "order not found"})
        return httpx.Response(200, json=_order("filled", "1", "10"))

    paper, live = _broker(handler), _broker(handler, live=True)
    since = dt.datetime(2025, 6, 2, 14, 59, tzinfo=dt.timezone.utc)
    with alpaca.shared_round([], orders_since=since):
        paper.position_qty("A", cached=True)
        assert paper.lookup("o2", cached=True).status == "submitted"  # still working: the positions read stays
        paper.position_qty("A", cached=True)
        assert lists == [{"status": "all", "after": "2025-06-02T14:59:00Z", "direction": "asc", "limit": "2"},
                         {"status": "all", "after": "2025-06-02T15:00:02.25Z", "direction": "asc", "limit": "2"}]
        o1 = paper.lookup("o1", cached=True)
        assert (o1.status, o1.filled_qty, o1.avg_price) == ("filled", 1, 10)
        assert paper.lookup("o3", cached=True).status == "canceled"
        assert seen == ["paper /v2/positions"] and len(lists) == 2  # three orders, two requests, both pages
        paper.position_qty("A", cached=True)  # a fill seen: the account holds something else, read again
        assert paper.lookup("old", cached=True).status == "filled"  # not in the list: asked on its own
        assert paper.lookup("lost", cached=True) is None  # only Alpaca's own answer says it never got an order
        assert live.lookup("o1", cached=True).status == "filled" and paper.lookup("o1").status == "filled"
        assert seen == ["paper /v2/positions", "paper /v2/positions", "paper /v2/orders:by_client_order_id old",
                        "paper /v2/orders:by_client_order_id lost", "live /v2/orders:by_client_order_id o1",
                        "paper /v2/orders:by_client_order_id o1"]
        clock["t"] += alpaca.ROUND_TTL
        down["list"] = True  # half a minute later the list is read again, and fails: each order on its own, as before
        seen.clear()
        assert paper.lookup("o1", cached=True).status == "filled" and paper.lookup("o2", cached=True).status == "filled"
        assert len(lists) == 3 and seen == ["paper /v2/orders:by_client_order_id o1", "paper /v2/orders:by_client_order_id o2"]
    with alpaca.shared_round([]):  # nothing working by our records: no list
        paper.lookup("o1", cached=True)
    assert len(lists) == 3


def test_an_order_still_working_keeps_the_pass_read_of_the_positions():
    import app.brokers.alpaca as alpaca

    seen, answer = [], {"status": "accepted"}
    handler = _account_and_orders(seen, lambda body: httpx.Response(200, json=_order(answer["status"], "0")))
    paper = _broker(handler)
    with alpaca.shared_round([]):
        paper.position_qty("A", cached=True)
        paper.submit("A", "BUY", 1, "w")  # working: the account checks leave A alone until its fill is booked
        paper.position_qty("A", cached=True)
        assert seen == ["paper GET /v2/positions", "paper POST /v2/orders"]
        answer["status"] = "filled"
        paper.submit("A", "BUY", 1, "f")  # filled at once: booked now, so the read is from before it
        paper.position_qty("A", cached=True)
        assert seen[2:] == ["paper POST /v2/orders", "paper GET /v2/positions"]


def test_the_paper_account_paces_its_trading_requests_to_the_minutes_budget(monkeypatch):
    import app.brokers.alpaca as alpaca

    waits, left, clock = [], {"n": "10"}, {"t": 1000.0}
    monkeypatch.setattr(alpaca.time, "sleep", lambda s: (waits.append(s), clock.update(t=clock["t"] + s)))
    monkeypatch.setattr(alpaca.time, "time", lambda: clock["t"])

    def handler(req):  # each answer: requests left of this account's budget, and its minute ends in 20 s
        return httpx.Response(200, json={"qty": "1"},
                              headers={"x-ratelimit-remaining": left["n"], "x-ratelimit-reset": str(clock["t"] + 20)})

    paper, live = _broker(handler), _broker(handler, live=True)
    paper.position_qty("A")
    paper.position_qty("A")  # 10 left: it goes on
    left["n"] = "9"
    paper.position_qty("A")
    live.position_qty("A")
    live.position_qty("A")  # the live account has its own budget and never waits for the paper bots
    assert waits == []
    paper.position_qty("A")  # 9 left: a burst of paper orders waits for the next minute instead of running into 429s
    assert waits == [20.0] and clock["t"] == 1020.0
