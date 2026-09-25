# Trading

An AI decision-support tool plus a backtesting app that measures it.

## Services

| Service | Port | What it does |
|---|---|---|
| `decision-service/` | 8000 | FastAPI + LangGraph agent. Runs one of 11 strategies on daily prices, tilts it with news (RAG), returns BUY/SELL/HOLD with reasoning. |
| `backtest-service/` | 8001 | FastAPI + WebSocket API. Replays history, calls decision-service each step, streams progress. |
| `trading-service/` | 8002 | Live/paper trading bots: scheduler, broker adapters, trading history database. |
| `frontend/` | 8080 | React + TypeScript + Ant Design dashboard (Vite build, served by nginx). Sign-in, backtests, live trading pages. |
| Langfuse | 3000 | LLM tracing UI (self-hosted, free). See every prompt/response/cost. |
| GlitchTip | 8082 | Error tracking (self-hosted, free, Sentry-compatible). |
| pgAdmin | 5050 | Browse the databases' tables and data (trading-db and news-db, already connected). |

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

Backtests run on a built-in portfolio simulator (`backtest-service/app/engines/simple.py`). The **strategy**
picker chooses how BUY/SELL is decided; the simulator only replays those decisions with fills, fees and stops.

## Strategies

Each backtest and each bot picks one strategy (`decision-service/app/strategies.py`, listed at
`GET http://localhost:8000/strategies`). The LLM never decides a trade: it reads the news and explains the result.
The dashboard's **Resources → Trading Strategies** guide explains each one with a chart, its exact rules, and how it
did on 2024–25 prices; **Resources → Trading Bot Lifecycle** covers what a bot does with the signal.
After changing a strategy, `make guide-charts` redraws the guide's charts from the real code (then rebuild the frontend).

| Strategy | Style | BUY | SELL (exit) |
|---|---|---|---|
| `sma_rsi` (default) | trend | SMA20 > SMA50 and RSI14 < 70 | SMA20 ≤ SMA50 and RSI14 > 30 |
| `trend_following` | trend | golden cross (SMA50 > SMA200), price > SMA50, ADX ≥ 20 | death cross, or price < SMA200 with sellers in control |
| `momentum` | momentum | 3-month return > 0, MACD rising above signal, RSI 50–70 | MACD < signal with RSI < 50 |
| `breakout` | breakout | close > 20-day high on ≥ 1.5× volume | close < 10-day low (Turtle exit) |
| `mean_reversion` | mean reversion | dip below the lower Bollinger Band with RSI < 30, then an up close back inside | back at the middle band |
| `range_trading` | mean reversion | ADX < 20, bounce off a twice-tested support | near resistance, or support breaks |
| `ma_pullback` | trend (swing) | uptrend, pullback to EMA20 rejected upward | EMA20 < EMA50 or close < EMA50 |
| `reversal` | mean reversion | bullish RSI divergence + up candle | bearish RSI divergence + down candle |
| `gap_and_go` | event | gap up ≥ 2% that holds, ≥ 1.5× volume | gap down that never recovers |
| `news_catalyst` | event | bullish news and the price agrees (fresh catalyst / volume raise confidence) | bearish news and the price agrees |
| `fibonacci` | trend | bounce in the 50–61.8% golden zone after an 8%+ move | close below the 78.6% level |

