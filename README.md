# Trading

An AI decision-support tool plus a backtesting app that measures it.

## Services

| Service | Port | What it does |
|---|---|---|
| `decision-service/` | 8000 | FastAPI + LangGraph agent. Turns prices + news (RAG) into a BUY/SELL/HOLD signal with reasoning. |
| `backtest-service/` | 8001 | FastAPI + WebSocket API. Replays history, calls decision-service each step, streams progress. |
| `frontend/` | 8080 | React + TypeScript + Ant Design dashboard (Vite build, served by nginx). Configure + monitor runs live. |
| Langfuse | 3000 | LLM tracing UI (self-hosted, free). See every prompt/response/cost. |
| GlitchTip | 8082 | Error tracking (self-hosted, free, Sentry-compatible). |

## Monitoring (local & free)

- **Langfuse (LLM tracing)** works out of the box: the compose file bootstraps a project with fixed
  API keys, and `decision-service` sends traces automatically. Open http://localhost:3000
  (login `admin@local.dev` / `langfuse123`).
- **GlitchTip (errors)** needs a one-time setup because the DSN is generated in its UI:
  1. Open http://localhost:8082, register a user, create an organization + project (platform: Python).
  2. Copy the project's **DSN**.
  3. Paste it into `SENTRY_DSN=` in both `decision-service/.env` and `backtest-service/.env`.
  4. `docker compose restart decision-service backtest-service`.

  Errors then appear in GlitchTip automatically (the code already returns the real error text too).

The backtest service has a pluggable engine: `simple` (built-in portfolio simulator, works now) and
`nautilus` (scaffold for NautilusTrader — see `backtest-service/app/engines/nautilus.py`).

## Run

```bash
docker compose up --build
```

- Dashboard: http://localhost:8080
- Backtest API docs: http://localhost:8001/docs
- Decision API docs: http://localhost:8000/docs

### Frontend dev (hot reload)

```bash
cd frontend && npm install && npm run dev
```

Vite serves on http://localhost:5173 and proxies the API/WebSocket to backtest-service on port 8001.

Set your API keys in `decision-service/.env` first (OpenAI required for LLM reasoning + news;
Alpaca / Polygon / Finnhub optional for news RAG). Without news keys the agent uses technicals only.

## Architecture

```
Browser ──► frontend :8080 (React + antd, nginx)
                │  POST /runs  +  WebSocket /ws/{id}   (proxied by nginx)
                ▼
        backtest-service :8001 ──(loops over dates)──► decision-service :8000
                │                                        prices (yfinance) + news RAG (Chroma) + LLM
                │                                        returns { action, confidence, reasoning, steps }
                ▼
        simulate portfolio ──► stream equity / trades / metrics back to the browser live
```
