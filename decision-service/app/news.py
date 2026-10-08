import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from time import sleep
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from .news_store import NewsStore

# News vector store: Postgres + pgvector in the news-db container (see news_store.py).
# Created lazily so importing this module (e.g. in tests) never opens a database connection.
NEWS_DATABASE_URL = os.getenv("NEWS_DATABASE_URL", "postgresql://news:news@news-db:5432/news")
EMBEDDING_MODEL = "text-embedding-3-small"
_store: NewsStore | None = None

# Every source is converted to the same shape:
# {"id", "headline", "summary", "ts": unix seconds (UTC), "source"}

# A decision "as of" a day is made at 15:30 New York time (a bot's close slot, which the backtest replays), so it
# may only use news published before then -- after-hours news (e.g. 16:05 earnings) belongs to the NEXT day.
# That is the default cutoff; a decision at another moment (e.g. 10:00) passes that moment.
MARKET_TZ = ZoneInfo("America/New_York")
DECISION_TIME = time(15, 30)

# News older than this is ignored entirely; within it, relevance decays with age (see _rank).
LOOKBACK_DAYS = 7
# Two items at least this similar (cosine of their embeddings) tell the same story: the same news from two sources,
# or one of Benzinga's template articles ("Evaluating Apple Against Peers ...") published again. Only the better
# ranked one is kept, so one story can't fill several of the k places. Measured on news-db (Sep 2026): template
# repeats and same-story pairs sit at 0.84-1.0; different stories about the same company stay under it.
DUPLICATE_SIMILARITY = 0.85
HALF_LIFE_HOURS = 36  # ordinary headlines: most of the price impact is gone within a day or two
EVENT_HALF_LIFE_HOURS = 168  # earnings/guidance/M&A: known to drift for days to weeks
_EVENT_RE = re.compile(
    r"\b(earnings|eps|quarterly results|revenue|guidance|outlook|forecast|beats?|miss(es|ed)?|"
    r"acquir\w*|acquisition|merger|buyout)\b",
    re.IGNORECASE,
)


