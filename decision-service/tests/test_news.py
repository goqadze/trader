"""Tests for the time-aware news retrieval in app/news.py.

No network, no OpenAI, no real Chroma: sources and the collection are replaced with fakes,
so we can check the look-ahead cutoff, the lookback window and the recency re-ranking exactly.
"""

from datetime import date, datetime, timezone

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


def test_rank_prefers_fresh_news_over_slightly_more_similar_old_news():
    docs = ["Apple opens store", "Apple launches product"]
    metas = [
        {"source": "alpaca", "ts": CUTOFF - 5 * 24 * HOUR},  # 5 days old, a bit more similar
        {"source": "finnhub", "ts": CUTOFF - 2 * HOUR},  # 2 hours old
    ]
    out = news._rank(docs, metas, [0.4, 0.6], CUTOFF, k=2)
    assert out == ["[finnhub, 2h ago] Apple launches product", "[alpaca, 5d ago] Apple opens store"]


def test_rank_keeps_week_old_earnings_above_week_old_gossip_and_truncates_to_k():
    week = CUTOFF - 7 * 24 * HOUR
    docs = ["Apple opens store", "Apple beats earnings", "Apple CEO interview"]
    metas = [{"source": "a", "ts": week}, {"source": "b", "ts": week}, {"source": "c", "ts": week}]
    out = news._rank(docs, metas, [0.5, 0.5, 0.5], CUTOFF, k=1)
    assert out == ["[b, 7d ago] Apple beats earnings"]


class FakeCollection:
    def __init__(self, result=None):
        self.result = result
        self.query_kwargs = None
        self.upserted = None

    def query(self, **kw):
        self.query_kwargs = kw
        return self.result

    def upsert(self, **kw):
        self.upserted = kw


def test_search_filters_to_lookback_window_and_reranks(monkeypatch):
    fake = FakeCollection({
        "documents": [["old", "new"]],
        "metadatas": [[{"source": "a", "ts": CUTOFF - 3 * 24 * HOUR}, {"source": "b", "ts": CUTOFF - HOUR}]],
        "distances": [[0.5, 0.5]],
    })
    monkeypatch.setattr(news, "_collection", lambda: fake)

    out = news.search_news("AAPL", AS_OF, k=2)

    assert out == ["[b, 1h ago] new", "[a, 3d ago] old"]
    assert fake.query_kwargs["n_results"] == 6  # over-fetch 3x before re-ranking
    assert fake.query_kwargs["where"] == {"$and": [
        {"symbol": "AAPL"},
        {"ts": {"$gte": CUTOFF - news.LOOKBACK_DAYS * 24 * HOUR}},
        {"ts": {"$lte": CUTOFF}},
    ]}


def test_search_with_no_matches_returns_empty(monkeypatch):
    fake = FakeCollection({"documents": [[]], "metadatas": [[]], "distances": [[]]})
    monkeypatch.setattr(news, "_collection", lambda: fake)
    assert news.search_news("AAPL", AS_OF) == []


def test_ingest_drops_news_published_after_the_cutoff(monkeypatch):
    def fake_source(symbol, start, end):
        assert (start, end) == (date(2025, 5, 26), AS_OF)  # LOOKBACK_DAYS window
        return [
            {"id": "x-1", "headline": "Before", "summary": "", "ts": CUTOFF - HOUR, "source": "x"},
            {"id": "x-2", "headline": "After-hours earnings", "summary": "", "ts": CUTOFF + HOUR, "source": "x"},
        ]

    fake = FakeCollection()
    monkeypatch.setattr(news, "SOURCES", {"x": ((), fake_source)})
    monkeypatch.setattr(news, "_collection", lambda: fake)

    report = news.ingest_news("AAPL", AS_OF)

    assert report == {"x": "1"}
    assert fake.upserted["ids"] == ["x-1"]
    assert fake.upserted["metadatas"] == [{"symbol": "AAPL", "ts": CUTOFF - HOUR, "source": "x"}]


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
