"""Tests for the time-aware news retrieval in app/news.py.

No network, no OpenAI, no database: sources, embeddings and the store are replaced with fakes,
so we can check the look-ahead cutoff, the lookback window and the recency re-ranking exactly.
(test_news_store.py runs the same flow against a real Postgres + pgvector database.)
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app import news

AS_OF = date(2025, 6, 2)  # a Monday, New York is on summer time (UTC-4)
CUTOFF = news.decision_cutoff(AS_OF)
HOUR = 3600


def test_cutoff_is_1530_new_york_time_in_summer_and_winter():
    assert CUTOFF == int(datetime(2025, 6, 2, 19, 30, tzinfo=timezone.utc).timestamp())  # EDT: UTC-4
    winter = news.decision_cutoff(date(2025, 1, 6))
    assert winter == int(datetime(2025, 1, 6, 20, 30, tzinfo=timezone.utc).timestamp())  # EST: UTC-5


def test_event_news_decays_slower_than_ordinary_news():
    assert news._half_life_hours("Apple beats Q2 earnings estimates") == news.EVENT_HALF_LIFE_HOURS
    assert news._half_life_hours("Microsoft raises full-year guidance") == news.EVENT_HALF_LIFE_HOURS
    assert news._half_life_hours("Apple opens new store in Mumbai") == news.HALF_LIFE_HOURS


def test_age_label():
    assert news._age_label(3.4) == "3h ago"
    assert news._age_label(-0.2) == "0h ago"
    assert news._age_label(50) == "2d ago"


def _cand(document, source, ts, similarity):
    return {"document": document, "source": source, "ts": ts, "similarity": similarity}


def test_rank_prefers_fresh_news_over_slightly_more_similar_old_news():
    out = news._rank([
        _cand("Apple opens store", "alpaca", CUTOFF - 5 * 24 * HOUR, 0.8),  # 5 days old, a bit more similar
        _cand("Apple launches product", "finnhub", CUTOFF - 2 * HOUR, 0.7),  # 2 hours old
    ], CUTOFF, k=2)
    assert out == ["[finnhub, 2h ago] Apple launches product", "[alpaca, 5d ago] Apple opens store"]


def test_rank_keeps_week_old_earnings_above_week_old_gossip_and_truncates_to_k():
    week = CUTOFF - 7 * 24 * HOUR
    out = news._rank([
        _cand("Apple opens store", "a", week, 0.75),
        _cand("Apple beats earnings", "b", week, 0.75),
        _cand("Apple CEO interview", "c", week, 0.75),
    ], CUTOFF, k=1)
    assert out == ["[b, 7d ago] Apple beats earnings"]


class FakeStore:
    """Stands in for NewsStore: records calls, returns canned candidates."""

    def __init__(self, candidates=None, known=None):
        self._candidates = candidates or []
        self.known = known or {}
        self.candidates_args = None
        self.upserted = []

    def candidates(self, symbol, start, end, query, limit=500):
        self.candidates_args = (symbol, start, end)
        return self._candidates

    def existing_embeddings(self, ids):
        return {i: self.known[i] for i in ids if i in self.known}

    def upsert(self, rows):
        self.upserted.extend(rows)


@pytest.fixture
def embed_calls(monkeypatch):
    """Fake OpenAI embeddings; records every batch of texts sent for embedding."""
    calls = []

    def fake_embed(texts):
        calls.append(list(texts))
        return [[0.1] * 3 for _ in texts]

    monkeypatch.setattr(news, "_embed", fake_embed)
    news._query_vector.cache_clear()
    return calls


def test_search_asks_the_store_for_the_lookback_window_and_reranks(monkeypatch, embed_calls):
    fake = FakeStore([_cand("old", "a", CUTOFF - 3 * 24 * HOUR, 0.75), _cand("new", "b", CUTOFF - HOUR, 0.75)])
    monkeypatch.setattr(news, "_get_store", lambda: fake)

    out = news.search_news("AAPL", AS_OF, k=2)

    assert out == ["[b, 1h ago] new", "[a, 3d ago] old"]
    cutoff_dt = datetime.fromtimestamp(CUTOFF, tz=timezone.utc)
    assert fake.candidates_args == ("AAPL", cutoff_dt - timedelta(days=news.LOOKBACK_DAYS), cutoff_dt)


def test_query_vector_is_embedded_once_per_symbol(monkeypatch, embed_calls):
    monkeypatch.setattr(news, "_get_store", lambda: FakeStore())
    news.search_news("AAPL", AS_OF)
    news.search_news("AAPL", date(2025, 6, 3))
    assert embed_calls == [["AAPL stock outlook, earnings, risks"]]


def test_search_with_no_matches_returns_empty(monkeypatch, embed_calls):
    monkeypatch.setattr(news, "_get_store", lambda: FakeStore())
    assert news.search_news("AAPL", AS_OF) == []


def test_ingest_drops_news_published_after_the_cutoff(monkeypatch, embed_calls):
    def fake_source(symbol, start, end):
        assert (start, end) == (date(2025, 5, 26), AS_OF)  # LOOKBACK_DAYS window
        return [
            {"id": "x-1", "headline": "Before", "summary": "", "ts": CUTOFF - HOUR, "source": "x"},
            {"id": "x-2", "headline": "After-hours earnings", "summary": "", "ts": CUTOFF + HOUR, "source": "x"},
        ]

    fake = FakeStore()
    monkeypatch.setattr(news, "SOURCES", {"x": ((), fake_source)})
    monkeypatch.setattr(news, "_get_store", lambda: fake)

    report = news.ingest_news("AAPL", AS_OF)

    assert report == {"x": "1", "newly embedded": "1"}
    assert [r["id"] for r in fake.upserted] == ["x-1"]
    row = fake.upserted[0]
    assert row["symbol"] == "AAPL" and row["document"] == "Before."
    assert row["published_at"] == datetime.fromtimestamp(CUTOFF - HOUR, tz=timezone.utc)


def test_ingest_only_pays_to_embed_articles_it_has_never_seen(monkeypatch, embed_calls):
    def fake_source(symbol, start, end):
        return [
            {"id": "x-old", "headline": "Seen before", "summary": "", "ts": CUTOFF - HOUR, "source": "x"},
            {"id": "x-new", "headline": "Brand new", "summary": "", "ts": CUTOFF - HOUR, "source": "x"},
        ]

    fake = FakeStore(known={"x-old": [0.9] * 3})
    monkeypatch.setattr(news, "SOURCES", {"x": ((), fake_source)})
    monkeypatch.setattr(news, "_get_store", lambda: fake)

    report = news.ingest_news("AAPL", AS_OF)

    assert embed_calls == [["Brand new."]]  # only the unseen article went to OpenAI
    assert report["newly embedded"] == "1"
    assert {r["id"]: r["embedding"] for r in fake.upserted} == {"x-old": [0.9] * 3, "x-new": [0.1] * 3}


def test_ingest_with_no_articles_touches_neither_openai_nor_the_database(monkeypatch, embed_calls):
    monkeypatch.setattr(news, "SOURCES", {"x": ((), lambda s, a, b: [])})
    monkeypatch.setattr(news, "_get_store", lambda: (_ for _ in ()).throw(AssertionError("store used")))
    assert news.ingest_news("AAPL", AS_OF) == {"x": "0"}
    assert embed_calls == []


@pytest.mark.parametrize("text", ["Apple misses revenue forecast", "Nvidia to acquire startup"])
def test_event_regex_variants(text):
    assert news._half_life_hours(text) == news.EVENT_HALF_LIFE_HOURS


def test_source_errors_never_leak_api_keys(monkeypatch):
    """httpx puts the whole URL (with apiKey=/token=) in its error text; the report must not."""
    import httpx

    req = httpx.Request("GET", "https://api.polygon.io/v2/reference/news?ticker=AAPL&apiKey=SECRET123")
    status_err = httpx.HTTPStatusError("401 Unauthorized for url " + str(req.url), request=req,
                                       response=httpx.Response(401, request=req))
    assert news._safe_error(status_err) == "HTTP 401 from api.polygon.io"

    conn_err = httpx.ConnectError("failed for https://finnhub.io/api/v1/company-news?symbol=AAPL&token=SECRET456")
    assert "SECRET456" not in news._safe_error(conn_err)

    def failing_source(symbol, start, end):
        raise status_err

    monkeypatch.setattr(news, "SOURCES", {"polygon": ((), failing_source)})
    report = news.ingest_news("AAPL", AS_OF)
    assert "SECRET123" not in report["polygon"]
