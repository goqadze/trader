# Trading

An AI decision-support tool plus a backtesting app that measures it.

## Services

| Service | Port | What it does |
|---|---|---|
| `decision-service/` | 8000 | FastAPI + LangGraph agent. Runs one of 11 strategies on daily prices, tilts it with news (RAG), returns BUY/SELL/HOLD with reasoning. |
| `backtest-service/` | 8001 | FastAPI + WebSocket API. Replays history, calls decision-service each step, streams progress. |
| `trading-service/` | 8002 | Live/paper trading bots: scheduler, broker adapters, trading history database. |
| `frontend/` | 8080 | React + TypeScript + Ant Design dashboard (Vite build, served by nginx). Sign-in, backtests, live trading pages. |
| `watchdog/` | — | Server only: emails you when a service is down (and back), the bots' scheduler is stuck, the disk is nearly full or the nightly backup didn't run. |
| Langfuse | 3000 | LLM tracing UI (self-hosted, free). See every prompt/response/cost. |
| GlitchTip | 8082 | Error tracking (self-hosted, free, Sentry-compatible). |
| pgAdmin | 5050 | Browse the databases' tables and data (trading-db and news-db, already connected). Login `admin@local.dev` + `PGADMIN_PASSWORD` from `.env`. |

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

## Is a result worth trading? (recommendation + Scan)

Every finished backtest ends with a **recommendation** from a checklist (`backtest-service/app/verdict.py`):
must-haves are 10+ closed trades and making money; it also wants wins outweighing losses (profit factor ≥ 1.2),
beating buy & hold on return or on a clearly smoother ride (Sharpe at least 0.2 higher), a worst drop shallower than 20% with the breaker never
tripped, 6+ months tested and no failed decisions. Grades: **Worth paper trading**, **Not yet**, **Not
recommended**. A backtest alone never earns "trade it with real money".

