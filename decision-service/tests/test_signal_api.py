"""The /signal endpoint's parameters (the agent itself is replaced: no prices, news or LLM calls)."""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def seen(monkeypatch):
    """Capture the state /signal hands to the agent, and answer with a fixed HOLD."""
    states = []

    def fake_invoke(state):
        states.append(state)
        return {**state, "action": "HOLD", "confidence": 0.5, "reasoning": "test", "steps": []}

    monkeypatch.setattr(main.agent, "invoke", fake_invoke)
    return states


def test_decided_at_reaches_the_agent_as_an_aware_datetime(seen):
    r = TestClient(main.app).post("/signal", params={"symbol": "aapl", "as_of": "2025-06-02", "decided_at": "2025-06-02T14:00:00Z"})
    assert r.status_code == 200
    assert seen[0]["decided_at"] == datetime(2025, 6, 2, 14, 0, tzinfo=timezone.utc)
    assert seen[0]["symbol"] == "AAPL" and seen[0]["as_of"] == date(2025, 6, 2)


def test_without_decided_at_the_default_1530_cutoff_applies(seen):
    TestClient(main.app).post("/signal", params={"symbol": "AAPL", "as_of": "2025-06-02"})
    assert "decided_at" not in seen[0]


def test_decided_at_on_another_day_is_refused(seen):
    """00:30 UTC on June 3 is still June 2 in New York (fine); June 3 10:00 New York is not (look-ahead)."""
    client = TestClient(main.app)
    ok = client.post("/signal", params={"symbol": "AAPL", "as_of": "2025-06-02", "decided_at": "2025-06-03T00:30:00Z"})
    bad = client.post("/signal", params={"symbol": "AAPL", "as_of": "2025-06-02", "decided_at": "2025-06-03T14:00:00Z"})
    assert ok.status_code == 200 and bad.status_code == 422


def test_the_strategy_reaches_the_agent_and_defaults_to_sma_rsi(seen):
    client = TestClient(main.app)
    client.post("/signal", params={"symbol": "AAPL", "as_of": "2025-06-02", "strategy": "breakout"})
    client.post("/signal", params={"symbol": "AAPL", "as_of": "2025-06-02"})
    assert [s["strategy"] for s in seen] == ["breakout", "sma_rsi"]


def test_unknown_strategy_is_refused(seen):
    r = TestClient(main.app).post("/signal", params={"symbol": "AAPL", "as_of": "2025-06-02", "strategy": "llm"})
    assert r.status_code == 422 and "unknown strategy" in r.text
    assert seen == []


def test_strategies_catalog_lists_every_strategy_with_its_rules():
    body = TestClient(main.app).get("/strategies").json()
    assert body[0]["id"] == "sma_rsi"  # the original simple strategy stays first (the default)
    assert len(body) == 11
    assert all(s["buy"] and s["sell"] and s["summary"] and s["suggested"]["rebalance_days"] >= 1 for s in body)


def test_explain_false_turns_off_the_llm_explanation(seen):
    client = TestClient(main.app)
    client.post("/signal", params={"symbol": "AAPL", "as_of": "2025-06-02"})
    client.post("/signal", params={"symbol": "AAPL", "as_of": "2025-06-02", "explain": "false"})
    assert [s["llm_explanation"] for s in seen] == [True, False]  # live bots keep it by default


def test_news_false_asks_for_a_technical_only_decision(seen):
    client = TestClient(main.app)
    client.post("/signal", params={"symbol": "AAPL", "as_of": "2025-06-02"})
    client.post("/signal", params={"symbol": "AAPL", "as_of": "2025-06-02", "news": "false"})
    assert [s["use_news"] for s in seen] == [True, False]  # live bots keep news by default
