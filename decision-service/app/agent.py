import os
from datetime import date
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from pydantic import BaseModel

from .observability import trace_config
from .risk import position_size
from .tools import get_prices, indicators


class State(TypedDict, total=False):
    """Data passed between graph nodes. Each node returns only the keys it wants to update."""

    symbol: str
    as_of: date
    mode: str  # "rules" (SMA/RSI logic) or "llm" (the model decides)
    account_balance: float  # used to size a BUY (risk a fixed % of this)
    stop_pct: float  # optional per-request stop-loss distance (else STOP_PCT env)
    target_pct: float  # optional per-request target distance (else TARGET_PCT env)
    indicators: dict  # SMA20, SMA50, RSI14, last close
    action: str  # BUY | SELL | HOLD
    confidence: float
    headlines: list[str]  # news retrieved from the vector store
    sentiment: str  # bullish | bearish | neutral | unavailable
    position: dict  # sizing for a BUY: shares, entry, stop, target, risk/reward
    steps: list[str]  # "show your work" trail
    reasoning: str  # final human-readable explanation


def fetch_data(state: State) -> State:
    """Node 1: download prices up to as_of and compute technical indicators."""
    df = get_prices(state["symbol"], state["as_of"])
    # SMA50 needs at least 50 bars; otherwise the signal would be meaningless
    if len(df) < 50:
        raise ValueError(f"Not enough price history for {state['symbol']} as of {state['as_of']}")
    ind = indicators(df)
    return {"indicators": ind, "steps": [f"Fetched {len(df)} daily bars up to {state['as_of']}", f"Indicators: {ind}"]}


class Sentiment(BaseModel):
    """Structured output the LLM must return when classifying news."""

    label: str  # bullish | bearish | neutral
    rationale: str


class Decision(BaseModel):
    """Structured output the LLM must return when it makes the trade decision (llm mode)."""

    action: str  # BUY | SELL | HOLD
    confidence: float  # 0..1
    rationale: str


def _news_enabled() -> bool:
    """News RAG needs an OpenAI key (embeddings + LLM) and at least one news source key."""
    from .news import enabled_sources

    return bool(os.getenv("OPENAI_API_KEY")) and bool(enabled_sources())


def news_rag(state: State) -> State:
    """Node 2 (RAG): fetch news -> store in vector DB -> retrieve relevant items -> LLM classifies sentiment."""
    # No keys configured: skip gracefully so the service still works with technicals only
    if not _news_enabled():
        return {"headlines": [], "sentiment": "unavailable", "steps": state["steps"] + ["News RAG skipped (need OPENAI_API_KEY and at least one news source key)"]}
    from .news import ingest_news, search_news

    try:
        report = ingest_news(state["symbol"], state["as_of"])  # pull from Alpaca/Polygon/Finnhub into Postgres/pgvector
        heads = search_news(state["symbol"], state["as_of"])  # semantic search for the most relevant items
    except Exception as e:  # news failure must not break the signal
        return {"headlines": [], "sentiment": "unavailable", "steps": state["steps"] + [f"News RAG failed: {e}"]}
    if not heads:
        return {"headlines": [], "sentiment": "neutral", "steps": state["steps"] + [f"Ingested {report}, none relevant"]}
    from langchain_openai import ChatOpenAI

    # with_structured_output forces the LLM to answer in the Sentiment schema (label + rationale).
    # temperature=0 so the same headlines classify the same way every run -> reproducible backtests.
    llm = ChatOpenAI(model=os.getenv("LLM_MODEL", "gpt-4.1-nano"), temperature=0).with_structured_output(Sentiment)
    out = llm.invoke(
        f"Classify overall sentiment for {state['symbol']} from these headlines as bullish, bearish or neutral, "
        "with a one-sentence rationale. Each headline is tagged with its age: weight recent items most; "
        "only earnings/guidance/M&A news stays relevant beyond a couple of days:\n- " + "\n- ".join(heads),
        config=trace_config(),
    )
    label = out.label.lower()
    return {
        "headlines": heads,
        "sentiment": label,
        "steps": state["steps"] + [f"Sources ingested: {report}", f"News ({len(heads)} retrieved): {label} - {out.rationale}"],
    }


