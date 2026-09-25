"""A failing LLM (e.g. OpenAI's 429 rate limit) must never cost a decision: the explanation falls back to the
rule text, and failed news sentiment means deciding on the technicals alone. No network: the LLM is faked."""

from datetime import date

import pytest

from app import agent, news


class _RateLimited:
    """Stands in for ChatOpenAI: every call fails like a 429 that outlasted the retries."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def with_structured_output(self, schema):
        return self

    def invoke(self, *args, **kwargs):
        raise RuntimeError("Error code: 429 - rate limit reached")


@pytest.fixture
def rate_limited(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _RateLimited)


def test_explanation_falls_back_to_the_rule_text(rate_limited):
    out = agent.explain({"symbol": "AAPL", "as_of": date(2026, 8, 3), "action": "BUY", "steps": ["SMA20 > SMA50, RSI14=55 -> BUY"]})
    assert out["reasoning"] == "BUY AAPL: SMA20 > SMA50, RSI14=55 -> BUY"


def test_failed_sentiment_decides_without_news(rate_limited, monkeypatch):
    monkeypatch.setattr(news, "ingest_news", lambda *a, **k: {"alpaca": "3"})
    monkeypatch.setattr(news, "search_news", lambda *a, **k: [{"text": "[alpaca, 2h ago] Apple beats", "document": "Apple beats", "age_hours": 2}])
    out = agent._analyze_news({"symbol": "AAPL", "as_of": date(2026, 8, 3)})
    assert out["sentiment"] == "unavailable"  # no news tilt either way
    assert out["cacheable"] is False  # the next strategy on this day tries again
    assert "News sentiment failed" in out["steps"][0]


def test_the_sentiment_call_retries_longer_than_the_explanation(monkeypatch):
    made = []
    monkeypatch.setattr("langchain_openai.ChatOpenAI", lambda **kw: made.append(kw) or _RateLimited(**kw))
    agent._chat(max_retries=agent.SENTIMENT_RETRIES)
    agent._chat(max_retries=agent.EXPLAIN_RETRIES)
    assert made[0]["max_retries"] > made[1]["max_retries"]


def test_a_failed_analysis_leaves_no_lock_behind(monkeypatch):
    monkeypatch.setattr(agent, "_news_enabled", lambda: True)

    def boom(state):
        raise RuntimeError("database down")

    monkeypatch.setattr(agent, "_analyze_news", boom)
    with pytest.raises(RuntimeError):
        agent.news_rag({"symbol": "AAPL", "as_of": date(2026, 8, 3), "steps": []})
    assert agent._news_inflight == {}
