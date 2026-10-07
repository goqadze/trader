"""The /news/sentiment endpoint: the news step alone, for the dip buyer (no prices, news sources or LLM: the news
analysis is replaced)."""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import agent, main


@pytest.fixture
def analyzed(monkeypatch):
    """Replace the news analysis with a fixed bearish verdict and record the states it was asked about."""
    states = []

    def fake(state):
        states.append(state)
        return {"headlines": ["Recall widens"], "catalysts": ["Recall widens"], "sentiment": "bearish",
                "steps": ["News (1 retrieved): bearish - recall"], "cacheable": True}

    agent._news_cache.clear()
    monkeypatch.setattr(agent, "_news_enabled", lambda: True)
    monkeypatch.setattr(agent, "_analyze_news", fake)
    yield states
    agent._news_cache.clear()


def test_returns_the_news_verdict_for_a_symbol(analyzed):
    r = TestClient(main.app).get("/news/sentiment", params={"symbol": "aapl", "as_of": "2025-06-02"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "AAPL" and body["sentiment"] == "bearish"
    assert body["headlines"] == ["Recall widens"] and body["catalysts"] == ["Recall widens"]
    assert analyzed[0]["as_of"] == date(2025, 6, 2)


def test_decided_at_is_the_news_cutoff(analyzed):
    r = TestClient(main.app).get("/news/sentiment", params={"symbol": "AAPL", "as_of": "2025-06-02",
                                                            "decided_at": "2025-06-02T15:15:00Z"})
    assert r.status_code == 200
    assert analyzed[0]["decided_at"] == datetime(2025, 6, 2, 15, 15, tzinfo=timezone.utc)


def test_decided_at_on_another_day_is_refused(analyzed):
    r = TestClient(main.app).get("/news/sentiment", params={"symbol": "AAPL", "as_of": "2025-06-02",
                                                            "decided_at": "2025-06-03T14:00:00Z"})
    assert r.status_code == 422 and analyzed == []


def test_a_past_moment_is_judged_once(analyzed):
    client = TestClient(main.app)
    params = {"symbol": "AAPL", "as_of": "2025-06-02", "decided_at": "2025-06-02T15:15:00Z"}
    client.get("/news/sentiment", params=params)
    client.get("/news/sentiment", params=params)
    assert len(analyzed) == 1


def test_without_news_keys_the_verdict_is_unavailable(monkeypatch):
    monkeypatch.setattr(agent, "_news_enabled", lambda: False)
    body = TestClient(main.app).get("/news/sentiment", params={"symbol": "AAPL", "as_of": "2025-06-02"}).json()
    assert body["sentiment"] == "unavailable" and body["headlines"] == [] and body["catalysts"] == []
