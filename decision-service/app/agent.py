import logging
import os
import threading
from collections import OrderedDict
from datetime import date, datetime
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from pydantic import BaseModel

from .observability import trace_config
from .risk import position_size
from .strategies import DEFAULT_STRATEGY, STRATEGIES, NewsView, format_facts
from .tools import get_prices, project_partial_volume

logger = logging.getLogger("decision-service")


class State(TypedDict, total=False):
    """Data passed between graph nodes. Each node returns only the keys it wants to update."""

    symbol: str
    as_of: date
    decided_at: datetime  # optional: the exact decision moment (live bots); news after it is ignored
    strategy: str  # which decision strategy runs (a key of strategies.STRATEGIES)
    account_balance: float  # used to size a BUY (risk a fixed % of this)
    stop_pct: float  # optional per-request stop-loss distance (else STOP_PCT env)
    target_pct: float  # optional per-request target distance (else TARGET_PCT env)
    indicators: dict  # the strategy's facts: indicator values and patterns, always incl. last_close
    action: str  # BUY | SELL | HOLD
    confidence: float
    headlines: list[str]  # news retrieved from the vector store
    catalysts: list[str]  # the fresh (<48h) company-specific events among them (earnings, upgrades, deals, ...)
    sentiment: str  # bullish | bearish | neutral | unavailable
    position: dict  # sizing for a BUY: shares, entry, stop, target, risk/reward
    steps: list[str]  # "show your work" trail
    reasoning: str  # final human-readable explanation


def fetch_data(state: State) -> State:
    """Node 1: download prices up to as_of and compute what the chosen strategy looks at."""
    strat = STRATEGIES[state.get("strategy", DEFAULT_STRATEGY)]
    df = get_prices(state["symbol"], state["as_of"])
    # Every indicator needs a warm-up (SMA200 needs 200 bars); fewer bars would make the signal meaningless
    if len(df) < strat.min_bars:
        raise ValueError(f"Not enough price history for {state['symbol']} as of {state['as_of']}: "
                         f"{strat.name} needs {strat.min_bars} daily bars, got {len(df)}")
    steps = [f"Strategy: {strat.name}", f"Fetched {len(df)} daily bars up to {state['as_of']}"]
    df, note = project_partial_volume(df, state.get("decided_at"))
    if note:
        steps.append(note)
    facts = strat.analyze(df)
    return {"indicators": facts, "steps": steps + [f"Indicators: {format_facts(facts)}"]}


# The OpenAI account has a tokens-per-minute cap; a strategy comparison sends many calls at once and hits it
# (429). The OpenAI client retries with backoff, honoring the "try again in Xms" the 429 carries.
SENTIMENT_RETRIES = 6  # the sentiment feeds the decision: keep trying through a short rate-limit burst
EXPLAIN_RETRIES = 1  # the explanation is only prose: give up fast and use the rule text instead


def _chat(**kw):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=os.getenv("LLM_MODEL", "gpt-4.1-nano"), **kw)


class Sentiment(BaseModel):
    """Structured output the LLM must return when classifying news."""

    label: str  # bullish | bearish | neutral
    rationale: str


def _news_enabled() -> bool:
    """News RAG needs an OpenAI key (embeddings + LLM) and at least one news source key."""
    from .news import enabled_sources

    return bool(os.getenv("OPENAI_API_KEY")) and bool(enabled_sources())


def news_rag(state: State) -> State:
    """Node 2 (RAG): fetch news -> store in vector DB -> retrieve relevant items -> LLM classifies sentiment,
    and flag fresh catalysts (earnings, upgrades, deals, ...) for the event strategies."""
    # No keys configured: skip gracefully so the service still works with technicals only
    if not _news_enabled():
        return {"headlines": [], "sentiment": "unavailable", "steps": state["steps"] + ["News RAG skipped (need OPENAI_API_KEY and at least one news source key)"]}
    key = _news_cache_key(state)
    news = _cached_news(key, lambda: _analyze_news(state)) if key else _analyze_news(state)
    return {"headlines": news["headlines"], "catalysts": news["catalysts"], "sentiment": news["sentiment"],
            "steps": state["steps"] + news["steps"]}


