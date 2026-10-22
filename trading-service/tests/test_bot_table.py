"""The overview's bot table, a page at a time (GET /bots/table): filtered by broker and sector, sorted by a column on
the server, with what the page shows of every bot (counts per broker, totals, sector choices, Halt all's counts)."""

import pytest
from conftest import make_bot
from fastapi.testclient import TestClient

from app import main
from app.models import DIP, Holding, WatchItem

PERIODS = {"practice": ["2025-04-09", "2026-01-08"], "exam": ["2026-01-09", "2026-10-09"],
           "stress": ["2024-07-09", "2025-04-08"]}


def setup(code: str, score: float, periods: dict = PERIODS) -> dict:
    """A tested setup's copy as a bot keeps it (main._setup_snapshot): its windows' returns 10, 20, -10 → All 18.8%."""
    return {"test_id": 1, "code": code, "name": f"{code} test", "study_id": 1, "study": "Sweep", "strategy": "dip",
            "score": score, "periods": periods, "config": {},
            "results": {"practice": {"total_return_pct": 10}, "exam": {"total_return_pct": 20}, "stress": {"total_return_pct": -10}},
            "started_at": "2026-10-15T12:00:00"}


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def bots(session) -> dict[str, int]:
    """Seven bots, oldest first (ids 1-7): their returns, positions, stars, setups, accounts and sectors differ."""
    a = make_bot(session, symbol="AAPL", rating=2, setup=setup("T1", 5.0))  # Tech; 0%
    b = make_bot(session, symbol="XOM", rating=5, cash=9_000.0, shares=10, cost_basis=1_000.0, last_price=120.0)  # +2%, +$200
    c = make_bot(session, symbol="NVDA", broker="alpaca-paper", cash=9_500.0,  # -5%; a setup without an All (two windows)
                 setup=setup("T2", -1.0, {p: PERIODS[p] for p in ("practice", "exam")}))
    d = make_bot(session, symbol="DIP", strategy=DIP, broker="alpaca-paper", cash=9_000.0, universe=["XOM", "CVX", "AAPL"],
                 drop_pct=0.05, lookback=1, lookback_unit="days", interval="1d", max_positions=2)  # Energy + Tech; -1%, -$100
    session.add(Holding(bot_id=d.id, symbol="XOM", shares=10, cost_basis=1_000.0, last_price=90.0))
    session.add_all([WatchItem(bot_id=d.id, symbol=s) for s in ("XOM", "CVX", "AAPL")])
    e = make_bot(session, symbol="AAPL", broker="alpaca-live", rating=2, setup=setup("T1", 5.0))  # real money; 0%
    f = make_bot(session, symbol="MSFT", status="archived")
    g = make_bot(session, symbol="KO", status="paused")  # Defensive; 0%
    session.commit()
    return {k: v.id for k, v in zip("abcdefg", (a, b, c, d, e, f, g))}


def table(client, **params) -> dict:
    r = client.get("/bots/table", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def ids(client, **params) -> list[int]:
    return [b["id"] for b in table(client, **params)["items"]]


def test_the_table_is_sorted_by_any_column_on_the_server(client, bots):
    a, b, c, d, e, g = (bots[k] for k in "abcdeg")
    assert ids(client) == [g, e, d, c, b, a]  # newest first; archived ones only when asked for
    assert ids(client, sort="ret", order="desc") == [b, g, e, a, d, c]  # ties keep the newest-first order
    assert ids(client, sort="ret", order="asc") == [c, d, g, e, a, b]
    assert ids(client, sort="pos", order="desc") == [b, g, e, c, a, d]
    assert ids(client, sort="rating", order="desc") == [b, e, a, g, d, c]
    assert ids(client, sort="id", order="asc") == [a, b, c, d, e, g]
    assert ids(client, sort="score", order="desc") == [e, a, c, g, d, b]  # no tested setup: last either way
    assert ids(client, sort="score", order="asc") == [c, e, a, g, d, b]
    assert ids(client, sort="all", order="desc") == [e, a, g, d, c, b]  # c's setup has no All
    assert ids(client, sort="all", order="asc") == [e, a, g, d, c, b]

    # The page's numbers are the bots' own (the ones the sort used)
    page = table(client, sort="ret", order="desc")["items"]
    assert [x["return_pct"] for x in page] == [2.0, 0.0, 0.0, 0.0, -1.0, -5.0]
    dip = next(x for x in page if x["id"] == d)
    assert (dip["equity"], dip["unrealized_pnl"], [w["symbol"] for w in dip["watchlist"]]) == (9_900.0, -100.0, ["AAPL", "CVX", "XOM"])
    assert table(client, sort="all")["items"][0]["setup"]["all"] == pytest.approx(18.8)
    assert client.get("/bots/table", params={"sort": "name"}).status_code == 422


def test_pages_and_a_page_past_the_end(client, bots):
    a, b, c, d, e, g = (bots[k] for k in "abcdeg")
    assert ids(client, limit=2) == [g, e]
    assert ids(client, limit=2, offset=2) == [d, c]
    t = table(client, limit=2, offset=10)  # fewer bots than that: the last page
    assert ([x["id"] for x in t["items"]], t["offset"], t["total"]) == ([b, a], 4, 6)
    assert table(client, broker="nope", offset=50)["offset"] == 0
    assert client.get("/bots/table", params={"limit": 0}).status_code == 422


def test_a_broker_and_sectors_narrow_it_and_the_totals_are_the_brokers(client, bots):
    a, b, c, d, e, f, g = (bots[k] for k in "abcdefg")
    t = table(client)
    assert (t["broker"], t["total"], t["brokers"]) == ("all", 6, {"paper": 3, "alpaca-paper": 2, "alpaca-live": 1})
    assert t["totals"] == {"capital": 60_000.0, "added": 0.0, "withdrawn": 0.0, "equity": 59_600.0, "pnl": -400.0,
                           "active": 5, "count": 6}
    assert t["sectors"] == {"Tech": 3, "Energy": 2, "Chips": 1, "Defensive": 1}  # the most common first
    assert (t["active"], t["active_live"]) == (5, 1)  # for Halt all: every account
    assert [x["id"] for x in t["brief"]] == [g, e, d, c, b, a] and t["brief"][2]["strategy"] == DIP

    paper = table(client, broker="alpaca-paper")
    assert ([x["id"] for x in paper["items"]], paper["broker"], paper["total"]) == ([d, c], "alpaca-paper", 2)
    assert paper["totals"] == {"capital": 20_000.0, "added": 0.0, "withdrawn": 0.0, "equity": 19_400.0, "pnl": -600.0,
                               "active": 2, "count": 2}
    assert paper["sectors"] == {"Chips": 1, "Energy": 1, "Tech": 1}  # that broker's bots only
    assert (paper["active"], paper["brokers"]["paper"]) == (5, 3)  # still every account's
    assert table(client, broker="nope")["broker"] == "all"  # a broker with no bots: all of them

    assert ids(client, sector="Energy") == [d, b]
    assert ids(client, sector=["Energy", "Chips"]) == [d, c, b]  # any of them
    assert ids(client, sector="Energy", sort="ret", order="desc") == [b, d]
    assert table(client, sector="Energy")["totals"]["count"] == 6  # the cards don't follow the table's filter

    archived = table(client, include_archived=True)
    assert (archived["total"], archived["brokers"]["paper"]) == (7, 4)
    assert [x["id"] for x in archived["items"]] == [g, f, e, d, c, b, a]
    assert archived["totals"] == t["totals"]  # an archived bot is in the list, not in the totals
