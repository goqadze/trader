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

On the Alpaca paper account (130+ dip bots on one account, a few hundred buys in one check, and Alpaca allows 200
trading requests a minute per account) a scheduled check sends its orders without waiting for their fills: the
check says "BUY $x of SYM sent; waiting for the fill", and the next scheduler pass, 30 seconds later, books the fills
(holding, price, trade email) from one read of the account's orders. A pass also reads the account's buying power
once and takes each buy off it, and asks whether a symbol can be split once a day; the paper requests slow to the
minute's budget instead of being refused. A buy costs about one request (four before) and a burst of 277 buys should
take about 2 minutes instead of 6.5. "Run now" and the live account still wait for each fill. The live account's bots
have a scheduler pass of their own every 30 seconds, so a long pass of the others never holds their account check,
prices and checks back.

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
setup's rules, watchlist and capital; a running bot's Edit loads its rules, and ticks two more boxes where the saved
setup differs: switch to its watchlist (a held symbol left out still exits at its target or stop) and to the tested setup
it was saved from (`symbols` and `setup_preset_id` in `PATCH /bots/{id}/dip`, one change that is refused whole; a live
bot asks first). Saving under an existing name replaces it. Kept in trading-db (`GET`/`POST /dip/presets`,
`DELETE /dip/presets/{id}`), so every browser sees them. A setup saved from a test (Test results' Save, `test_id`; or
**Save** in a bot's Tested setup popup, `POST /bots/{id}/setup/save`) keeps a copy of that test and its results
(`setup` on the saved setup, the test's code shown in the list), so a bot started from it (`setup_preset_id` in
`POST /bots/dip` and `/bots/rotation`) or switched to it gets the Tested setup tag too ("Bot switched to it" in the popup).
Setups saved from Test results before they kept their test (2026-10-27) get it back at startup (`link_saved_setups`):
the test with the same name and the same rules, from the newest study that existed when it was saved. Switching a bot
to a saved setup without a test (saved from a form) keeps the bot's own tested setup, and the Edit form says so.
In the Test results tab (dip and rotation), a tested setup's rocket button opens the new-bot form filled in with its
rules, watchlist and capital, named after it: pick the broker, check it, create. The bot keeps a copy of that test
(`setup_test_id` in `POST /bots/dip` and `/bots/rotation`; `setup` on the bot): its name, study, rules and results per
window, so it stays when the study is replaced. A **Tested setup** tag (the test's code in the bot table, on the bot's
page) opens them in a popup, with the rules the bot now trades differently from the test ("Stop: 8% → 10%"). The
form's "Don't link", or loading a saved setup without a test into it, starts the bot without the copy. The popup and the bot table's
Score and All columns show the setup's score and All (its windows back to back, as in the grid's All column; worked
out from the windows the bot keeps, so `setup.all` is null where the grid has no All column); both columns sort, with
bots not started from a test last. A red LIVE on the tag of a bot that isn't live means real money trades the same setup:
`setup_live` lists those live bots (archived ones left out), and the popup links to them.

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

