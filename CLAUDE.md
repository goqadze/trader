# CLAUDE.md

Instructions for Claude Code in this repo. Read this first; it saves rediscovering how things work.
Deeper docs: [README.md](README.md) (features, safety design, sign-in, database), [DEPLOY.md](DEPLOY.md) (server
setup), `frontend/public/guides/architecture.html` (architecture, pipelines, gotchas). Machine-specific facts (the
server's address, local scripts) are in `CLAUDE.local.md`, which is git-ignored: **this repo is public**, so never
commit server addresses, account numbers, balances or keys.

## What this is

A trading app run by one person (a developer learning to trade): backtests of daily, intraday and rotation strategies,
plus bots that trade on the built-in paper simulator, Alpaca paper, and **Alpaca live (real money)**. It runs on the
Mac for development and on a Hetzner server (Ubuntu, Docker) for the real bots.

## Working with the user

- Answer in the language of the user's latest message (Georgian or English). Keep answers short.
- No personalized investment advice (not a licensed advisor): give facts, not buy/sell calls.
- **Commit, push and deploy only when asked** (usually "commit push and deploy"). Never deploy on your own.
- Test carefully: a test for every change, a mutation check that the test fails without the change, the UI at desktop
  and phone width (375px), and after a deploy a check of the server. Say plainly what couldn't be tested.
- Report results with numbers (tests passed, bots per broker), short.

## Finding your way (read before searching)

- Each service has a code map in its own `CLAUDE.md` (modules, key functions, recipes, test helpers):
  `trading-service/`, `frontend/`, `decision-service/`, `backtest-service/`. It loads when you open a file there;
  start from it instead of exploring.
- Search by symbol, don't read whole files: `grep -nE "^(async def|def|class) " <file>`; endpoints:
  `grep -nE "^@(app|router)\.(get|post|put|patch|delete)" trading-service/app/*.py`. Read big files (`main.py`,
  `schemas.py`, `dip.py`, `journal.py`, `BotPage.tsx`) by line range.
- Workflows are skills: `deploy` (`scripts/deploy.sh`), `ui-check` (`frontend/vite.harness.config.mjs`).
  Mutation checks: `scripts/mutation-check.sh`. Read-only server checks: `bot-check.sh` (CLAUDE.local.md) and
  `scripts/deploy.sh verify`.
- Other Claude sessions may work in this checkout at the same time: commit only your own files (`git add <files>`,
  never `-A`), and edit shared files such as `.claude/launch.json` instead of overwriting them.

## Safety (real money runs on the server)

- Production is **read-only** unless the user asks for a change: GET the app's API only; no orders, no
  pause/resume/edit of bots, no restarts beyond a requested deploy.
- Never call Alpaca account endpoints (`/v2/account`, `/v2/positions`) yourself, and never place a test order with
  real money. The live order path is covered by tests with a fake broker.
- Don't stop the Mac's `trading-service` (the user wants local services running). Warn if a local `alpaca-paper` bot
  becomes active: the Mac and the server would trade the same Alpaca paper account.
- `trading-service` runs **one uvicorn worker** on purpose: the scheduler lives in the process, two would trade twice.
- Every port binds to `127.0.0.1`. Never change that to `0.0.0.0`: only the dashboard has a sign-in.
- Live orders need `ALLOW_LIVE_TRADING=true` + live keys + a per-bot confirmation. Keep all three gates.

## Project map

| Path | What | Key files |
|---|---|---|
| `trading-service/` (:8002) | Bots, scheduler, brokers, trading DB, sign-in, journal, email alerts | `app/main.py` (API), `scheduler.py` (loop every 30 s), `trader.py` (signal → orders, stops, fills), `dip.py` (dip buyer), `rotation.py` (momentum rotation), `brokers/` (`base.py` interface, `paper.py`, `alpaca.py`), `models.py` (tables), `schemas.py` (validation), `db.py`, `notify.py`, `risk_report.py` (daily risk email) + `macro_dates.json`, `splits.py`, `studies.py` (Test results grid), `journal.py`, `auth.py`, `manage.py` |
| `decision-service/` (:8000) | BUY/SELL/HOLD: 11 daily strategies + news RAG + LLM explanation; intraday plans; bars | `app/strategies.py`, `ta.py`, `agent.py` (LangGraph), `news.py` + `news_store.py` (pgvector), `market_news.py` (market and sector moods), `intraday.py`, `intraday_strategies.py`, `futures.py` |
| `backtest-service/` (:8001) | Replays history through decision-service; scans | `app/runner.py`, `engines/` (`simple`, `dip`, `rotation`, `intraday`), `scan.py`, `verdict.py`, `models.py` |
| `frontend/` (:8080) | React 18 + TypeScript + antd 5, Vite build served by nginx | `src/App.tsx` (routes: `/` = live trading, `/backtest`, `/compare`, `/dip`, `/rotation`, `/scan`, `/journal`, `/alerts`, `/trading/:id`), `src/trading/`, `src/dip/`, `src/rotation/`, `src/studies/`, `src/journal/`, `nginx.conf` (auth_request on every API call), `public/guides/` |
| `watchdog/` | Server only: emails when a service is down, the scheduler is stuck, disk is low, backup is old | `watchdog.py` |
| `scripts/` | `deploy.sh` (deploy + server checks), `mutation-check.sh`, `backup-trading-db.sh` (server cron 22:30 UTC, keeps 7 days), `pull-server-backups.sh` (Mac launchd 09:00) | |
| `.claude/skills/` | Claude Code workflows: `deploy`, `ui-check` | |
| `docker-compose.yml` | Mac: code bind-mounted, backends run with `--reload` | |
| `docker-compose.server.yml` | Server: code baked into images, profiles (`news`, `backtest`, `monitoring`, `pgadmin`, `full`, `public`) | |

Databases: `trading-db` (Postgres, `localhost:5433`, `trading`/`trading`), `news-db` (Postgres + pgvector, `:5434`,
a cache, no backups needed).

## Local development (Mac)

- `docker compose up -d` runs everything. Backends hot-reload: an edit under `*/app/` is live at once in the Mac
  containers, and `trading-service` adds new DB columns to the local DB on reload.
- The frontend at :8080 is a production nginx build (no hot reload): `docker compose up -d --build --no-deps frontend`.
  For hot reload: `cd frontend && npm run dev` (Vite on :5173, proxies the APIs).
- Schema changes have no migration tool: `db.init_db()` adds missing columns (`_add_missing_columns`) and relaxes
  dropped NOT NULL ones (`_relax_removed_columns`) at startup. New columns must be nullable or have a default.
- Library versions are locked: each Python service installs `requirements.txt -c requirements.lock`, the frontend
  uses `npm ci`. After changing a `requirements.txt`, run `make lock`, rebuild, `make test`.

## Tests

```bash
make test             # all four Python suites, inside containers
make test-trading     # trading-service on Postgres (trading_test DB on trading-db)
make test-decision    # decision-service (news tests use news_test on news-db)
make test-backtest
make test-watchdog
cd frontend && npx tsc -p tsconfig.json --noEmit --incremental false   # frontend type check
```

- trading-service tests blank the Alpaca keys, turn the scheduler and live trading off, and refuse a
  `TEST_DATABASE_URL` whose DB name doesn't end in `_test` (`tests/conftest.py`). Without it they use a throwaway SQLite.
- Brokers are faked in tests (`FakeBroker` in `trading-service/tests/conftest.py`). Never hit a real broker in a test.
- Mutation check: `scripts/mutation-check.sh <service> app/<file>.py '<sed that undoes the change>' tests/<file>.py -k <test>`
  (runs on a copy inside the container; exit 0 = caught, 1 = survived).
- Frontend: no unit tests; type check + the `ui-check` skill (desktop and 375px).
- The full runbook (server-image tests on Postgres, read-only API sweep, live-bot data check, a writable DB copy for
  the UI) is in Claude's memory (`testing-runbook`).

## Deploy (only when the user asks)

The server has no CI. Use the `deploy` skill: `scripts/deploy.sh --dry-run <services>`, then
`scripts/deploy.sh --restart --expect "<new bundle text>" <services>`. It backs up the server DB, copies the committed
HEAD (a `git archive`: uncommitted edits stay on the Mac; never `--delete`, which once deleted a server-only dump),
builds, runs the trading-service tests in the new server image, restarts only at a quiet moment, and verifies
(containers, 0 restarts, scheduler, live allowed, watchdog, logs, git clean, bundle).

- Commit and push only the user's change first.
- Restart trading-service after the close (16:00 New York) if possible; in market hours only between checks with no
  open orders, never 10:00-10:30 or 15:30-16:00 (the script enforces this). A restart is safe: orders reconcile and
  last-decision times live in the DB.
- A file deleted or moved in the repo stays on the server (the script lists them): remove it there by hand, a stale
  `.tsx` breaks `tsc -b` in the frontend build.
- The server's `.env` sets `COMPOSE_FILE=docker-compose.server.yml`, so plain `docker compose` there uses the server
  file. A new setting in a `.env.example` goes into the server's `.env` by hand (ask the user for secrets).

## Code conventions

- Comments and docstrings explain *why* in plain English, for a reader new to trading. Module docstrings say what the
  file is for. Match that style and density.
- Commit messages: one plain sentence saying what changes for the user, no type prefix (e.g. "Bot table sorts by
  Return and by the positions' dollar gain"); body optional.
- Validation lives in `trading-service/app/schemas.py`. API errors are 4xx with a readable `detail`.
- Things kept in sync by hand:
  - the strategy list: `decision-service/app/strategies.py` STRATEGIES, `trading-service/app/schemas.py` and
    `backtest-service/app/models.py` `Strategy`, `frontend/src/strategies.ts`;
  - the rotation momentum formula: `trading-service/app/rotation.py` `momentum()` and
    `backtest-service/app/engines/rotation.py`;
  - dip rules: `trading-service/app/dip.py` (bot) and `backtest-service/app/engines/dip.py` (backtest).
- Emails link to `{DASHBOARD_URL}/trading/{bot_id}`: keep that route.
- When behavior changes, update README.md, and the guides in `frontend/public/guides/` if they describe it
  (`make guide-charts` redraws the strategy charts).

## Gotchas

- `frontend/vite.config.js` is tracked and shadows `vite.config.ts` in dev: edit both.
- `npm run typecheck` rewrites the tracked `tsconfig.tsbuildinfo`: use the `npx tsc ... --incremental false` line above.
- Never `git stash` in the main checkout: the hot-reloading Mac containers would run the stashed-away code.
- The Mac compose mounts `./trading-service/app:/code/app`: testing an export elsewhere needs its own `-v .../app:/code/app`.
- On the server, `trading-service` has no code mount: rsync changes nothing live until `docker compose up -d`.
- `docker compose logs` only covers the current container; after a recreate the old lines are gone.
- Grepping logs for `429` also matches client ports: grep `" 429 "` or `Too Many`.
- Alpaca market data is 200 requests/min shared by the paper and live keys: batch reads (one snapshot request per
  scheduler pass, see `brokers/alpaca.py` `shared_round`), never one request per bot per symbol.
- Yahoo's 5- to 30-minute bars go back only 60 days.
- Bots follow NYSE regular hours only (`market.py`). The Mac is on Tbilisi time (UTC+4): the US session is 17:30-00:00
  while the US is on summer time, 18:30-01:00 in winter.
- After a Vite hot reload mid-test, antd confirm modals from the old module can stay open: reload the page first.
