import math
import os
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import chromadb
import httpx
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

# Local vector database; /data is a Docker volume so news survives restarts.
# Opened lazily so importing this module (e.g. in tests) doesn't touch the disk.
_client = None

# Every source is converted to the same shape:
# {"id", "headline", "summary", "ts": unix seconds (UTC), "source"}

# The backtest fills at the day's close, so a decision "as of" a day may only use news published
# before ~15:30 New York time -- after-hours news (e.g. 16:05 earnings) belongs to the NEXT day.
MARKET_TZ = ZoneInfo("America/New_York")
DECISION_TIME = time(15, 30)

# News older than this is ignored entirely; within it, relevance decays with age (see _rank).
LOOKBACK_DAYS = 7
HALF_LIFE_HOURS = 36  # ordinary headlines: most of the price impact is gone within a day or two
EVENT_HALF_LIFE_HOURS = 168  # earnings/guidance/M&A: known to drift for days to weeks
_EVENT_RE = re.compile(
    r"\b(earnings|eps|quarterly results|revenue|guidance|outlook|forecast|beats?|miss(es|ed)?|"
    r"acquir\w*|acquisition|merger|buyout)\b",
    re.IGNORECASE,
)


def decision_cutoff(as_of: date) -> int:
    """Unix timestamp of 15:30 New York time on as_of: the latest moment news can influence that day's trade."""
    return int(datetime.combine(as_of, DECISION_TIME, tzinfo=MARKET_TZ).timestamp())


def _half_life_hours(text: str) -> float:
    """Earnings/guidance/M&A news keeps mattering for longer than ordinary headlines."""
    return EVENT_HALF_LIFE_HOURS if _EVENT_RE.search(text) else HALF_LIFE_HOURS


def _age_label(hours: float) -> str:
    """Human-readable age for the LLM prompt: '3h ago' or '2d ago'."""
    return f"{max(0, round(hours))}h ago" if hours < 24 else f"{round(hours / 24)}d ago"


def _collection():
    """Open (or create) the 'news' collection; OpenAI turns text into embedding vectors for search."""
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path="/data/chroma")
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
            "ts": int(datetime.fromisoformat(a["created_at"].replace("Z", "+00:00")).timestamp()),
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
            "ts": int(datetime.fromisoformat(a["published_utc"].replace("Z", "+00:00")).timestamp()),
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
            "ts": int(a["datetime"]),  # Finnhub already gives a unix timestamp
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


def ingest_news(symbol: str, as_of: date, days: int = LOOKBACK_DAYS) -> dict[str, str]:
    """Pull every enabled source for the window ending at as_of. One source failing doesn't stop the rest."""
    start = as_of - timedelta(days=days)
    cutoff = decision_cutoff(as_of)
    report, docs, ids, metas = {}, [], [], []  # report = per-source count or error, shown in the steps trail
    for name in enabled_sources():
        try:
            articles = [a for a in SOURCES[name][1](symbol, start, as_of) if a["ts"] <= cutoff]  # no look-ahead
        except Exception as e:
            report[name] = f"failed ({e})"
            continue
        report[name] = str(len(articles))
        for a in articles:
            docs.append(f"{a['headline']}. {a['summary']}".strip())  # the text that gets embedded
            ids.append(a["id"])
            metas.append({"symbol": symbol, "ts": a["ts"], "source": a["source"]})  # used for filtering (Chroma compares numbers)
    if docs:
        # upsert = insert or update by id, so re-running the same request doesn't create duplicates
        _collection().upsert(documents=docs, ids=ids, metadatas=metas)
    return report


def _rank(docs: list[str], metas: list[dict], distances: list[float], cutoff: int, k: int) -> list[str]:
    """Re-rank semantic matches so fresh news wins: score = similarity * 0.5 ** (age / half-life).
    Returns the top k as '[source, age] text', newest-weighted."""
    scored = []
    for d, m, dist in zip(docs, metas, distances):
        # Chroma's default distance is squared L2; OpenAI vectors are unit length, so cosine = 1 - dist/2
        similarity = max(0.0, 1 - dist / 2)
        age_h = (cutoff - m["ts"]) / 3600
        score = similarity * math.pow(0.5, age_h / _half_life_hours(d))
        scored.append((score, f"[{m['source']}, {_age_label(age_h)}] {d}"))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [text for _, text in scored[:k]]


def search_news(symbol: str, as_of: date, k: int = 8) -> list[str]:
    """Top-k items for the symbol from the LOOKBACK_DAYS before the decision cutoff, relevance x recency."""
    cutoff = decision_cutoff(as_of)
    res = _collection().query(
        query_texts=[f"{symbol} stock outlook, earnings, risks"],  # semantic query: what matters for the outlook
        n_results=k * 3,  # over-fetch, then _rank re-orders by recency and keeps k
        # Only this symbol, only the lookback window, nothing after the cutoff (no look-ahead).
        # Rows ingested before timestamps existed have no "ts" and are skipped automatically.
        where={"$and": [
            {"symbol": symbol},
            {"ts": {"$gte": cutoff - LOOKBACK_DAYS * 86400}},
            {"ts": {"$lte": cutoff}},
        ]},
    )
    if not res["documents"] or not res["documents"][0]:
        return []
    return _rank(res["documents"][0], res["metadatas"][0], res["distances"][0], cutoff, k)