**Daily risk report** (tick it under Email alerts; **Send today's risk report** sends one now, and one sent by hand
before 8:45 doesn't replace the day's, which reads the morning's news): on each trading day at 8:45 New York (after the 8:30 CPI and jobs reports, 45 minutes before the open: time to pause a bot), one email saying
where trading looks risky today and how risky (`trading-service/app/risk_report.py`). For every sector the bots watch
or hold: decision-service's LLM reads its headlines published so far (up to 9:25 at most; its ETFs' and biggest
companies', `GET /news/sector`, judged against the market; the email's first line says how far they go) and the
market's (`GET /news/market`, `decision-service/app/market_news.py`); its ETF's moves yesterday and over 5 days; the
symbols that move with it (correlation of daily returns with its ETF over 120 trading days, 0.6 or more); and the CPI,
jobs report and Fed decision days (`trading-service/app/macro_dates.json`, from BLS and the Fed, to `known_through`:
add the next year's dates before then). Each symbol is rated **HIGH** (its sector's news reads risk-off, or a sector
it moves with does), **ELEVATED** (the market's news reads risk-off, or a release is due today) or **NORMAL**, and the
report adds up what the live and the paper bots hold at each level. Information only: the bots don't follow it (as buy
filters on 230 dip setups these signals cost more than they saved). `GET /risk-report` shows it without sending;
`GET /risk-report/live` gives it as data (each sector's level, news and ETF moves, and every live bot's slots: held
ones with their distance to the stop, free ones with the watched symbols they could buy), for a pre-open routine.

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
- The simulator, Alpaca paper, or Alpaca live (real money: `confirm_live`, the form's "real money" tick). On an Alpaca
  account its universe may share symbols with dip and other rotation bots (see "Several bots, one symbol, one
  account" below), and the account is reconciled symbol by symbol. A rebalance order that another bot's working order
  blocks waits for the next 30-second tick (6 rounds of orders at most, then it gives up until the next month end).

Sector badges (`trading-service/app/sectors.py`): each bot shows which parts of the market it trades (Energy, Chips,
Defensive ...) in the bot table's Sectors column (filterable) and on its page. They're worked out from its symbols
(a dip bot's watchlist, a rotation bot's universe, a single bot's symbol): each symbol has a sector (a hand-made list,
else Yahoo's sector), and a sector holding at least a fifth of the symbols is a badge, three at most. 13 energy
stocks give Energy; 8 chip makers and 4 consumer staples give Chips and Defensive; a list spread thinner is
Diversified. The pencil next to them sets them by hand (`PUT /bots/{id}/sectors`, any names, up to 6) or back to
automatic. Only a label: how the bot trades doesn't change. The Test results grids (dip and rotation) give each
tested setup the badges a bot with its symbols would get, in a Sectors column with a filter (`"sector"` in the tests'
`filters`: a setup with any of the chosen sectors stays).

Rating: your own 0 to 5 stars for each bot, to mark favourites. Click a star in the bot table's Rating column or next
to the name on the bot's page; the same star again clears it (`PUT /bots/{id}/rating`, kept in the trading DB, so the
phone and the Mac see the same). The first click on the Rating heading puts the most stars on top; the sort is
remembered in that browser. Only a label: how the bot trades doesn't change, and it leaves no event in the activity.

The bot table comes a page at a time (`GET /bots/table`: 25, 50, 100 or 200 rows, the size remembered in the
browser). trading-service filters it (broker tab, Bot: dip buyer, momentum rotation or single symbol, Sectors, Show archived), sorts it (ID, Rating, Score, All, Return,
vs B&H, Position; none = newest first) and sends only that page's bots in full, with what the page shows of all of them: the
bots per broker, the broker's totals for the cards, the Bot and Sectors choices, the active bots for Halt all and every bot's
kind for the activity feed. vs B&H is Return minus B&H in points: the bots that beat holding what they trade by the
most come first, those without a buy & hold yet last. Return, vs B&H and Position need every bot's equity: all their holdings are read at once, so a
page costs a fraction of `GET /bots` (every bot with its watchlist, ~2.5 MB for 200 bots), which other pages still use.

Free cash (`GET /accounts`): under the totals, each Alpaca account (paper, live; only those with keys) shows the money
no bot holds: the account's cash minus the cash of every bot on it that isn't archived. A paused bot keeps its cash for
when it resumes; an archived one gives it back. E.g. $484.25 of cash with bots holding $24.16 + $140 + $140 leaves
$180.09 for a new bot or Add money. Red below zero: the bots count on more than the account has. A second line shows
when the bots can't spend all of the cash right now: they never buy on margin, so they spend at most the smallest of
the cash, Alpaca's buying power and its non-margin buying power, and after the close the non-margin buying power
leaves out the day's sales until they settle (T+1). After the Oct 16 sales it read $78.56 against $484.25 of cash: no
money gone, it counts again once they settle. Read from Alpaca at most every 30 seconds. The Mac and the server keep
separate bots on the same Alpaca paper account, so each counts only its own.

Alpaca's fees (`fees.py`): Alpaca takes two small fees a trading day from the live account, not per order: a CAT fee
($0.01 on any day with trades, buys too) and a REG fee (SEC and FINRA on the day's sales, rounded up to a cent). Its
orders come back without them, so once an hour trading-service reads the account's fee lines of the last 30 days and
books each one once (table `account_fees`): it comes out of the cash and realized P&L of the bot that caused most of it
that day (the most sales in dollars for REG, the most filled orders for CAT), archived bots too, with a `fee` event on
that bot. So the bots' cash stays what the account has and free cash stops drifting below zero by a cent or two a day.
A fee of a bot archived since then stays in free cash: that bot's cash was already handed back. A fee from a day no bot
traded, or of another kind, is kept with no bot and shows in the activity. Only the live account: the paper account is
shared with the Mac, whose bots would book the same fee again.

Money in and out of a running bot (both on its page; on Alpaca no money moves between accounts, only the bot's share
of the account changes, so the account's free cash goes down or up):

- **Add money** (`POST /bots/{id}/capital`): more cash for the bot. A dip bot's slots grow from its next buy (with
  *top up*, its next check first brings each holding up to a full slot); a rotation bot invests it at its next
  rebalance; a single-symbol bot's next buy uses it. Buy & hold buys its basket with the same money that day.
- **Take money out** (`POST /bots/{id}/withdraw`, `trading-service/app/withdraw.py`): from the bot's cash first,
  realized profit included; only what the cash doesn't cover comes from selling the same share of each holding at
  market (so in regular hours only). Fractions of a share where the bot and the broker trade them; elsewhere whole
  shares, each holding's share rounded down and the rest made up one share at a time from the holdings nearest to
  their next share (the extra a whole share raises stays in its cash). The sales are sized on fresh quotes less the
  bot's slippage and fee estimates; if they fill lower, or one doesn't fill, that much less comes out and the cash
  never stays under zero (a sale still working is settled by the scheduler once it finishes). A bot keeps $100: to
  take everything out, close its positions and archive it. Refused (with the reason) while one of its orders is
  working, during a rebalance, or on an archived bot; a real-money bot asks for a confirmation tick. The dialog shows
  the plan first (`GET /bots/{id}/withdraw?amount=`, nothing sent). E.g. a bot of $412 with $40 of cash: taking $150
  out takes the $40 and sells about 30% of each holding for the other $110.
- **How it's measured**: "put in" is the starting capital plus money added; money taken out doesn't lower it. The
  return is (equity + money taken out) / put in − 1, so taking money out never moves it, and taking profit out never
  turns it into a loss. Buy & hold gets the same money in and out on the same days (it sells its basket for a
  withdrawal) and is measured the same way. Example: $400 put in, worth $450 (+12.5%); take $100 out: worth $350,
  still +12.5%. The drawdown breaker's peak shrinks in proportion, so a withdrawal neither trips nor resets it. After
  a withdrawal a dip bot's slots are smaller (its holdings keep their stop, target and days held; one sold whole isn't
  bought again before tomorrow, and a waiting top-up is dropped), a rotation bot's next rebalance sizes its picks on
  the smaller equity, and a single-symbol bot's stop order at the broker is placed again for the shares left. Each
  withdrawal is in the bot's events and emailed with the trades (category *Buys and sells*); its sales show as
  `withdrawal` orders.

Safety built in:

- **Paper by default.** Live needs an env flag, live keys, and an explicit confirmation per bot; setting the
  flag back to `false` blocks live orders immediately, even for existing bots.
- **Kill switch**: *Halt all* pauses every bot. Paused bots make no new decisions, but stop-loss/take-profit
  keep protecting open positions.
- **Drawdown breaker**: a bot auto-pauses if equity falls `max_drawdown_pct` below its peak.
- **No duplicate or lost orders**: each order is saved *before* it is sent, with a unique client id; after a crash or
  a network drop the scheduler asks the broker what happened instead of re-sending.
- **Your own money only, never a loan**: every Alpaca account is a margin account, and from $2,000 of equity its
  buying power includes borrowed money. The bots' buys stop at the smallest of the account's cash, its non-margin
  buying power and its buying power, so they never trade on margin.
- **Stock splits**: once a day each holding follows its splits (Alpaca's corporate actions, else Yahoo): shares x
  ratio, its buy / stop / target prices / ratio, so a 10-for-1 split never looks like a 90% fall to the stop-loss. A
  symbol isn't bought on its split day. If the split data misses one, a sale whose share count and price both moved by
  the same ratio is held back and the bot pauses until the data has it.
- **Reconciliation** (real brokers): if the account's shares don't match the bots' records (e.g. you traded by
  hand), the bot pauses.
- **Several bots, one symbol, one account**: dip and rotation bots may trade the same symbol on one real account.
  Each books only its own fills and sells only its own shares; the account check adds up all their records; a sale
  bigger than what the account holds beyond the other bots' shares isn't sent (it would sell theirs, or sell short)
  and pauses the bot; orders on one account go out one at a time, and none while another bot's order on the other
  side of that symbol is still working (Alpaca's wash-trade protection would reject it): it's retried at the next check.
  An order sent but not booked yet is asked about first: once Alpaca has filled it, the other side goes out.
  Single-symbol bots keep their symbols to themselves: a single-symbol bot's stop-loss rests at Alpaca as an open sell
  order, Alpaca rejects buys of that symbol meanwhile, and its account check counts only its own shares.
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
| Back up | `make backup-trading-db` → `backups/trading-<timestamp>.sql.gz` (git-ignored; keeps 7 days) |
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

Library versions are locked, so the Mac and the server build the same thing: each Python service installs exactly
the versions in its `requirements.lock`, the frontend exactly those in `package-lock.json` (`npm ci`). After changing a
`requirements.txt` (or to take updates), run `make lock`, rebuild, and run `make test`.

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