def _analyze_news(state: State) -> dict:
    """The news half of news_rag, independent of the strategy: {headlines, catalysts, sentiment, steps, cacheable}.
    cacheable is False when a source or the whole step failed, so a retry can still get the full picture."""
    from .news import MARKET_TZ, catalysts, ingest_news, search_news

    # A live bot passes its decision moment (e.g. 10:00): only news published before then counts, and
    # headline ages are measured from then. Without it (backtests): 15:30 New York on as_of.
    cutoff = int(state["decided_at"].timestamp()) if state.get("decided_at") else None
    cutoff_label = state["decided_at"].astimezone(MARKET_TZ).strftime("%H:%M") if cutoff else "15:30"
    try:
        report = ingest_news(state["symbol"], state["as_of"], cutoff=cutoff)  # pull from Alpaca/Polygon/Finnhub into Postgres/pgvector
        items = search_news(state["symbol"], state["as_of"], cutoff=cutoff)  # semantic search for the most relevant items
    except Exception as e:  # news failure must not break the signal
        return {"headlines": [], "catalysts": [], "sentiment": "unavailable", "steps": [f"News RAG failed: {e}"], "cacheable": False}
    complete = not any(str(v).startswith("failed") for v in report.values())
    if not items:
        return {"headlines": [], "catalysts": [], "sentiment": "neutral", "steps": [f"Ingested {report}, none relevant"], "cacheable": complete}
    heads = [i["text"] for i in items]
    events = catalysts(items)
    # with_structured_output forces the LLM to answer in the Sentiment schema (label + rationale).
    # temperature=0 so the same headlines classify the same way every run -> reproducible backtests.
    llm = _chat(temperature=0, max_retries=SENTIMENT_RETRIES).with_structured_output(Sentiment)
    try:
        out = llm.invoke(
            f"Classify overall sentiment for {state['symbol']} from these headlines as bullish, bearish or neutral, "
            "with a one-sentence rationale. Each headline is tagged with its age: weight recent items most; "
            "only earnings/guidance/M&A news stays relevant beyond a couple of days:\n- " + "\n- ".join(heads),
            config=trace_config(),
        )
    except Exception as e:  # e.g. still rate-limited after the retries: decide on the technicals alone
        logger.warning("news sentiment failed for %s as_of=%s: %s", state["symbol"], state["as_of"], type(e).__name__)
        return {"headlines": heads, "catalysts": events, "sentiment": "unavailable",
                "steps": [f"News sentiment failed ({type(e).__name__}): decided without news"], "cacheable": False}
    label = out.label.lower()
    steps = [f"Sources ingested (news up to {cutoff_label} New York): {report}",
             f"News ({len(heads)} retrieved): {label} - {out.rationale}"]
    if events:
        steps.append(f"Fresh catalysts (< 48h): {len(events)} - " + " | ".join(e if len(e) <= 160 else e[:157] + "..." for e in events[:3]))
    return {"headlines": heads, "catalysts": events, "sentiment": label, "steps": steps, "cacheable": complete}


# Comparing strategies replays the same symbol and days once per strategy, and the news step doesn't depend
# on the strategy. A finished past day's result is kept here, so the news APIs, embeddings and the sentiment
# LLM run once per (symbol, day) instead of once per strategy. In memory only: a restart clears it.
NEWS_CACHE_SIZE = 4096
_news_cache: OrderedDict[tuple, dict] = OrderedDict()
_news_inflight: dict[tuple, threading.Lock] = {}
_news_lock = threading.Lock()


def _news_cache_key(state: State) -> tuple | None:
    """Only backtest days that are over (a past as_of, the default 15:30 cutoff) are cached: a live bot's
    news can still change, and it decides once per slot anyway."""
    from .news import MARKET_TZ

    if state.get("decided_at") or state["as_of"] >= datetime.now(MARKET_TZ).date():
        return None
    return state["symbol"], state["as_of"]


def _cached_news(key: tuple, compute: Callable[[], dict]) -> dict:
    """compute() once per key; callers asking for the same key at the same time wait for that one result.
    /signal runs in FastAPI's thread pool, so this uses thread locks."""
    with _news_lock:
        if key in _news_cache:
            _news_cache.move_to_end(key)
            return _news_cache[key]
        key_lock = _news_inflight.setdefault(key, threading.Lock())
    with key_lock:
        with _news_lock:
            if key in _news_cache:  # computed by the caller we waited for
                return _news_cache[key]
        try:
            out = compute()
        finally:
            with _news_lock:
                _news_inflight.pop(key, None)
        if out["cacheable"]:
            with _news_lock:
                _news_cache[key] = out
                while len(_news_cache) > NEWS_CACHE_SIZE:
                    _news_cache.popitem(last=False)
    return out


