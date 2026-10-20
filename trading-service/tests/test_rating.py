"""A bot's stars, 0 to 5 (PUT /bots/{id}/rating): the owner's favourites, for sorting the bot table."""

import pytest
from conftest import make_bot
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main
from app.models import Bot, Event


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def ratings(client) -> dict[int, int]:
    r = client.get("/bots", params={"include_archived": True})
    assert r.status_code == 200, r.text
    return {b["id"]: b["rating"] for b in r.json()}


def test_a_new_bot_is_not_rated(client, session):
    bot = make_bot(session)
    assert ratings(client)[bot.id] == 0
    assert client.get(f"/bots/{bot.id}").json()["rating"] == 0


def test_stars_are_kept_and_can_be_cleared(client, session):
    bot = make_bot(session)
    other = make_bot(session, symbol="MSFT")
    r = client.put(f"/bots/{bot.id}/rating", json={"rating": 4})
    assert r.status_code == 200, r.text
    assert r.json()["rating"] == 4
    assert ratings(client) == {bot.id: 4, other.id: 0}  # only that bot
    session.expire_all()
    assert session.get(Bot, bot.id).rating == 4

    assert client.put(f"/bots/{bot.id}/rating", json={"rating": 0}).json()["rating"] == 0  # cleared
    assert ratings(client)[bot.id] == 0


def test_rating_changes_nothing_else_and_leaves_no_event(client, session):
    bot = make_bot(session)
    before = {k: v for k, v in client.get(f"/bots/{bot.id}").json().items() if k != "rating"}
    client.put(f"/bots/{bot.id}/rating", json={"rating": 5})
    after = client.get(f"/bots/{bot.id}").json()
    assert after["rating"] == 5
    assert {k: v for k, v in after.items() if k != "rating"} == before
    assert session.scalars(select(Event).where(Event.bot_id == bot.id)).all() == []


def test_paused_and_archived_bots_can_be_rated_too(client, session):
    paused = make_bot(session, status="paused")
    archived = make_bot(session, status="archived")
    assert client.put(f"/bots/{paused.id}/rating", json={"rating": 2}).json()["rating"] == 2
    assert client.put(f"/bots/{archived.id}/rating", json={"rating": 3}).json()["rating"] == 3
    assert ratings(client) == {paused.id: 2, archived.id: 3}


@pytest.mark.parametrize("body", [
    {"rating": 6},  # five stars at most
    {"rating": -1},
    {"rating": 2.5},  # whole stars
    {"rating": "3"},  # a number, not text
    {"rating": None},
    {},
])
def test_bad_ratings_are_refused(client, session, body):
    bot = make_bot(session)
    assert client.put(f"/bots/{bot.id}/rating", json=body).status_code == 422
    session.expire_all()
    assert session.get(Bot, bot.id).rating == 0


def test_unknown_bot_is_404(client):
    assert client.put("/bots/999/rating", json={"rating": 3}).status_code == 404
