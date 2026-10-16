"""Sector badges: worked out from a bot's symbols until set by hand (sectors.py, PUT /bots/{id}/sectors)."""

import pytest
from conftest import make_bot
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, sectors
from app.models import DIP, ROTATION, Bot, Event, SymbolSector, WatchItem

ENERGY = ["XOM", "CVX", "COP", "EOG", "SLB", "OXY", "PSX", "MPC", "VLO", "HAL", "DVN", "KMI", "WMB"]  # 13
CHIPS = ["NVDA", "AMD", "AVGO", "MU", "INTC", "TSM", "QCOM", "TXN"]  # 8
DEFENSIVE = ["KO", "PEP", "PG", "WMT"]  # 4
SECTOR_ETFS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]


def test_thirteen_energy_stocks_are_one_energy_badge():
    assert sectors.badges(ENERGY, {}) == ["Energy"]


def test_eight_chips_and_four_staples_are_two_badges_biggest_first():
    assert sectors.badges(DEFENSIVE + CHIPS, {}) == ["Chips", "Defensive"]


def test_a_fifth_of_the_symbols_makes_a_badge_less_does_not():
    assert sectors.badges(ENERGY[:4] + ["NVDA"], {}) == ["Energy", "Chips"]  # 1 of 5: a fifth
    assert sectors.badges(ENERGY[:5] + ["NVDA"], {}) == ["Energy"]  # 1 of 6


def test_three_badges_at_most_ties_in_the_listed_order():
    # four sectors with 2 symbols each: all qualify, the first three of BADGES' order stay
    assert sectors.badges(["XOM", "CVX", "NVDA", "AMD", "KO", "PEP", "JPM", "BAC"], {}) == ["Chips", "Energy", "Defensive"]


def test_a_list_spread_thin_is_diversified():
    assert sectors.badges(SECTOR_ETFS, {}) == ["Diversified"]


def test_unknown_symbols_dont_count():
    assert sectors.badges(["XOM", "ZZZZ", "QQQQ"], {}) == ["Energy"]
    assert sectors.badges(["ZZZZ"], {}) == []
    assert sectors.badges([], {}) == []


def test_an_unlisted_symbol_takes_yahoos_sector():
    assert sectors.badges(["ZZZZ"], {"ZZZZ": "Utilities"}) == ["Utilities"]
    assert sectors.badges(["ZZZZ"], {"ZZZZ": "Equity Precious Metals"}) == ["Metals & mining"]  # an ETF's category
    assert sectors.badges(["NVDA"], {"NVDA": "Technology"}) == ["Chips"]  # the hand-made list wins


def test_the_lists_are_consistent():
    assert set(sectors._SECTORS) <= set(sectors.BADGES)
    assert set(sectors.YAHOO.values()) <= set(sectors.BADGES)
    listed = [s for symbols in sectors._SECTORS.values() for s in symbols.split()]
    assert len(listed) == len(set(listed)), "a symbol is in two sectors"


def test_names_typed_by_hand_are_tidied():
    assert sectors.clean(["  energy ", "Energy", "my   theme", "", "MY THEME"]) == ["Energy", "my theme"]


# --- API ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def dip_bot(session, watch, universe=None):
    start = list(universe or watch)
    bot = make_bot(session, symbol="DIP", strategy=DIP, interval="15m", drop_pct=0.05, lookback=1, lookback_unit="hours",
                   drop_from="high", target_mode="reference", rise_pct=0.05, max_positions=2, max_hold_days=0, news=False,
                   trend_filter=False, universe=start, benchmark_prices={s: 10.0 for s in start},
                   benchmark_price=10_000.0, last_price=10_000.0)
    for s in watch:
        session.add(WatchItem(bot_id=bot.id, symbol=s, status="watching"))
    session.commit()
    return bot


def listed(client) -> dict[int, tuple[list[str], bool]]:
    r = client.get("/bots", params={"include_archived": True})
    assert r.status_code == 200, r.text
    return {b["id"]: (b["sectors"], b["sectors_auto"]) for b in r.json()}