Chosen from four strategy round-ups ([Evest](https://www.evest.com/en/trading-blog/trading-strategies/),
[DataDrivenInvestor](https://datadriveninvestor.com/articles/top-10-trading-strategies-that-everyone-should-know/),
[XBTFX](https://xbtfx.com/blog/top-15-most-popular-trading-strategies/),
[DBS](https://www.dbs.bank.in/in/treasures/articles/learning-centre/trading-strategies)) for what this app can run:
daily bars, one symbol, long-only. Intraday methods (scalping, ICT/SMC, sessions), two-symbol or short-selling ones
(pairs, arbitrage) and hand-drawn trendlines were left out.

**Confidence** is on one scale for all: a textbook setup is 0.60 (the default minimum, so it trades), extra
confirmations add up to +0.25, news that agrees/disagrees adds/subtracts 0.15, HOLD is 0.40. **News RAG** tilts every
strategy and also flags *fresh catalysts* (earnings, guidance, up/downgrades, deals, FDA, recalls, lawsuits in the
last 48h), which `gap_and_go` and `news_catalyst` use directly.

Each strategy suggests a starting cadence and stop/target (the **Apply** link under the picker). Setup strategies
(breakout, mean reversion, gaps, news...) fire on a specific day, so they suggest checking **every trading day**; a
weekly check would miss most setups.

## Live trading (paper first)

Once a backtest convinces you, click **Trade this strategy** on its result, or open **Live trading → New bot**.
Each **bot** is one symbol + one parameter set + its own capital, so you can run many stocks, or the
same stock with different parameters side by side, and compare them. Every bot has a page with its
position, equity vs. buy & hold, and its full history: every decision (with the agent's reasoning),
every order, and an audit log of every change.

How a bot runs (`trading-service/app/scheduler.py`):

- **Decides every `rebalance_days` trading days, at the time(s) set per bot in "Check at"**:
  - **Before the close** (default): 15:30 New York, earlier on half days. That matches the backtest
    (which fills at the close) and the news cutoff.
  - **After the open**: 10:00 New York, skipping the jumpy first 30 minutes.
  - **Both**: 10:00 and 15:30 on each decision day.

  Every check uses the same daily indicators, with that moment's price. News only counts up to the
  decision moment. "Before the close" is the only one the backtest tests. Holidays and early closes
  come from the NYSE calendar.
- **Same rules as the backtest**: BUY when flat and confidence ≥ minimum, spending `position_pct`
  of the bot's cash; SELL the whole position on a SELL signal, the stop-loss, or the take-profit.
- **Stricter than the backtest where money is at stake**: stops/targets are checked every 5 minutes
  during market hours (not only at the close), and it never trades on a quote older than 20 minutes.
- **Alpaca bots keep their stop-loss at the broker**: right after a buy fills, a good-till-canceled stop
  order for all the shares rests at Alpaca. It fires even while this app, or the machine it runs on, is
  off. Every other sell cancels it first and waits for the broker to confirm, so a position can never be
  sold twice. Paper-simulator bots rely on the 5-minute check. The take-profit is always checked by the app.

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
- **Restart-safe**: the time of the last decision is stored in the database, so a restart never repeats a
  check (and never skips the afternoon one because the morning one ran).
- **History is never deleted**: bots are archived, not removed. Data lives in its own Postgres container
  (`trading-db`, Docker volume `trading-db`), separate from Langfuse's database.
- **Every port listens on localhost only**: only the dashboard has a sign-in (see below), and it can place orders.
  To reach a server, see [DEPLOY.md](DEPLOY.md) (SSH tunnel or Tailscale).
- **Comes back by itself**: every container is `restart: unless-stopped`, so a crash, a Docker restart or a
  reboot doesn't leave bots without their decision-service.

### Trading database

| What | How |
|---|---|
| Browse tables and data | pgAdmin at http://localhost:5050 (Resources → Databases): Servers → trading-db → trading → Schemas → public → Tables, then right-click a table → View/Edit Data. No login, no password prompt |
| Connect with any SQL client | `localhost:5433`, database `trading`, user/password `trading` (localhost only) |
| Quick look from the terminal | `docker compose exec trading-db psql -U trading trading` |
| Back up | `make backup-trading-db` → `backups/trading-<timestamp>.sql.gz` (git-ignored; keeps 30 days) |
| Restore | `gunzip -c backups/<file>.sql.gz \| docker compose exec -T trading-db psql -U trading trading` |
| Daily on a server | a cron line runs `scripts/backup-trading-db.sh` (see [DEPLOY.md](DEPLOY.md#8-daily-backups)) |

### News store (RAG)

decision-service keeps fetched headlines and their OpenAI embeddings in `news-db` (Postgres + pgvector,
`localhost:5434`, `news`/`news`, also in pgAdmin). Each search filters to one symbol and the 7 days before the decision
cutoff, then ranks by exact cosine similarity × recency. Articles are embedded once; re-fetching a
window costs no extra OpenAI calls. It's a cache: deleting the `news-db` volume only means news is
re-downloaded on the next decision, so it needs no backups.

pgAdmin can edit and delete rows too, so be careful with anything that writes (it has no undo). Its connections
come from `pgadmin/servers.json`, re-read at every start. The passwords in `pgadmin/pgpass` are only copied in on
pgAdmin's first start; after changing them, recreate it with `docker compose rm -sf pgadmin && docker volume rm
trading_pgadmin && docker compose up -d pgadmin` (that only resets pgAdmin's own settings, never your data).

`docker compose down` keeps the data; `docker compose down -v` **deletes it** (volumes included).
Tests use a separate `trading_test` database and refuse to run against any database not named `*_test`.

## Sign-in and users

The dashboard needs a sign-in. Anyone can **sign up**, but the account can't sign in until an admin approves it on
the **Users** page (in the header for admins; a badge counts sign-ups waiting). Admins can also reject, disable and
re-enable, promote, or delete accounts there. An approved user can use the whole dashboard; admins also manage users
and see the Monitoring and API docs links.

- **The first admin** comes from the command line (it asks for the password):
  `docker compose exec trading-service python -m app.manage create-admin <username>`.
  Forgot a password? `docker compose exec trading-service python -m app.manage set-password <username>`.
- **Staying signed in**: signing in stores a JWT, signed with `JWT_SECRET` from `trading-service/.env`, in an
  HttpOnly cookie that page scripts can't read. With **Keep me signed in** it lasts `SESSION_DAYS` (30) and every
  visit renews it; without, it ends when the browser closes. Disabling an account or changing a password signs it
  out everywhere at once.
- **How it's enforced**: before passing on any API or WebSocket call, nginx asks trading-service `GET /auth/check`
  (`auth_request` in `frontend/nginx.conf`), so the backtest API is covered too. The service ports (8000–8002)
  skip nginx and stay localhost-only.
- 5 wrong passwords for one username from one address lock that pair out for 15 minutes.
- Served over HTTPS? Set `COOKIE_SECURE=true` in `trading-service/.env`.

Code: `trading-service/app/auth.py` (API, tokens), `app/manage.py` (command line), `frontend/src/auth/` (pages).

## Run

```bash
docker compose up --build
```

On a fresh database, create the admin (see [Sign-in and users](#sign-in-and-users)) and sign in at http://localhost:8080.

**Laptop or server?** Either one works. Bots only decide while the stack is running, so for real money use an
always-on server. [DEPLOY.md](DEPLOY.md) covers both: keeping a Mac awake during market hours, and a
step-by-step server setup (Docker, private access over SSH or Tailscale, daily backups).

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
                │  every API / WebSocket call: signed in?  ──► trading-service GET /auth/check (auth_request)
                │  POST /runs  +  WebSocket /ws/{id}   (proxied by nginx)
                ▼
        backtest-service :8001 ──(loops over dates)──► decision-service :8000
                │                                        prices (yfinance) + news RAG (pgvector) + LLM
                │                                        returns { action, confidence, reasoning, steps }
                ▼
        simulate portfolio ──► stream equity / trades / metrics back to the browser live

Browser ──► /api/trading/*  (proxied by nginx)
                ▼
        trading-service :8002 ── scheduler (every 30s) ──► decision-service  (10:00 and/or 15:30 ET)
                │                     │
                │                     └──► broker: paper simulator | Alpaca paper | Alpaca live
                │                                  (Alpaca also holds each position's stop-loss order)
                ▼
        Postgres (trading-db): bots, decisions, orders, equity, audit log
```