def decide(state: State) -> State:
    """Node 3: pick BUY/SELL/HOLD. Dispatches to rules or the LLM depending on mode."""
    if state.get("mode") == "llm":
        return _decide_llm(state)
    return _decide_rules(state)


def _decide_rules(state: State) -> State:
    """Deterministic placeholder strategy: SMA trend + RSI, nudged by news sentiment."""
    i = state["indicators"]
    trend_up = i["sma20"] > i["sma50"]  # short-term average above long-term = uptrend
    if trend_up and i["rsi14"] < 70:  # uptrend and not overbought
        action, conf = "BUY", 0.6
    elif not trend_up and i["rsi14"] > 30:  # downtrend and not oversold
        action, conf = "SELL", 0.6
    else:  # mixed signals
        action, conf = "HOLD", 0.4
    # Compare the technical action with news sentiment: agreement raises confidence, conflict lowers it
    sent = state.get("sentiment", "unavailable")
    agrees = (action, sent) in {("BUY", "bullish"), ("SELL", "bearish")}
    conflicts = (action, sent) in {("BUY", "bearish"), ("SELL", "bullish")}
    conf += 0.15 if agrees else -0.15 if conflicts else 0
    rule = f"SMA20 {'>' if trend_up else '<='} SMA50, RSI14={i['rsi14']:.0f} -> {action}; news {sent} -> confidence {conf:.2f}"
    return {"action": action, "confidence": conf, "steps": state["steps"] + [rule]}


def _decide_llm(state: State) -> State:
    """The LLM weighs indicators + news together and returns the decision itself, with a rationale.
    Falls back to the rules if no OpenAI key is configured."""
    if not os.getenv("OPENAI_API_KEY"):
        out = _decide_rules(state)
        out["steps"] = out["steps"] + ["LLM mode requested but no OPENAI_API_KEY; used rules instead"]
        return out

    from langchain_openai import ChatOpenAI

    i = state["indicators"]
    heads = state.get("headlines", [])
    sent = state.get("sentiment", "unavailable")
    news_block = "\n".join(f"- {h}" for h in heads) if heads else "(no news retrieved)"
    # temperature=0 for repeatable backtests
    llm = ChatOpenAI(model=os.getenv("LLM_MODEL", "gpt-4.1-nano"), temperature=0).with_structured_output(Decision)
    out = llm.invoke(
        f"You are a disciplined trading analyst. Decide BUY, SELL or HOLD for {state['symbol']} as of {state['as_of']}. "
        "Give a confidence between 0 and 1 and a one-sentence rationale. This is not financial advice.\n"
        f"Technical indicators: last_close={i['last_close']:.2f}, SMA20={i['sma20']:.2f}, "
        f"SMA50={i['sma50']:.2f}, RSI14={i['rsi14']:.1f}.\n"
        f"Overall news sentiment: {sent}.\nHeadlines:\n{news_block}",
        config=trace_config(),
    )
    # Guard against the model returning something unexpected
    action = out.action.upper().strip()
    if action not in ("BUY", "SELL", "HOLD"):
        action = "HOLD"
    conf = max(0.0, min(1.0, float(out.confidence)))
    return {
        "action": action,
        "confidence": conf,
        "reasoning": out.rationale,  # llm mode writes its own reasoning, so explain() will skip
        "steps": state["steps"] + [f"LLM decision: {action} (confidence {conf:.2f}) - {out.rationale}"],
    }


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
    """Node 5: write a short human-readable explanation (rules mode only; llm mode already wrote one)."""
    if state.get("reasoning"):  # llm decision already produced a rationale — don't spend another call
        return {}
    facts = "; ".join(state["steps"])
    if os.getenv("OPENAI_API_KEY"):
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(model=os.getenv("LLM_MODEL", "gpt-4.1-nano"))
        msg = llm.invoke(
            f"In 2 sentences, explain this trading signal for a human trader. "
            f"Signal: {state['action']} {state['symbol']}. Facts: {facts}. Not financial advice.",
            config=trace_config(),
        )
        return {"reasoning": msg.content}
    # Fallback without an LLM: just join the steps
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