def test_bot_list_shows_each_kind_of_bots_badges(client, session):
    single = make_bot(session, symbol="AAPL")
    rotation = make_bot(session, symbol="ROTATION", strategy=ROTATION, universe=SECTOR_ETFS, top_n=3, lookback_months=12,
                        skip_months=0, abs_filter=False)
    # a dip bot's watchlist changed since it started (universe): the badges follow what it watches now
    dip = dip_bot(session, watch=ENERGY, universe=CHIPS)
    session.add(SymbolSector(symbol="ZZZZ", sector="Healthcare"))
    session.commit()
    unlisted = make_bot(session, symbol="ZZZZ")
    got = listed(client)
    assert got[single.id] == (["Tech"], True)
    assert got[rotation.id] == (["Diversified"], True)
    assert got[dip.id] == (["Energy"], True)
    assert got[unlisted.id] == (["Healthcare"], True)
    assert client.get(f"/bots/{dip.id}").json()["sectors"] == ["Energy"]


def test_badges_set_by_hand_stay_until_reset_to_automatic(client, session):
    bot = dip_bot(session, watch=CHIPS + DEFENSIVE)
    r = client.put(f"/bots/{bot.id}/sectors", json={"sectors": ["power & GRID", " My  theme "]})
    assert r.status_code == 200, r.text
    assert (r.json()["sectors"], r.json()["sectors_auto"]) == (["Power & grid", "My theme"], False)
    assert listed(client)[bot.id] == (["Power & grid", "My theme"], False)
    assert r.json()["sectors_from_symbols"] == ["Chips", "Defensive"]  # what "back to automatic" would give

    r = client.put(f"/bots/{bot.id}/sectors", json={"sectors": []})  # none at all, by hand
    assert (r.json()["sectors"], r.json()["sectors_auto"]) == ([], False)

    r = client.put(f"/bots/{bot.id}/sectors", json={"sectors": None})  # back to its symbols
    assert (r.json()["sectors"], r.json()["sectors_auto"]) == (["Chips", "Defensive"], True)
    session.expire_all()
    assert session.get(Bot, bot.id).sectors is None
    said = session.scalars(select(Event.message).where(Event.bot_id == bot.id).order_by(Event.id)).all()
    assert said == ["Sector badges: Power & grid, My theme", "Sector badges: none",
                    "Sector badges: automatic (from its symbols)"]


def test_the_same_badges_again_change_nothing(client, session):
    bot = make_bot(session, symbol="XOM")
    client.put(f"/bots/{bot.id}/sectors", json={"sectors": ["Energy"]})
    client.put(f"/bots/{bot.id}/sectors", json={"sectors": ["energy"]})
    assert len(session.scalars(select(Event).where(Event.bot_id == bot.id)).all()) == 1


def test_an_archived_bot_can_be_labelled_too(client, session):
    bot = make_bot(session, symbol="XOM", status="archived")
    r = client.put(f"/bots/{bot.id}/sectors", json={"sectors": ["Energy", "Old"]})
    assert r.status_code == 200 and r.json()["sectors"] == ["Energy", "Old"]


@pytest.mark.parametrize("body", [
    {"sectors": ["A", "B", "C", "D", "E", "F", "G"]},  # 7: six at most
    {"sectors": ["x" * 25]},  # longer than 24
    {},  # sectors is required (null resets, it can't be left out)
    {"sectors": "Energy"},  # a list, not a string
])
def test_bad_badges_are_refused(client, session, body):
    bot = make_bot(session, symbol="XOM")
    assert client.put(f"/bots/{bot.id}/sectors", json=body).status_code == 422


def test_unknown_bot_is_404_and_the_badge_list_is_served(client):
    assert client.put("/bots/999/sectors", json={"sectors": ["Energy"]}).status_code == 404
    assert client.get("/sectors").json() == sectors.BADGES
