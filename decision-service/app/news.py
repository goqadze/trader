import os
from datetime import date, datetime, timedelta, timezone

import chromadb
import httpx
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

# Local vector database; /data is a Docker volume so news survives restarts
_client = chromadb.PersistentClient(path="/data/chroma")

# Every source is converted to the same shape:
# {"id", "headline", "summary", "day": date, "source"}


def _to_int(d: date) -> int:
    """2025-06-02 -> 20250602. Chroma filters compare numbers, not dates."""
    return d.year * 10000 + d.month * 100 + d.day


def _collection():
    """Open (or create) the 'news' collection; OpenAI turns text into embedding vectors for search."""
    ef = OpenAIEmbeddingFunction(api_key=os.environ["OPENAI_API_KEY"], model_name="text-embedding-3-small")
    return _client.get_or_create_collection("news", embedding_function=ef)


def _get(url: str, **kw):
    """HTTP GET returning parsed JSON; raises on 4xx/5xx so callers can report the failure."""
    r = httpx.get(url, timeout=20, **kw)
    r.raise_for_status()
    return r.json()


def _alpaca(symbol: str, start: date, end: date) -> list[dict]:
    """Alpaca News API (Benzinga-sourced); deep free history, so it's the best fit for backtests."""
    data = _get(
        "https://data.alpaca.markets/v1beta1/news",
        headers={"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]},
        params={"symbols": symbol, "start": f"{start}T00:00:00Z", "end": f"{end + timedelta(days=1)}T00:00:00Z", "limit": 50, "sort": "desc"},
    )
    # Convert each article to the common shape; prefix the id with the source to avoid id collisions
    return [
        {
            "id": f"alpaca-{a['id']}",
            "headline": a["headline"],
            "summary": a.get("summary") or "",
            "day": datetime.fromisoformat(a["created_at"].replace("Z", "+00:00")).date(),
            "source": "alpaca",
        }
        for a in data.get("news", [])
    ]


def _polygon(symbol: str, start: date, end: date) -> list[dict]:
    """Polygon.io news; the free tier is rate-limited (~5 calls/min)."""
    data = _get(
        "https://api.polygon.io/v2/reference/news",
        params={
            "ticker": symbol,
            "published_utc.gte": f"{start}T00:00:00Z",
            "published_utc.lte": f"{end}T23:59:59Z",
            "order": "desc",
            "limit": 50,
            "apiKey": os.environ["POLYGON_API_KEY"],
        },
    )
    return [
        {
            "id": f"polygon-{a['id']}",
            "headline": a["title"],
            "summary": a.get("description") or "",
            "day": datetime.fromisoformat(a["published_utc"].replace("Z", "+00:00")).date(),
            "source": "polygon",
        }
        for a in data.get("results", [])
    ]


def _finnhub(symbol: str, start: date, end: date) -> list[dict]:
    """Finnhub company news; free plan only covers roughly the last year."""
    data = _get(
        "https://finnhub.io/api/v1/company-news",
        params={"symbol": symbol, "from": str(start), "to": str(end), "token": os.environ["FINNHUB_API_KEY"]},
    )
    return [
        {
            "id": f"finnhub-{a['id']}",
            "headline": a["headline"],
            "summary": a.get("summary") or "",
            "day": datetime.fromtimestamp(a["datetime"], tz=timezone.utc).date(),  # Finnhub gives a unix timestamp
            "source": "finnhub",
        }
        for a in data[:50]  # cap at 50 items per call
    ]


# name -> (env vars required, fetcher). To add a source: write a fetcher and register it here.
SOURCES = {
    "alpaca": (("ALPACA_API_KEY", "ALPACA_SECRET_KEY"), _alpaca),
    "polygon": (("POLYGON_API_KEY",), _polygon),
    "finnhub": (("FINNHUB_API_KEY",), _finnhub),
}


def enabled_sources() -> list[str]:
    """Sources whose API keys are all set in the environment."""
    return [n for n, (envs, _) in SOURCES.items() if all(os.getenv(e) for e in envs)]


def ingest_news(symbol: str, as_of: date, days: int = 14) -> dict[str, str]:
    """Pull every enabled source for the window ending at as_of. One source failing doesn't stop the rest."""
    start = as_of - timedelta(days=days)
    report, docs, ids, metas = {}, [], [], []  # report = per-source count or error, shown in the steps trail
    for name in enabled_sources():
        try:
            articles = [a for a in SOURCES[name][1](symbol, start, as_of) if a["day"] <= as_of]  # no look-ahead
        except Exception as e:
            report[name] = f"failed ({e})"
            continue
        report[name] = str(len(articles))
        for a in articles:
            docs.append(f"{a['headline']}. {a['summary']}".strip())  # the text that gets embedded
            ids.append(a["id"])
            metas.append({"symbol": symbol, "day": _to_int(a["day"]), "source": a["source"]})  # used for filtering
    if docs:
        # upsert = insert or update by id, so re-running the same request doesn't create duplicates
        _collection().upsert(documents=docs, ids=ids, metadatas=metas)
    return report


def search_news(symbol: str, as_of: date, k: int = 8) -> list[str]:
    """Top-k relevant items for the symbol from on/before as_of, tagged with their source."""
    res = _collection().query(
        query_texts=[f"{symbol} stock outlook, earnings, risks"],  # semantic query: what matters for the outlook
        n_results=k,
        # Only this symbol and only articles dated on/before as_of (again: no look-ahead)
        where={"$and": [{"symbol": symbol}, {"day": {"$lte": _to_int(as_of)}}]},
    )
    if not res["documents"]:
        return []
    # Prefix each item with its source, e.g. "[alpaca] Apple beats earnings..."
    return [f"[{m['source']}] {d}" for d, m in zip(res["documents"][0], res["metadatas"][0])]
