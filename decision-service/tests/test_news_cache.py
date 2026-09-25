"""Tests for the per-day news cache that lets a strategy comparison run the news step once per day.

No network or LLM: news_rag's analysis (_analyze_news) is replaced with a counter.
"""

import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from app import agent
from app.news import MARKET_TZ


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    agent._news_cache.clear()
    agent._news_inflight.clear()
    monkeypatch.setattr(agent, "_news_enabled", lambda: True)
    yield
    agent._news_cache.clear()


def _counting(monkeypatch, cacheable=True, sentiment="bullish"):
    calls = []

    def fake(state):
        calls.append(state["as_of"])
        return {"headlines": ["h"], "catalysts": [], "sentiment": sentiment, "steps": ["News: " + sentiment], "cacheable": cacheable}

    monkeypatch.setattr(agent, "_analyze_news", fake)
    return calls


def _state(as_of, strategy="sma_rsi", **extra):
    return {"symbol": "AAPL", "as_of": as_of, "strategy": strategy, "steps": [f"Strategy: {strategy}"], **extra}


PAST = date(2026, 8, 3)


def test_a_past_day_is_analyzed_once_across_strategies(monkeypatch):
    calls = _counting(monkeypatch)
    a = agent.news_rag(_state(PAST, "sma_rsi"))
    b = agent.news_rag(_state(PAST, "breakout"))
    assert calls == [PAST]
    assert a["sentiment"] == b["sentiment"] == "bullish"
    # Each call keeps its own strategy's steps in front of the shared news steps
    assert b["steps"] == ["Strategy: breakout", "News: bullish"]


def test_other_days_and_symbols_are_separate(monkeypatch):
    calls = _counting(monkeypatch)
    agent.news_rag(_state(PAST))
    agent.news_rag(_state(PAST + timedelta(days=1)))
    agent.news_rag({**_state(PAST), "symbol": "META"})
    assert len(calls) == 3


def test_failed_or_partial_news_is_not_cached(monkeypatch):
    calls = _counting(monkeypatch, cacheable=False, sentiment="unavailable")
    agent.news_rag(_state(PAST))
    agent.news_rag(_state(PAST))
    assert len(calls) == 2  # a source may be back next time


def test_today_is_never_cached(monkeypatch):
    calls = _counting(monkeypatch)
    today = datetime.now(MARKET_TZ).date()  # not over yet
    agent.news_rag(_state(today))
    agent.news_rag(_state(today))
    assert len(calls) == 2


def test_a_past_10am_decision_is_cached_apart_from_the_close(monkeypatch):
    calls = _counting(monkeypatch)
    ten = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)  # 10:00 New York
    agent.news_rag(_state(PAST, decided_at=ten))
    agent.news_rag(_state(PAST, "breakout", decided_at=ten))  # another strategy, same morning: shared
    agent.news_rag(_state(PAST))  # the 15:30 decision sees more news: its own entry
    assert len(calls) == 2


def test_simultaneous_callers_share_one_analysis(monkeypatch):
    calls = []
    gate = threading.Event()

    def slow(state):
        calls.append(1)
        gate.wait(2)
        return {"headlines": [], "catalysts": [], "sentiment": "neutral", "steps": [], "cacheable": True}

    monkeypatch.setattr(agent, "_analyze_news", slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(agent.news_rag(_state(PAST)))) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.2)  # let the others queue up behind the first
    gate.set()
    for t in threads:
        t.join(5)
    assert len(results) == 5 and len(calls) == 1


def test_cache_is_bounded(monkeypatch):
    _counting(monkeypatch)
    monkeypatch.setattr(agent, "NEWS_CACHE_SIZE", 3)
    for d in range(5):
        agent.news_rag(_state(PAST + timedelta(days=d)))
    assert len(agent._news_cache) == 3
