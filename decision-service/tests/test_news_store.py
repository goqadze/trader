"""The news store against a REAL Postgres + pgvector database (the news-db container).

`make test-decision` sets TEST_NEWS_DATABASE_URL to a separate `news_test` database; without it these
tests are skipped. OpenAI is never called: embeddings are small hand-made vectors whose similarities
are known exactly, so the SQL filtering and cosine math can be checked to the decimal.
"""

import math
import os
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from app import news
from app.news_store import _SCHEMA, EMBEDDING_DIM, NewsStore

URL = os.getenv("TEST_NEWS_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs TEST_NEWS_DATABASE_URL (run via `make test-decision`)")

AS_OF = date(2025, 6, 2)
CUTOFF = news.decision_cutoff(AS_OF)
CUTOFF_DT = datetime.fromtimestamp(CUTOFF, tz=timezone.utc)
HOUR = timedelta(hours=1)


def vec(x: float, y: float = 0.0) -> list[float]:
    """A unit vector in the first two of 1536 dimensions: cosine similarity between vec(1,0) and vec(a,b) is a/|(a,b)|."""
    n = math.hypot(x, y)
    return [x / n, y / n] + [0.0] * (EMBEDDING_DIM - 2)


QUERY = vec(1, 0)


@pytest.fixture(scope="module")
def store():
    conninfo = psycopg.conninfo.conninfo_to_dict(URL)
    if not conninfo.get("dbname", "").endswith("_test"):
        raise RuntimeError("TEST_NEWS_DATABASE_URL must name a database ending in _test (tests delete rows)")
    admin = psycopg.conninfo.make_conninfo(URL, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (conninfo["dbname"],)).fetchone():
            conn.execute(f'CREATE DATABASE "{conninfo["dbname"]}"')
    s = NewsStore(URL)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def empty(store):
    with store._conn() as conn:
        conn.execute("TRUNCATE news")


def row(id, symbol="AAPL", published=CUTOFF_DT - HOUR, embedding=None, document=None, source="alpaca"):
    document = document or id  # default the text to the id, so results are easy to tell apart
    return {"id": id, "symbol": symbol, "source": source, "published_at": published, "headline": document,
            "summary": "", "document": document, "embedding": embedding or vec(1, 0)}


def test_schema_uses_pgvector_and_is_safe_to_create_twice(store):
    with store._conn() as conn:
        assert conn.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'").fetchone()
        conn.execute(_SCHEMA)  # a restart runs it again: must not fail


def test_candidates_are_filtered_to_symbol_and_window_with_exact_cosine_similarity(store):
    store.upsert([
        row("in-exact", embedding=vec(1, 0)),                                   # similarity 1.0
        row("in-diagonal", embedding=vec(1, 1)),                                # similarity 0.7071
        row("in-orthogonal", embedding=vec(0, 1)),                              # similarity 0.0
        row("other-symbol", symbol="MSFT"),                                     # wrong symbol
        row("too-old", published=CUTOFF_DT - timedelta(days=8)),               # outside the 7-day window
        row("after-cutoff", published=CUTOFF_DT + HOUR),                        # look-ahead: must never appear
    ])
    found = store.candidates("AAPL", CUTOFF_DT - timedelta(days=7), CUTOFF_DT, QUERY)

    assert [(c["document"], round(c["similarity"], 4)) for c in found] == [  # best match first
        ("in-exact", 1.0), ("in-diagonal", 0.7071), ("in-orthogonal", 0.0),
    ]
    assert round(float(found[1]["embedding"][1]), 4) == 0.7071  # each article's own vector: repeats can be told apart


def test_upsert_is_idempotent(store):
    store.upsert([row("a-1")])
    store.upsert([row("a-1", document="Apple news, updated summary")])
    found = store.candidates("AAPL", CUTOFF_DT - timedelta(days=7), CUTOFF_DT, QUERY)
    assert [c["document"] for c in found] == ["Apple news, updated summary"]


def test_one_article_about_two_symbols_is_found_for_both(store):
    """The Chroma version keyed rows by article id only, so ingesting an AAPL+MSFT article for MSFT
    overwrote its AAPL tag and AAPL searches lost it. Keyed by (id, symbol), both keep it."""
    store.upsert([row("alpaca-77", symbol="AAPL", document="Apple and Microsoft sign AI deal")])
    store.upsert([row("alpaca-77", symbol="MSFT", document="Apple and Microsoft sign AI deal")])
    window = (CUTOFF_DT - timedelta(days=7), CUTOFF_DT, QUERY)
    assert len(store.candidates("AAPL", *window)) == 1
    assert len(store.candidates("MSFT", *window)) == 1


def test_existing_embeddings_come_back_for_reuse(store):
    store.upsert([row("a-1", embedding=vec(1, 1))])
    known = store.existing_embeddings(["a-1", "never-seen"])
    assert list(known) == ["a-1"]
    assert round(float(known["a-1"][0]), 4) == 0.7071


def test_end_to_end_ingest_then_search_with_recency_ranking(store, monkeypatch):
    """The real flow the agent runs: fetch -> embed only new -> store -> search -> recency re-rank."""
    articles = [
        {"id": "x-fresh", "headline": "Apple ships new iPhone", "summary": "", "ts": CUTOFF - 2 * 3600, "source": "x"},
        {"id": "x-stale", "headline": "Apple opens a store", "summary": "", "ts": CUTOFF - 5 * 86400, "source": "x"},
        {"id": "x-future", "headline": "Apple after-hours news", "summary": "", "ts": CUTOFF + 3600, "source": "x"},
    ]
    vectors = {"Apple ships new iPhone.": vec(1, 1), "Apple opens a store.": vec(1, 0)}  # stale is MORE similar
    calls = []

    def fake_embed(texts):
        calls.append(list(texts))
        return [vectors.get(t, QUERY) for t in texts]

    monkeypatch.setattr(news, "SOURCES", {"x": ((), lambda s, a, b: articles)})
    monkeypatch.setattr(news, "_embed", fake_embed)
    monkeypatch.setattr(news, "_get_store", lambda: store)
    news._query_vector.cache_clear()

    assert news.ingest_news("AAPL", AS_OF) == {"x": "2", "newly embedded": "2"}  # the future article is dropped
    assert news.ingest_news("AAPL", AS_OF) == {"x": "2", "newly embedded": "0"}  # second run: nothing re-embedded

    out = news.search_news("AAPL", AS_OF, k=5)
    # Fresh (similarity 0.71, 2h old) beats stale (similarity 1.0, 5 days old) after the recency decay
    assert [i["text"] for i in out] == ["[x, 2h ago] Apple ships new iPhone.", "[x, 5d ago] Apple opens a store."]
