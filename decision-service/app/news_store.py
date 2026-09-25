"""News vector store on Postgres + pgvector (the news-db container).

One table, one row per (article, symbol):

    news(id, symbol, source, published_at, headline, summary, document, embedding vector(1536), ingested_at)

Why no vector index (HNSW/IVFFlat): every search first narrows to ONE symbol and a 7-day window through
the (symbol, published_at) B-tree index -- typically a few hundred rows -- and then computes exact cosine
distance on those. That is fast and exact. An approximate index would apply the symbol/date filter
AFTER picking nearest neighbours and could silently return fewer matches than asked for.
"""

import threading
from datetime import datetime

from psycopg.types.json import Jsonb

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

EMBEDDING_DIM = 1536  # OpenAI text-embedding-3-small; changing the model means changing this and re-ingesting

_SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS news (
    id           text        NOT NULL,              -- source-prefixed article id, e.g. alpaca-4213
    symbol       text        NOT NULL,              -- an article about AAPL and MSFT gets one row per symbol
    source       text        NOT NULL,              -- alpaca | polygon | finnhub
    published_at timestamptz NOT NULL,
    headline     text        NOT NULL,
    summary      text        NOT NULL DEFAULT '',
    document     text        NOT NULL,              -- the exact text that was embedded
    embedding    vector({EMBEDDING_DIM}) NOT NULL,
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, symbol)
);
CREATE INDEX IF NOT EXISTS news_symbol_published ON news (symbol, published_at DESC);
-- One news judgment per symbol and decision moment (the news cutoff): the retrieved headlines, fresh catalysts,
-- the LLM's sentiment and the steps shown. Judged once, then every backtest, comparison and restart reuses it,
-- so the same settings always give the same result. version: bump to re-judge after changing how it's done.
CREATE TABLE IF NOT EXISTS news_judgments (
    symbol     text        NOT NULL,
    cutoff     timestamptz NOT NULL,
    version    int         NOT NULL,
    result     jsonb       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, cutoff, version)
);
"""


class NewsStore:
    def __init__(self, url: str):
        self.url = url
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()

    def _conn(self):
        """Pooled connection; the schema is created on first use (idempotent, so restarts are safe)."""
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    # The extension must exist before register_vector can look up the `vector` type
                    with psycopg.connect(self.url, autocommit=True) as conn:
                        conn.execute(_SCHEMA)
                    self._pool = ConnectionPool(self.url, min_size=1, max_size=5, configure=register_vector,
                                                open=True, check=ConnectionPool.check_connection)
        return self._pool.connection()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def existing_embeddings(self, ids: list[str]) -> dict[str, np.ndarray]:
        """Embeddings already stored for these article ids (under any symbol), so they aren't paid for twice."""
        if not ids:
            return {}
        with self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT ON (id) id, embedding FROM news WHERE id = ANY(%s)", (ids,)).fetchall()
        # pgvector hands back its own Vector type; turn it into a plain numpy array for reuse in upsert()
        return {r[0]: r[1].to_numpy() if hasattr(r[1], "to_numpy") else np.asarray(r[1]) for r in rows}

    def upsert(self, rows: list[dict]) -> None:
        """Insert or refresh articles. Keyed by (id, symbol): re-ingesting the same window never duplicates."""
        if not rows:
            return
        with self._conn() as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO news (id, symbol, source, published_at, headline, summary, document, embedding)
                VALUES (%(id)s, %(symbol)s, %(source)s, %(published_at)s, %(headline)s, %(summary)s, %(document)s, %(embedding)s)
                ON CONFLICT (id, symbol) DO UPDATE SET
                    source = EXCLUDED.source, published_at = EXCLUDED.published_at, headline = EXCLUDED.headline,
                    summary = EXCLUDED.summary, document = EXCLUDED.document, embedding = EXCLUDED.embedding
                """,
                [{**r, "embedding": np.asarray(r["embedding"], dtype=np.float32)} for r in rows],
            )

    def candidates(self, symbol: str, start: datetime, end: datetime, query: list[float], limit: int = 500) -> list[dict]:
        """Every article for `symbol` published in [start, end], with its cosine similarity to `query`.
        `end` is the look-ahead cutoff: nothing published after it is ever returned."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT source, document, published_at, 1 - (embedding <=> %(q)s) AS similarity
                FROM news
                WHERE symbol = %(symbol)s AND published_at >= %(start)s AND published_at <= %(end)s
                ORDER BY embedding <=> %(q)s
                LIMIT %(limit)s
                """,
                {"q": np.asarray(query, dtype=np.float32), "symbol": symbol, "start": start, "end": end, "limit": limit},
            ).fetchall()
        return [{"source": r[0], "document": r[1], "ts": int(r[2].timestamp()), "similarity": float(r[3])} for r in rows]

    def get_judgment(self, symbol: str, cutoff: datetime, version: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT result FROM news_judgments WHERE symbol = %s AND cutoff = %s AND version = %s",
                               (symbol, cutoff, version)).fetchone()
        return row[0] if row else None

    def put_judgment(self, symbol: str, cutoff: datetime, version: int, result: dict) -> None:
        """The first judgment for a moment wins: a later one never replaces it."""
        with self._conn() as conn:
            conn.execute("INSERT INTO news_judgments (symbol, cutoff, version, result) VALUES (%s, %s, %s, %s) "
                         "ON CONFLICT DO NOTHING", (symbol, cutoff, version, Jsonb(result)))