# Fresh company-specific events that move a stock for days: results and guidance, deals, analyst rating
# changes, regulators and courts. Lets the event strategies tell a real catalyst from background chatter.
CATALYST_MAX_AGE_HOURS = 48
_CATALYST_RE = re.compile(
    r"\b(earnings|eps|quarterly results|guidance|"
    r"(beat|beats|missed|misses)\s+(\w+\s+){0,3}(estimates|expectations|consensus)|"  # not "AMD beat Nvidia by 160 points"
    r"acquir\w*|acquisition|merger|buyout|takeover|upgrade[sd]?|downgrade[sd]?|price target|fda|approv(al|es|ed)|"
    r"recall(s|ed)?|lawsuit|sued|probe|investigation|buyback|repurchase|bankrupt\w*|layoffs?)\b",
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


# Pages of 50 (Alpaca's maximum) until the window is complete. A busy stock has a few hundred articles a week;
# the cap only guards against a runaway loop.
ALPACA_MAX_PAGES = 30


def _alpaca(symbol: str, start: date, end: date) -> list[dict]:
    """Alpaca News API (Benzinga-sourced); deep free history, so it's the best fit for backtests.
    Fetches EVERY article in the window, not just the newest page: otherwise what a day's decision sees would
    depend on which other days happened to be fetched before (a weekly and a daily backtest would disagree)."""
    params = {"symbols": symbol, "start": f"{start}T00:00:00Z", "end": f"{end + timedelta(days=1)}T00:00:00Z", "limit": 50, "sort": "desc"}
    headers = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
    news: list[dict] = []
    for _ in range(ALPACA_MAX_PAGES):
        data = _get("https://data.alpaca.markets/v1beta1/news", headers=headers, params=params)
        news += data.get("news", [])
        if not data.get("next_page_token"):
            break
        params = {**params, "page_token": data["next_page_token"]}
    # Convert each article to the common shape; prefix the id with the source to avoid id collisions
    return [
        {
            "id": f"alpaca-{a['id']}",
            "headline": a["headline"],
            "summary": a.get("summary") or "",
            "ts": int(datetime.fromisoformat(a["created_at"].replace("Z", "+00:00")).timestamp()),
            "source": "alpaca",
        }
        for a in news
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


FINNHUB_HISTORY_DAYS = 365  # the free plan's company news only reaches back about a year
FINNHUB_RETRY_DELAYS = (2.0, 5.0, 10.0)  # the free plan allows 60 calls a minute: wait out a 429


def _finnhub(symbol: str, start: date, end: date) -> list[dict]:
    """Finnhub company news (many publishers, Benzinga among them: _rank drops the repeats). A window older than
    the free plan's year is simply not covered: no request, no articles, not a failure."""
    earliest = date.today() - timedelta(days=FINNHUB_HISTORY_DAYS)
    if end < earliest:
        return []
    params = {"symbol": symbol, "from": str(max(start, earliest)), "to": str(end), "token": os.environ["FINNHUB_API_KEY"]}
    for delay in (*FINNHUB_RETRY_DELAYS, None):
        try:
            data = _get("https://finnhub.io/api/v1/company-news", params=params)
            break
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429 or delay is None:
                raise
            sleep(delay)
    return [
        {
            "id": f"finnhub-{a['id']}",
            "headline": a["headline"],
            "summary": a.get("summary") or "",
            "ts": int(a["datetime"]),  # Finnhub already gives a unix timestamp
            "source": "finnhub",
        }
        for a in data
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


def _has_key(env: str) -> bool:
    """Set, and not a commented-out placeholder like "# paste your key here" (which only earns a 401 per call)."""
    value = (os.getenv(env) or "").strip()
    return bool(value) and not value.startswith("#")


def enabled_sources() -> list[str]:
    """Sources whose API keys are all set in the environment."""
    return [n for n, (envs, _) in SOURCES.items() if all(_has_key(e) for e in envs)]


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


def _rank(candidates: list[dict], cutoff: int, k: int) -> list[dict]:
    """Re-rank semantic matches so fresh news wins: score = similarity * 0.5 ** (age / half-life), then keep the top
    k that aren't near-duplicates (DUPLICATE_SIMILARITY) of a better ranked one. Each candidate is {"source",
    "document", "ts", "similarity", "embedding" (optional: without it nothing counts as a repeat)}. Returns
    {"text": "[source, age] document" (what the LLM reads), "document", "age_hours"}."""
    scored = []
    for c in candidates:
        age_h = (cutoff - c["ts"]) / 3600
        score = max(0.0, c["similarity"]) * math.pow(0.5, age_h / _half_life_hours(c["document"]))
        scored.append((score, c, age_h))
    scored.sort(key=lambda x: x[0], reverse=True)
    kept, seen = [], []  # seen: unit vectors of the stories already kept
    for _, c, age_h in scored:
        if len(kept) == k:
            break
        e = c.get("embedding")
        if e is not None:
            e = np.asarray(e, dtype=np.float32)
            e = e / (np.linalg.norm(e) or 1.0)
            if any(float(e @ s) >= DUPLICATE_SIMILARITY for s in seen):
                continue  # the same story, already in from a better ranked copy
            seen.append(e)
        kept.append({"text": f"[{c['source']}, {_age_label(age_h)}] {c['document']}", "document": c["document"], "age_hours": age_h})
    return kept


def catalysts(items: list[dict]) -> list[str]:
    """The retrieved items that report a company-specific event from the last CATALYST_MAX_AGE_HOURS."""
    return [i["text"] for i in items if i["age_hours"] <= CATALYST_MAX_AGE_HOURS and _CATALYST_RE.search(i["document"])]


def search_news(symbol: str, as_of: date, k: int = 8, cutoff: int | None = None) -> list[dict]:
    """Top-k items (see _rank) for the symbol from the LOOKBACK_DAYS before the decision cutoff, relevance x recency.
    The database returns EVERY article in the window (exact similarity, no approximate index), so an
    older-but-relevant or newer-but-less-similar item is never lost before the recency re-ranking.
    cutoff: as in ingest_news; headline ages ("2h ago") are measured from it."""
    cutoff = cutoff or decision_cutoff(as_of)
    end = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    found = _get_store().candidates(symbol, end - timedelta(days=LOOKBACK_DAYS), end, list(_query_vector(symbol)))
    return _rank(found, cutoff, k)
