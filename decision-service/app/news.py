import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import httpx

from .news_store import NewsStore

# News vector store: Postgres + pgvector in the news-db container (see news_store.py).
# Created lazily so importing this module (e.g. in tests) never opens a database connection.
NEWS_DATABASE_URL = os.getenv("NEWS_DATABASE_URL", "postgresql://news:news@news-db:5432/news")
EMBEDDING_MODEL = "text-embedding-3-small"
_store: NewsStore | None = None

# Every source is converted to the same shape:
# {"id", "headline", "summary", "ts": unix seconds (UTC), "source"}

# The backtest fills at the day's close, so a decision "as of" a day may only use news published
# before ~15:30 New York time -- after-hours news (e.g. 16:05 earnings) belongs to the NEXT day.
# That is the default cutoff; a live bot deciding at another time (e.g. 10:00) passes its exact moment.
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


def _get_store() -> NewsStore:
    global _store
    if _store is None:
        _store = NewsStore(NEWS_DATABASE_URL)
    return _store


def _embed(texts: list[str]) -> list[list[float]]:
    """OpenAI turns each text into a 1536-number vector; texts with similar meaning get similar vectors."""
    from openai import OpenAI

    resp = OpenAI().embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [d.embedding for d in resp.data]


@lru_cache(maxsize=256)
def _query_vector(symbol: str) -> tuple[float, ...]:
    """The search question for a symbol never changes, so embed it once per symbol, not once per decision."""
    return tuple(_embed([f"{symbol} stock outlook, earnings, risks"])[0])


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


def _safe_error(e: Exception) -> str:
    """Error text that is safe to store and display. httpx errors embed the full request URL, and
    Polygon/Finnhub take the API key as a URL parameter (apiKey=, token=), so the raw message would
    leak secrets into the decision history, the UI and the LLM traces."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from {e.request.url.host}"
    return re.sub(r"\?[^\s'\"]*", "?<redacted>", str(e))  # drop every query string


def enabled_sources() -> list[str]:
    """Sources whose API keys are all set in the environment."""
    return [n for n, (envs, _) in SOURCES.items() if all(os.getenv(e) for e in envs)]


def ingest_news(symbol: str, as_of: date, days: int = LOOKBACK_DAYS, cutoff: int | None = None) -> dict[str, str]:
    """Pull every enabled source for the window ending at as_of and store it. One source failing doesn't
    stop the rest. Only articles never seen before are embedded, so re-running a window costs nothing.
    cutoff: unix time after which news is ignored (default: 15:30 New York on as_of)."""
    start = as_of - timedelta(days=days)
    cutoff = cutoff or decision_cutoff(as_of)
    report: dict[str, str] = {}  # per-source count or error, shown in the decision's steps trail
    articles: dict[str, dict] = {}  # by id: the same article can't be stored twice in one batch
    for name in enabled_sources():
        try:
            found = [a for a in SOURCES[name][1](symbol, start, as_of) if a["ts"] <= cutoff]  # no look-ahead
        except Exception as e:
            report[name] = f"failed ({_safe_error(e)})"
            continue
        report[name] = str(len(found))
        for a in found:
            articles[a["id"]] = a
    if not articles:
        return report

    store = _get_store()
    known = store.existing_embeddings(list(articles))  # stored earlier, possibly for another symbol
    docs = {aid: f"{a['headline']}. {a['summary']}".strip() for aid, a in articles.items()}  # the text that gets embedded
    new_ids = [aid for aid in articles if aid not in known]
    if new_ids:
        known.update(zip(new_ids, _embed([docs[aid] for aid in new_ids])))
    report["newly embedded"] = str(len(new_ids))

    store.upsert([
        {
            "id": aid, "symbol": symbol, "source": a["source"],
            "published_at": datetime.fromtimestamp(a["ts"], tz=timezone.utc),
            "headline": a["headline"], "summary": a["summary"], "document": docs[aid], "embedding": known[aid],
        }
        for aid, a in articles.items()
    ])
    return report


def _rank(candidates: list[dict], cutoff: int, k: int) -> list[str]:
    """Re-rank semantic matches so fresh news wins: score = similarity * 0.5 ** (age / half-life).
    Each candidate is {"source", "document", "ts", "similarity"}. Returns the top k as '[source, age] text'."""
    scored = []
    for c in candidates:
        age_h = (cutoff - c["ts"]) / 3600
        score = max(0.0, c["similarity"]) * math.pow(0.5, age_h / _half_life_hours(c["document"]))
        scored.append((score, f"[{c['source']}, {_age_label(age_h)}] {c['document']}"))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [text for _, text in scored[:k]]


def search_news(symbol: str, as_of: date, k: int = 8, cutoff: int | None = None) -> list[str]:
    """Top-k items for the symbol from the LOOKBACK_DAYS before the decision cutoff, relevance x recency.
    The database returns EVERY article in the window (exact similarity, no approximate index), so an
    older-but-relevant or newer-but-less-similar item is never lost before the recency re-ranking.
    cutoff: as in ingest_news; headline ages ("2h ago") are measured from it."""
    cutoff = cutoff or decision_cutoff(as_of)
    end = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    found = _get_store().candidates(symbol, end - timedelta(days=LOOKBACK_DAYS), end, list(_query_vector(symbol)))
    return _rank(found, cutoff, k)
