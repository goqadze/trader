# decision-service: code map

BUY/SELL/HOLD for a symbol and day: a technical strategy decides, the news RAG nudges confidence (±0.15), an LLM
explains. Also intraday bars/plans, market and sector moods. FastAPI on :8000. Find a symbol with
`grep -nE "^(async def|def|class) " app/<file>.py`.

| Module | What | Key symbols |
|---|---|---|
| `main.py` | Endpoints: `GET /health /strategies /bars/intraday /intraday/plans /news/sentiment /news/market /news/sector`, `POST /signal` | `signal`, `Signal` |
| `agent.py` | The LangGraph pipeline behind `POST /signal` | `fetch_data` → `news_rag` → `decide` → `size_position` → `explain`; `State`, `Sentiment` |
| `strategies.py` | The 11 daily strategies | `Strategy` (id, name, style, rules in words, `min_bars`), `STRATEGIES` dict, `catalog()`, `Verdict`, `NewsView` |
| `ta.py` | Indicators | `sma`, `ema`, `rsi`, `macd`, `bollinger`, `atr`, `adx`, swing points, candle patterns, `rel_volume` |
| `tools.py` | Prices from Yahoo | `get_prices`, `yahoo_symbol`, `indicators` |
| `news.py` + `news_store.py` | News ingest + pgvector search (news-db) | `ingest_news`, `search_news`, `decision_cutoff`, `NewsStore` |
| `market_news.py` | Market / sector mood (risk report, dip filters) | `market_mood`, `sector_mood`, `headlines` |
| `intraday.py`, `intraday_strategies.py` | 5/15/30-minute bars; ORB + ICT plans | `bars_between`, `rewind_to`, `plans`, `orb`, `ict`, `ict_amd` |
| `risk.py` | Position sizing | `position_size`, `PositionPlan` |
| `futures.py` | NQ/ES/YM rebuilt from QQQ/SPY/DIA | `rebuild` |

- Adding a strategy: `strategies.py` `STRATEGIES` + the three hand-synced lists (trading-service `schemas.py`,
  backtest-service `models.py` `Strategy`, `frontend/src/strategies.ts`), then `make guide-charts`.
- Tests: `tests/test_<module>.py`; `make test-decision` (news-store tests need news-db's `news_test`).
- LLM: model and keys in `.env` (don't edit secrets); tracing via Langfuse (`observability.py`).