**Scan** (`/scan`, `backtest-service/app/scan.py`) finds symbol + strategy combinations: every strategy on every
symbol over a **practice** window, then only the ones that passed sit an **exam** on later years the practice never
saw. What passes both is a candidate; the summary also shows which strategies held up on several symbols. Scans
run on the server (leaving the page doesn't stop them; restarting backtest-service forgets them), at most
`SCAN_PARALLEL_RUNS` (3) runs at a time so a normal backtest still gets a slot. **Technical only** (news off,
`news=false` on `/signal`) makes a scan fast and free; **Re-check with news** opens a finalist on the Backtest page.

The Scan page also **pools** each strategy's trades across all symbols (twin ETFs counted once): trades, win rate,
profit factor (gross won ÷ gross lost), average trade, how many symbols made money. One symbol can be lucky; a
profit factor near 1.0 over hundreds of trades means the rule has no edge.

## Momentum rotation (portfolio)

**Rotation** (`/rotation`, `backtest-service/app/engines/rotation.py`, `POST /runs/rotation`) backtests a portfolio:
at the start and each month's last close, rank a universe by its 12-1 momentum (the return over the past 12 months,
skipping the latest one; Jegadeesh & Titman), hold the top N in equal parts, sell the rest; with "only hold what
rose" a slot without a rising symbol stays in cash. The benchmark is the whole universe bought in equal parts on
day one. The default universe is the 11 S&P sector ETFs: testing past years on today's largest companies would
pick yesterday's winners in hindsight (survivorship bias), and the page warns when a universe holds stocks.
The universe (and a dip buyer's watchlist) is picked in a popup: the suggested groups of `frontend/src/symbols.ts`
as badges to switch on and off, a filter, a box for any other ticker, and clear buttons per group and for all. Five
groups are the picks of U.S. News articles of October 2026 (high-dividend ETFs, REITs, clean energy, space, money
market ETFs); picked today, they carry the same hindsight warning.
Buys are sized like the bot: whole shares, or with **Fractional shares** (on by default) fractions of one, $1 or
more, so a small **Starting capital** still holds equal parts; a pick that stays is only traded when the change is
worth at least 2% of its slot.
**Paper trade this rotation** turns the tested rules into a rotation bot (see below).

## Dip buyer (watchlist, backtest + bot)

**Dip buyer** (`/dip`, `backtest-service/app/engines/dip.py`, `POST /runs/dip`; bot: `trading-service/app/dip.py`,
`POST /bots/dip`) watches a list of symbols. At every check (the end of each 5/15/30/60-minute bar from the open, or
once a day before the close) it:

1. sells a holding at or under its **stop** (`stop_pct` under the buy) and **blacklists** the symbol until you
   re-enable it; sells at its **target** (back at the price the fall started from, or `rise_pct` above the buy); or,
   if set, after `max_hold_days`;
2. measures each symbol against its **reference**: the highest close of the last `lookback` days (or hours), or the
   close at that window's start (`drop_from`). At least `drop_pct` under it = the **buy zone**;
3. buys the deepest falls first while slots are free (`max_positions`, each 1/N of the equity, whole shares).
   **Fractional shares** (`fractional`, off by default): buys fractions of a share (to a millionth, $1 or more,
   like Alpaca's fractional orders), so a slot smaller than one share's price still buys. The bot asks Alpaca whether
   a symbol can be split and buys whole shares of one it can't; the backtest assumes every symbol can.
   **News on**: decision-service's `GET /news/sentiment` reads the headlines first; bearish news blocks that symbol
   for the rest of the day. **Trend filter**: only symbols whose 50-day average is above the 200-day one.
   **Wait for the turn** (`rebound`, on by default, x1 = `rebound_pct` 1%): a fall into the buy zone isn't bought
   yet. The bot follows it (the reference it fell from, its lowest price since) and buys once the price is x1 above
   that low, bearish turned bullish, still under the reference (a `rebound` signal). Back at the reference first =
   that dip is over. Dip bots created before the option keep buying at once.

The backtest replays every check on intraday bars from decision-service (Alpaca, years of history; 15-minute bars
were added for this) or on Yahoo daily closes, with practice and exam windows and the usual recommendation. It counts
the blacklist too: `reenable_days` stands in for you re-enabling a symbol (0 = never, like a bot you never touch).
**Test the last** n months sets the periods for you: the exam is the last n months up to today, the practice the n
months before (a setup saved that way counts back from the day it is loaded). **Paper trade these rules** creates
the bot with the same rules. The result's **Charts** tab draws each symbol's
price with every trade on it: where the fall started (the window's high), its low where it turned, the buy and the
sale, the bearish stretch shaded red and the bullish one green; click a trade to zoom in with its target and stop
(`GET /runs/{id}/prices`: a run's prices stay in memory, thinned keeping each stretch's high and low). The live bot reads Yahoo's bars for the window (5- to
30-minute bars only go back 60 days, so its window is at most 40 days there) and the broker's quote before each trade.
While it runs you can add and remove symbols (a removed symbol that is held still exits), blacklist or re-enable
them, sell one holding, and change any rule, including the news switch (`PATCH /bots/{id}/dip`).

**Saved setups**: liked a result? **Save** the backtest form under a name (the rules, the watchlist, the capital, the
blacklist and the periods), then pick the name from the list to fill them all back in. The new-bot form loads a
setup's rules, watchlist and capital; a running bot's Edit loads its rules only. Saving under an existing name
replaces it. Kept in trading-db (`GET`/`POST /dip/presets`, `DELETE /dip/presets/{id}`), so every browser sees them.

Every dip into the buy zone, recovery, stop-loss and news veto is saved as a **signal** (`GET /bots/{id}/signals`,
`GET /signals`), whatever the bot did. The bell in the header polls them every 30 s and can show a pop-up, a desktop
notification, play a sound (falling tones = down, rising = up) or read them aloud. A scheduled check that changed
nothing leaves no decision row (a 15-minute bot checks 25 times a day); the watchlist shows the last check instead.
The stop is checked at each check, not held at the broker. Paper accounts only for now.

## Email alerts

Every bot can email you about what matters (account menu → **Email alerts**): **buys and sells** (each filled order,
a sale with its profit or loss), **stop-loss and risk** (stop-loss sales, blacklisted symbols, the drawdown breaker,
HALT ALL), **problems** (errors and failed orders, at most one per bot and kind an hour) and, if you want them, every
**dip signal**. Pick the addresses and the kinds there and send a test email.

The mail server goes in `trading-service/.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`; see
`.env.example`), then `docker compose up -d --force-recreate trading-service`. Gmail: `smtp.gmail.com`, port 587, your
address and an App Password (2-Step Verification on, then myaccount.google.com/apppasswords). Port 587 also works on
a new Hetzner server, which blocks 25 and 465.

How it's built (`trading-service/app/notify.py`): an outbox. The fill, the blacklisting or the error only queues a
`notifications` row in its own transaction; a background loop sends whatever is queued every 15 s as one email (a
stop-loss sale and its blacklisting arrive together), retrying a failed send after 1, 5, 30 and 120 minutes. A slow or
broken mail server never holds up trading, and nothing is lost in a restart. `GET /email-alerts/history` (and the
page) shows what was sent, is waiting, failed or was skipped.

That outbox lives in trading-service, so it can't tell you trading-service is down. On a server the **watchdog**
(`watchdog/watchdog.py`, its own container in `docker-compose.server.yml`) does: every minute it asks each service the
server runs (trading-service's `/health` also checks the database and that the scheduler finished a round in the last
5 minutes), and emails the same addresses when one fails 3 checks in a row, every 6 hours while it stays down, and
when it's back. Also when the disk is under 10% free or the newest backup is over 36 hours old. Tests:
`make test-watchdog`.

## Intraday strategies (backtest only)

Three strategies trade within the day on 5-minute bars (Alpaca, years of history), one setup a day at most, every
position closed by 15:55, long and short:

- **Opening range breakout (`orb`)**: the published 5-minute ORB (Zarattini & Aziz, 2023): trade the first
  candle's direction from 9:35, stop at its other end, target 10× the risk.
- **ICT: sweep → shift → FVG (`ict_sweep_fvg`)**: 9:30-11:00 New York, price sweeps yesterday's or the opening
  range's low, the first close above the last swing high leaves a fair value gap in discount; a limit at the gap's
  middle, the stop under the sweep, the target the nearest liquidity paying ≥ 2× the risk. Shorts mirror it.

- **ICT Power of 3 (`ict_amd`)**: accumulation = the pre-market range (4:00-9:30, Alpaca's extended hours);
  manipulation = in the first hour price runs one side of it and closes back inside within 3 bars; distribution =
  a close through the swing high with a fair value gap anywhere in that move, in its discount half. A limit at the
  gap's middle, the stop beyond the sweep, the target the other side of the pre-market range or yesterday's extreme
  (the nearer paying ≥ 2× the risk). Premium/discount picks the side: longs only when the day opens in the lower half
  of yesterday's range, shorts only in the upper half.

Both ICT strategies have an **entry** setting: `limit` (default) waits for the price to come back to the gap's middle
until 11:00, which it often doesn't; `market` goes in at the next bar's open as soon as the setup completes, so every
setup trades, at a worse price. Same setup, stop and target either way.

They also have a **higher-timeframe trend filter** (`htf`: off, 1h, 4h, 1d): only longs while that timeframe trends up,
only shorts while it trends down, where up = its last finished bar closed above the average of its last 20 closes
(regular-hours bars built from the 5-minute ones). It only removes setups. A setup the account can't take (one
contract or share risks more than `risk_pct` of equity, or a future's margin is more than the account) is skipped,
and the Backtest page says so with the sizes.

They also trade **index futures**: `MNQ` / `NQ`, `MES` / `ES`, `MYM` / `YM`. Alpaca has no futures, so their bars are
rebuilt from the ETF on the same index (QQQ, SPY, DIA) times the previous day's future/ETF closing ratio from Yahoo,
on the future's tick (decision-service `app/futures.py`). The engine trades them as contracts
(backtest-service `app/futures.py`): risk = points to the stop × the multiplier, at most one contract per 10% of its
value in margin, a tick of slippage per market fill and about IBKR's fee per contract ($0.62 a side for the micros,
$2.25 for the minis). What the rebuild can't show: the overnight Globex session.

decision-service `app/intraday_strategies.py` holds the rules and serves each day's order plan
(`GET /intraday/plans`), built bar by bar from finished bars only (no look-ahead). backtest-service's
`IntradayEngine` replays the plans on the 5-minute bars: limit and market fills, stop before target when a bar
touches both, risk-based sizing (`risk_pct`, capped by the cash), slippage on market fills only (0.01% by default
for intraday: with tight stops, costs decide a lot). Live bots can't run them yet.

## Crypto (backtest only)

The symbol pickers suggest 12 coins Alpaca trades against the dollar (BTC, ETH, XRP, SOL, DOGE, ...), written like
Yahoo: `BTC-USD` (Alpaca calls the pair `BTC/USD`). Backtests read Yahoo's daily prices, which include weekends, and
run them with the daily strategies only (the intraday ones trade New York's session). Crypto never closes, so a
decision day decides on the daily close (00:00 UTC) whatever "Check at" says, and "every N days" counts calendar days.
The engine buys fractions of a coin (a $10,000 account can't buy a whole bitcoin) and charges Alpaca's crypto fee,
0.25% a fill for a market order at the lowest volume tier, unless a run sets its own `fee_pct`. Long only: Alpaca
can't short crypto or buy it on margin. Sharpe and volatility annualize over 365 days for prices with weekends.
Live bots refuse crypto symbols for now: they follow the stock market's hours, buy whole shares and hold a stop
order at the broker, and Alpaca's crypto orders take only market, limit and stop-limit.

## Live trading (paper first)

Once a backtest convinces you, click **Trade this strategy** on its result, or open **Live trading → New bot**.
Each **bot** is one symbol + one parameter set + its own capital, so you can run many stocks, or the
same stock with different parameters side by side, and compare them. Every bot has a page with its
position, equity vs. buy & hold, and its full history: every decision (with the agent's reasoning),
every order, and an audit log of every change.

How a bot runs (`trading-service/app/scheduler.py`):

- **Decides every `rebalance_days` trading days, at the time(s) set per bot in "Check at"**:
  - **Before the close** (default): 15:30 New York, earlier on half days.
  - **After the open**: 10:00 New York, skipping the jumpy first 30 minutes.
  - **Both**: 10:00 and 15:30 on each decision day.

  Every check uses the same daily indicators, with that moment's price. News only counts up to the
  decision moment. Holidays and early closes come from the NYSE calendar.
- **Backtests replay the same moments**: the day as it stood at 10:00 or 15:30 (rebuilt from Alpaca's
  30-minute prices, years back, with the same keys as the news; Yahoo's last 60 days without them),
  filled at that moment's price, stop and target from the fill, the same position sizing and the same
  drawdown breaker. Each moment's news is judged once and saved in news-db (a live bot's judgment
  included), so a backtest, a comparison and a rerun see the same news. What can still differ from a
  live bot: its real fill prices, and today's price bar, which comes from Yahoo live.
- **Same rules as the backtest**: BUY when flat and confidence ≥ minimum, spending `position_pct`
  of the bot's cash; SELL the whole position on a SELL signal, the stop-loss, or the take-profit.
- **Stops and targets during the day**: checked every 5 minutes during market hours (the backtest
  matches this with each day's high and low), and it never trades on a quote older than 20 minutes.
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

**Rotation bots** (`trading-service/app/rotation.py`, `POST /bots/rotation`; **New rotation bot**, or **Paper trade
this rotation** on the Rotation page) run the rotation backtest's rules on one bot holding several symbols:

- At its first decision time, then on each month's last trading day at 15:30 New York, it ranks the universe by
  the backtest's momentum formula (Yahoo daily closes, as in the backtest), sells what dropped out of the top N,
  trims or tops up what stays, and buys what came in, in equal parts. Sells go first; the buys wait for their fills.
  A month end missed while the service was down is made up at the next 15:30.
- Whole shares, or with `fractional` (on for new bots, switchable on a running one; NULL on older bots = whole
  shares) fractions of one where Alpaca can split the symbol, $1 or more; a symbol it can't split is bought in whole
  shares. A pick that stays is only traded when the change is worth at least 2% of its slot ($1 at least).
- No stop-loss or take-profit, like the backtest; the drawdown breaker pauses it (no more rebalances, it keeps its
  holdings). Its benchmark is the universe bought in equal parts when it started. Each order records its symbol;
  `rotation` = moving into or out of a symbol, `rebalance` = a trim or top-up.
- Paper accounts only for now (the simulator or Alpaca paper). On Alpaca paper no other bot may trade a symbol of its
  universe, and the account is reconciled symbol by symbol.

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
  To reach a server, see [DEPLOY.md](DEPLOY.md) (SSH tunnel, Tailscale, or HTTPS on your own domain).
- **Comes back by itself**: every container is `restart: unless-stopped`, so a crash, a Docker restart or a
  reboot doesn't leave bots without their decision-service.

### Trading database

| What | How |
|---|---|
| Browse tables and data | pgAdmin at http://localhost:5050 (Resources → Databases): Servers → trading-db → trading → Schemas → public → Tables, then right-click a table → View/Edit Data. Sign in as `admin@local.dev` with `PGADMIN_PASSWORD` from the project's `.env` (copy `.env.example`); the databases then open without a password |
| Connect with any SQL client | `localhost:5433`, database `trading`, user/password `trading` (localhost only) |
| Quick look from the terminal | `docker compose exec trading-db psql -U trading trading` |
| Back up | `make backup-trading-db` → `backups/trading-<timestamp>.sql.gz` (git-ignored; keeps 30 days) |
| Restore | `gunzip -c backups/<file>.sql.gz \| docker compose exec -T trading-db psql -U trading trading` |
| Daily on a server | a cron line runs `scripts/backup-trading-db.sh` (see [DEPLOY.md](DEPLOY.md#9-daily-backups)) |

### News store (RAG)

decision-service keeps fetched headlines and their OpenAI embeddings in `news-db` (Postgres + pgvector,
`localhost:5434`, `news`/`news`, also in pgAdmin). Each search filters to one symbol and the 7 days before the decision
cutoff, then ranks by exact cosine similarity × recency. Articles are embedded once; re-fetching a
window costs no extra OpenAI calls. It's a cache: deleting the `news-db` volume only means news is
re-downloaded on the next decision, so it needs no backups.

pgAdmin can edit and delete rows too, so be careful with anything that writes (it has no undo). Its connections
come from `pgadmin/servers.json`, re-read at every start. Its login password (`PGADMIN_PASSWORD` in `.env`) and the
database passwords in `pgadmin/pgpass` are only read on pgAdmin's first start; after changing them, reset it with
`docker compose rm -sf pgadmin && docker volume rm trading_pgadmin && docker compose up -d pgadmin` (that only
resets pgAdmin's own settings and query history, never your data).

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
step-by-step server setup (Docker, private access over SSH or Tailscale or HTTPS on your own domain, daily backups).
A server runs `docker-compose.server.yml`: built images, capped logs, and only the parts you pick with
`COMPOSE_PROFILES` (the bots alone fit a 2 vCPU / 4 GB server).

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

The dashboard's **Resources → Architecture & Stack** guide (`frontend/public/guides/architecture.html`) walks
through all of this in more depth: the LangGraph pipeline, the LangChain calls, the news RAG, the data sources, the
test harnesses and the gotchas worth knowing. Update it when the architecture changes.

```
Browser ──► frontend :8080 (React + antd, nginx)
                │  every API / WebSocket call: signed in?  ──► trading-service GET /auth/check (auth_request)
                │  POST /runs, /scans  +  WebSocket /ws/{id}   (proxied by nginx)
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
