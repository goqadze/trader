"""Optional LLM tracing via Langfuse (self-hosted, open-source).

Activates only when LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are set. When enabled,
every LLM call in the agent is sent to the local Langfuse UI so you can inspect the
exact prompt, response, latency and token cost of the sentiment / decision / explain
steps. If Langfuse is down or the SDK isn't installed, tracing silently no-ops."""

import os

_cache: dict = {}  # memoize the handler (or the fact that there isn't one)


def langfuse_handler():
    """Return a LangChain CallbackHandler for Langfuse, or None if not configured."""
    if "handler" in _cache:
        return _cache["handler"]

    handler = None
    if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
        try:
            from langfuse.callback import CallbackHandler

            handler = CallbackHandler(
                public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
                secret_key=os.environ["LANGFUSE_SECRET_KEY"],
                host=os.getenv("LANGFUSE_HOST", "http://langfuse:3000"),
            )
        except Exception:
            handler = None  # never let tracing setup break the request
    _cache["handler"] = handler
    return handler


def trace_config():
    """Config dict to pass into any LangChain `.invoke(...)`; None when tracing is off."""
    h = langfuse_handler()
    return {"callbacks": [h]} if h else None
