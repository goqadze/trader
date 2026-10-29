"""The overview's bot table, a page at a time (GET /bots/table): filtered by broker, kind and sector, sorted by a column
on the server, with what the page shows of every bot (counts per broker, totals, kind and sector choices, Halt all's
counts), and the history of the archived bots (GET /bots/archived)."""

from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_bot
from fastapi.testclient import TestClient

from app import main
from app.models import DIP, ROTATION, Event, Holding, WatchItem

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


def test_sorted_by_how_far_the_return_beats_buy_and_hold(client, bots, session):
    a, b, c, d, e, g = (bots[k] for k in "abcdeg")
    # b made the most (+2%) but XOM rose 20% (18 points behind holding it); w made +5% in cash while T fell 10% (15
    # ahead); the other bots' symbols didn't move. n has no buy & hold yet: last either way.
    w = make_bot(session, symbol="T", cash=10_500.0, last_price=90.0).id
    n = make_bot(session, symbol="NEW", benchmark_price=None).id
    assert ids(client, sort="ret", order="desc")[:2] == [w, b]
    assert ids(client, sort="beat", order="desc") == [w, g, e, a, d, c, b, n]  # ties keep the newest-first order
    assert ids(client, sort="beat", order="asc") == [b, c, d, g, e, a, w, n]

    # The page's two numbers give what it was sorted by
    page = table(client, sort="beat", order="desc")["items"]
    assert [round(x["return_pct"] - x["buy_hold_return_pct"], 2) for x in page[:-1]] == [15.0, 0, 0, 0, -1.0, -5.0, -18.0]
    assert page[-1]["buy_hold_return_pct"] is None


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
                           "active": 5, "count": 6, "archived": 1, "archived_pnl": 0.0}
    assert t["sectors"] == {"Tech": 3, "Energy": 2, "Chips": 1, "Defensive": 1}  # the most common first
    assert (t["active"], t["active_live"]) == (5, 1)  # for Halt all: every account
    assert [x["id"] for x in t["brief"]] == [g, e, d, c, b, a] and t["brief"][2]["strategy"] == DIP

    paper = table(client, broker="alpaca-paper")
    assert ([x["id"] for x in paper["items"]], paper["broker"], paper["total"]) == ([d, c], "alpaca-paper", 2)
    assert paper["totals"] == {"capital": 20_000.0, "added": 0.0, "withdrawn": 0.0, "equity": 19_400.0, "pnl": -600.0,
                               "active": 2, "count": 2, "archived": 0, "archived_pnl": 0.0}  # f is on the simulator
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
    assert archived["totals"] == t["totals"]  # an archived bot is in the list, not in the totals (counted apart)


def test_the_bot_filter_shows_dip_buyers_rotations_or_single_symbol_bots(client, bots, session):
    a, b, c, d, e, g = (bots[k] for k in "abcdeg")
    r = make_bot(session, symbol="ROT", strategy=ROTATION, broker="alpaca-paper", universe=["SPY", "TLT"], top_n=1).id
    t = table(client)
    assert t["kinds"] == {"symbol": 5, "dip": 1, "rotation": 1} and list(t["kinds"])[0] == "symbol"  # the most common first
    assert ids(client, kind="dip") == [d]
    assert ids(client, kind="rotation") == [r]
    assert ids(client, kind=["dip", "rotation"]) == [r, d]  # any of them
    assert ids(client, kind="symbol") == [g, e, c, b, a]
    assert ids(client, kind="symbol", sector="Energy") == [b]  # and the sector filter too
    assert ids(client, kind="rotation", sort="id", order="asc", limit=1) == [r]
    assert table(client, kind="rotation")["totals"]["count"] == 7  # the cards don't follow the table's filter
    assert table(client, kind="rotation")["total"] == 1
    assert table(client, broker="alpaca-paper")["kinds"] == {"rotation": 1, "dip": 1, "symbol": 1}  # that broker's bots
    assert client.get("/bots/table", params={"kind": "stock"}).status_code == 422


def test_archived_bots_keep_what_they_made_or_lost_in_the_cards_and_a_history(client, session):
    """Archiving hands a bot's cash back to the account, so the running bots' totals drop its loss: the cards count
    the shown broker's archived bots apart, and GET /bots/archived lists them, the earliest archived first, with a
    running total."""
    early, late = datetime(2026, 10, 16, 20, tzinfo=timezone.utc), datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
    old = make_bot(session, name="script", broker="alpaca-live", status="archived", allocated_cash=100.0, cash=100.0,
                   created_at=datetime(2026, 10, 10, tzinfo=timezone.utc))  # no "archived" event: by when it was made
    lost = make_bot(session, name="defensive", broker="alpaca-live", status="archived", allocated_cash=400.0, cash=357.08)
    # Money added and taken out: put in 120 + 30, came out 135 left + 10 taken out earlier
    topped = make_bot(session, name="grid", broker="alpaca-live", status="archived", allocated_cash=120.0,
                      added_cash=30.0, withdrawn_cash=10.0, cash=135.0)
    paper = make_bot(session, name="paper", broker="alpaca-paper", status="archived", allocated_cash=400.0, cash=420.0)
    running = make_bot(session, name="running", broker="alpaca-live", allocated_cash=200.0, cash=190.0)
    session.add_all([Event(bot_id=topped.id, created_at=early, kind="archived", message="Archived by user"),
                     # Alpaca's fee of its last day, booked after the archive (fees.py): not when it was archived
                     Event(bot_id=topped.id, created_at=late + timedelta(days=1), kind="fee", message="REG fee $0.01"),
                     Event(bot_id=lost.id, created_at=late, kind="archived", message="Archived by user")])
    session.commit()
    # Archived on the dashboard now: the last one
    news = make_bot(session, name="news", broker="alpaca-live", status="paused", allocated_cash=126.0, cash=117.21).id
    assert client.post(f"/bots/{news}/archive").status_code == 200

    live = table(client, broker="alpaca-live")["totals"]
    assert (live["capital"], live["pnl"], live["count"]) == (200.0, -10.0, 1)  # the running bot only
    assert (live["archived"], live["archived_pnl"]) == (4, -56.71)  # 0 - 5 - 42.92 - 8.79
    assert table(client, broker="alpaca-live", include_archived=True)["totals"] == live
    assert {k: table(client)["totals"][k] for k in ("archived", "archived_pnl")} == {"archived": 5, "archived_pnl": -36.71}

    r = client.get("/bots/archived", params={"broker": "alpaca-live"})
    assert r.status_code == 200, r.text
    h = r.json()
    assert [(x["id"], x["put_in"], x["came_out"], x["result"], x["running"]) for x in h["items"]] == [
        (old.id, 100.0, 100.0, 0.0, 0.0), (topped.id, 150.0, 145.0, -5.0, -5.0),
        (lost.id, 400.0, 357.08, -42.92, -47.92), (news, 126.0, 117.21, -8.79, -56.71)]
    assert h["result"] == -56.71
    assert h["items"][0]["archived_at"] is None and h["items"][2]["archived_at"].startswith("2026-10-20T20:00")
    assert running.id not in [x["id"] for x in h["items"]]
    assert [x["id"] for x in client.get("/bots/archived", params={"broker": "alpaca-paper"}).json()["items"]] == [paper.id]
    everything = client.get("/bots/archived").json()
    assert (len(everything["items"]), everything["result"]) == (5, -36.71)
    assert client.get("/bots/archived", params={"broker": "paper"}).json() == {"items": [], "result": 0.0}
