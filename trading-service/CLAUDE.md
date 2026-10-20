# trading-service: code map

Bots, scheduler, brokers, the trading DB, sign-in, journal, emails. FastAPI on :8002 (container port 8000).
Symbol names below are stable; find one with `grep -n "def <name>" app/*.py` instead of reading whole files
(`main.py` ~1,000 lines, `schemas.py` ~1,300).

## Find it fast

```bash
grep -nE "^@(app|router)\.(get|post|put|patch|delete)" app/main.py app/journal.py app/auth.py   # every endpoint + line
grep -nE "^(async def|def|class) " app/<file>.py                                                # outline of one module
grep -n "<column>" app/models.py app/schemas.py ../frontend/src/trading/types.ts                 # a field end to end
```

## Modules

| Module | What | Key symbols |
|---|---|---|
| `main.py` | All bot/dip/rotation/study/email endpoints | `_bot_out` (Bot → `BotOut`, adds equity, live, watchlist, sector badges), `_get_bot`, `_symbol_clash` (who may share a symbol), `_broker_or_400`, `create_bot` / `create_dip_bot` / `create_rotation_bot` (`setup_test_id` → `_setup_snapshot`: the bot's copy of its tested setup, `Bot.setup`), `update_bot`, `update_dip_bot`, `add_bot_capital`, `withdrawal_plan` / `withdraw_bot_capital`, `set_bot_rating` (0-5 stars, a label only), `halt_all`, `accounts` (free cash = account cash − bots' cash, `_funds` 30 s cache), `list_*` |
| `schemas.py` | Every request/response shape + validation (422s) | `BotCreate`, `DipBotCreate`, `DipRules`, `DipBotUpdate`, `RotationBotCreate`, `BotUpdate`, `BotOut`, `StatusOut`, `DipTestIn`, `EmailAlertsIn/Out`, Journal* |
| `models.py` | SQLAlchemy tables | `Bot` (all three kinds; `strategy` = a decision strategy, `"rotation"` or `"dip"`), `Holding` (multi-symbol positions), `WatchItem` (dip watchlist), `Order`, `Decision`, `EquitySnapshot`, `Event` (audit log), `Signal`, `DipPreset`, `DipStudy`/`DipTest`, `Notification`, `Setting`, `User`, `Journal*` |
| `db.py` | Engine + startup schema fixes | `init_db`, `_add_missing_columns` (new columns appear on reload), `_relax_removed_columns`, `get_session` |
| `scheduler.py` | The 30 s loop (docstring = the per-bot steps) | `run_forever` (the live bots' pass, then the others' beside it: `tick(live=True/False)`), `tick` → `round_symbols` + `round_orders_since` (one shared Alpaca read) → `process_bot` → `_process_dip` / `_process_rotation` / single-symbol path; `decision_due`, `next_decision_at`; `state` (`last_tick`, `live_tick` for /health) |
| `trader.py` | Signal → orders, fills, stops, money | `evaluate` (single-symbol decision), `submit_order`, `sync_pending`, `apply_broker_state`, `reconcile` (account vs records → pause), `protect`/`release_stop` (broker stop), `watch` (stop/target check), `mark_to_market`, `bot_equity`, `capital`, `return_pct` (counts money taken out), `add_capital`, `others_hold`, `_refusal` (shared-account guard), `bot_lock`/`account_lock` |
| `dip.py` | Dip buyer rules | `check` (one pass over a watchlist), `signal`, `stand` (7-day high vs last), `buy_fall`, `end_fall`, `due`/`next_check_at`, `watchlist`, `blacklist`/`reenable`, `top_up`, `sell_one`, `yahoo_bars`/`history_start`/`window_bars` |
| `rotation.py` | Momentum rotation | `momentum` (keep in sync with backtest-service `engines/rotation.py`), `rank`, `rebalance`, `step`, `watch`, `reconcile`, `close_all`, `next_rebalance_at` |
| `withdraw.py` | Taking money out of a running bot: cash first, then the same share of each holding | `plan` / `size` (the sales, rounding), `problem` (refusals), `withdraw`, `take_out` (cash, taken out, buy & hold units, drawdown peak), `settle` (what the sales didn't raise stays in; scheduler too), `MIN_LEFT` |
| `brokers/base.py` | Broker interface | `Broker` (`quote`, `submit`, `lookup`, `submit_stop`, `cancel`, `buying_power`, `funds`, `position_qty`, `supports_notional`), `Quote`, `BrokerOrder`, `BrokerError` |
| `brokers/alpaca.py` | Alpaca paper + live | `AlpacaBroker`, `shared_round` (one snapshot per pass; paper: one account read less each buy, one orders read for `lookup(cached=True)`), `scheduled` (a scheduled dip check's paper orders don't wait for fills), `_assets` (paper fractionable, once a day), `_Budget`/`_make_room`/`_rate_wait` (200/min, paper paced, 429 wait), `_snapshot_price` (IEX midpoint) |
| `brokers/paper.py` | Built-in simulator | `PaperBroker`, `yahoo_quote` |
| `brokers/__init__.py` | Which brokers exist / are usable | `catalog`, `get_broker` |
| `market.py` | NYSE calendar | `is_open`, `session_bounds`, `slot_window`, `decision_time`, `next_decision_time`, `month_end_after`, `ny_date` |
| `notify.py` | Email outbox | `enqueue`, `trade`, `from_event`, `dip_signal`, `compose` (HTML), `send_pending`, `run_forever`; links use `dashboard_url()/trading/{id}` |
| `risk_report.py` | Daily pre-open risk email | `build`, `run_once`, `due`, `run_forever`; `macro_dates.json` |
| `studies.py` | Test results grid (server-side paging; each test's sector badges from its symbols, `sector` filter) | `index`, `forget`, `page`, `sort_keys` |
| `sectors.py` | Sector badges per bot (from its symbols, or set by hand: `Bot.sectors`) | `badges` (the rule), `SYMBOLS` (hand-made symbol → sector), `YAHOO` (fallback), `of_bot`, `clean`; endpoints `GET /sectors`, `PUT /bots/{id}/sectors` |
| `journal.py` | Journal API (`/journal`, per signed-in user) | `list_trades`, `create_trade`, `apply`, `stats`, `import_bot_trades` |
| `auth.py` | Sign-in, sessions, admin approval | `current_user`, `admin_user`, `issue_session`, `login`, `signup` |
| others | | `splits.py` (`fetch`, `apply_all`), `shares.py` (`affordable`, `dollars`), `yahoo_cache.py` (`Shared`), `alpaca_prices.py` (`closes`: SIP fallback for Yahoo), `decision_client.py` (`get_signal`, moods), `config.py` (`Settings`), `manage.py` (admin CLI) |

## Recipes

- **New bot field**: column in `models.py` `Bot` (nullable or default) → input in `schemas.py` (`BotCreate`/`DipRules`/...)
  and output in `BotOut` → set it in the create/update endpoint in `main.py` → use it in `trader.py`/`dip.py`/
  `rotation.py` → `frontend/src/trading/types.ts` `Bot` + the form. Dip rules also go in backtest-service
  `engines/dip.py` + `models.py` `DipConfig` (kept in sync by hand).
- **New endpoint**: in `main.py` next to its neighbours; shapes in `schemas.py`; errors as `HTTPException(4xx, "readable detail")`;
  client in `frontend/src/trading/api.ts`.
- **New event / email**: `trader.log_event(...)` writes the audit log; `notify.from_event` decides whether it emails.

## Tests (`tests/`)

- One file per module (`test_trader.py`, `test_dip.py`, `test_api.py`, ...). `conftest.py` gives: `FakeBroker`
  (price, fills, stops, `buying_power`), `make_bot(session, **fields)`, `signal(action, confidence)` (a fake
  decision-service), `at(hour, minute, day)` (NY time), `session`; API tests use `client` (TestClient) in `test_api.py`.
- Run one test: `docker compose run --rm --no-deps -v "$PWD/trading-service:/code" -w /code -e SENTRY_DSN= -e SMTP_HOST= trading-service sh -c "pip install -q -r requirements-dev.txt && pytest -q tests/test_dip.py -k <name>"`
  (throwaway SQLite; `make test-trading` = all, on Postgres).
- Mutation check: `scripts/mutation-check.sh trading-service app/<file>.py '<sed expr>' tests/<file>.py -k <name>`.
