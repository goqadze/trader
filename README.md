# Trading

An AI decision-support tool plus a backtesting app that measures it.

## Services

| Service | Port | What it does |
|---|---|---|
| `decision-service/` | 8000 | FastAPI + LangGraph agent. Turns prices + news (RAG) into a BUY/SELL/HOLD signal with reasoning. |
| `backtest-service/` | 8001 | FastAPI + WebSocket API. Replays history, calls decision-service each step, streams progress. |
| `trading-service/` | 8002 | Live/paper trading bots: scheduler, broker adapters, trading history database. |
| `frontend/` | 8080 | React + TypeScript + Ant Design dashboard (Vite build, served by nginx). Backtests + live trading pages. |
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

## Live trading (paper first)

Once a backtest convinces you, click **Trade this strategy** on its result, or open **Live trading → New bot**.
Each **bot** is one symbol + one parameter set + its own capital, so you can run many stocks, or the
same stock with different parameters side by side, and compare them. Every bot has a page with its
position, equity vs. buy & hold, and its full history: every decision (with the agent's reasoning),
every order, and an audit log of every change.

How a bot runs (`trading-service/app/scheduler.py`):

- **Decides once per trading day, 30 min before the close** (15:30 New York; earlier on half days),
  every `rebalance_days` trading days. That matches the backtest (which fills at the close) and the
  news cutoff. Holidays and early closes come from the NYSE calendar.
- **Same rules as the backtest**: BUY when flat and confidence ≥ minimum, spending `position_pct`
  of the bot's cash; SELL the whole position on a SELL signal, the stop-loss, or the take-profit.
- **Stricter than the backtest where money is at stake**: stops/targets are checked every 5 minutes
  during market hours (not only at the close), and it never trades on a quote older than 20 minutes.

Brokers (choose per bot):

| Broker | What it is | Setup |
|---|---|---|
| `paper` | Built-in simulator: live Yahoo prices, simulated fills with the backtest's slippage/fee model | none |
| `alpaca-paper` | Alpaca paper account: real order routing, fake money | `ALPACA_PAPER_KEY_ID` / `ALPACA_PAPER_SECRET_KEY` in `trading-service/.env` |
| `alpaca-live` | **Real money** | `ALLOW_LIVE_TRADING=true` **and** `ALPACA_LIVE_*` keys, plus a confirmation tick per bot |

Adding Interactive Brokers later = one new class implementing `trading-service/app/brokers/base.py`.

Safety built in:

- **Paper by default.** Live needs an env flag, live keys, and an explicit confirmation per bot; setting the
  flag back to `false` blocks live orders immediately, even for existing bots.
- **Kill switch**: *Halt all* pauses every bot. Paused bots make no new decisions, but stop-loss/take-profit
  keep protecting open positions.
- **Drawdown breaker**: a bot auto-pauses if equity falls `max_drawdown_pct` below its peak.
- **No duplicate or lost orders**: each order is saved *before* it is sent, with a unique client id; after a crash or
  a network drop the scheduler asks the broker what happened instead of re-sending.
- **Reconciliation** (real brokers): if the account's shares don't match the bot's records (e.g. you traded by
  hand), the bot pauses. Only one bot per symbol per real account, so positions can't mix.
- **Restart-safe**: "already decided today" is stored in the database, so a restart never decides twice.
- **History is never deleted**: bots are archived, not removed. Data lives in the `trading-data` Docker volume
  (SQLite; set `DATABASE_URL` for Postgres).
- The dashboard and trading API listen on **localhost only**, because they can place orders.

## Run

```bash
docker compose up --build
```

- Dashboard: http://localhost:8080 (Backtest and Live trading pages)
- Backtest API docs: http://localhost:8001/docs
- Decision API docs: http://localhost:8000/docs
- Trading API docs: http://localhost:8002/docs

Tests for all three backends: `make test`.

### Frontend dev (hot reload)

```bash
cd frontend && npm install && npm run dev
```

Vite serves on http://localhost:5173 and proxies the backtest API/WebSocket (port 8001) and the trading API (port 8002).

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

Browser ──► /api/trading/*  (proxied by nginx)
                ▼
        trading-service :8002 ── scheduler (every 30s) ──► decision-service  (15:30 ET decision)
                │                     │
                │                     └──► broker: paper simulator | Alpaca paper | Alpaca live
                ▼
        SQLite (trading-data volume): bots, decisions, orders, equity, audit log
```