def decide(state: State) -> State:
    """Node 3: the chosen strategy picks BUY/SELL/HOLD from its facts; news that agrees raises the
    confidence, news that conflicts lowers it (unless the strategy already uses news in its own rule)."""
    strat = STRATEGIES[state.get("strategy", DEFAULT_STRATEGY)]
    sent = state.get("sentiment", "unavailable")
    v = strat.decide(state["indicators"], NewsView(sent, tuple(state.get("catalysts", []))))
    conf, rule = v.confidence, v.rule
    if strat.news_tilt:
        agrees = (v.action, sent) in {("BUY", "bullish"), ("SELL", "bearish")}
        conflicts = (v.action, sent) in {("BUY", "bearish"), ("SELL", "bullish")}
        conf += 0.15 if agrees else -0.15 if conflicts else 0
        rule = f"{rule}; news {sent} -> confidence {conf:.2f}"
    return {"action": v.action, "confidence": round(max(0.0, min(1.0, conf)), 2), "steps": state["steps"] + [rule]}


def size_position(state: State) -> State:
    """Node 4: turn a BUY into an actual share count using fixed-fractional risk sizing.
    Only sizes BUYs (this is a long-only cash account); SELL/HOLD need no sizing."""
    if state.get("action") != "BUY":
        return {}
    entry = state["indicators"]["last_close"]  # buy at the latest close
    # Derive a stop and target from the entry so the caller doesn't have to supply them.
    # Defaults give a 2:1 reward:risk (target 8% up vs stop 4% down); tune via env.
    # Per-request values (a trading bot's or backtest's own settings) win over the env defaults
    stop_pct = state.get("stop_pct") or float(os.getenv("STOP_PCT", "0.04"))  # stop-loss 4% below entry
    target_pct = state.get("target_pct") or float(os.getenv("TARGET_PCT", "0.08"))  # target 8% above entry
    risk_pct = float(os.getenv("RISK_PCT", "0.02"))  # risk 2% of the account per trade
    balance = state.get("account_balance", 500.0)
    stop = round(entry * (1 - stop_pct), 2)
    target = round(entry * (1 + target_pct), 2)
    plan = position_size(balance, entry, stop, target, risk_pct)
    pos = {
        "shares": plan.shares,
        "entry": round(entry, 2),
        "stop_loss": stop,
        "target": target,
        "risk_amount": plan.risk_amount,
        "reward_amount": plan.reward_amount,
        "risk_reward_ratio": plan.risk_reward_ratio,
    }
    # 0 shares means the stock is too pricey to buy even one within the risk budget
    note = "" if plan.shares > 0 else " (0 shares: entry too large for this account's risk budget)"
    step = (
        f"Sized BUY: {plan.shares} shares @ ~${entry:.2f} "
        f"(stop ${stop}, target ${target}); risk ${plan.risk_amount}, "
        f"reward ${plan.reward_amount}, R:R {plan.risk_reward_ratio}{note}"
    )
    return {"position": pos, "steps": state["steps"] + [step]}


def explain(state: State) -> State:
    """Node 5: write a short human-readable explanation of the decision."""
    facts = "; ".join(state["steps"])
    if os.getenv("OPENAI_API_KEY"):
        try:
            msg = _chat(max_retries=EXPLAIN_RETRIES).invoke(
                f"In 2 sentences, explain this trading signal for a human trader: name the rule that fired and the "
                f"deciding numbers. Signal: {state['action']} {state['symbol']}. Facts: {facts}. Not financial advice.",
                config=trace_config(),
            )
            return {"reasoning": msg.content}
        except Exception as e:  # the explanation is optional: a failure (e.g. rate limit) must not cost the decision
            logger.warning("explanation failed for %s as_of=%s: %s", state["symbol"], state.get("as_of"), type(e).__name__)
    # Fallback without an LLM (or when it fails): just join the steps
    return {"reasoning": f"{state['action']} {state['symbol']}: {facts}"}


# --- Build the LangGraph workflow: a straight line of five nodes ---
_graph = StateGraph(State)
_graph.add_node("fetch_data", fetch_data)
_graph.add_node("news_rag", news_rag)
_graph.add_node("decide", decide)
_graph.add_node("size_position", size_position)
_graph.add_node("explain", explain)
_graph.add_edge(START, "fetch_data")
_graph.add_edge("fetch_data", "news_rag")
_graph.add_edge("news_rag", "decide")
_graph.add_edge("decide", "size_position")
_graph.add_edge("size_position", "explain")
_graph.add_edge("explain", END)
agent = _graph.compile()  # main.py calls agent.invoke({...})
