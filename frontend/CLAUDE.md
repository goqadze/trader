# frontend: code map

React 18 + TypeScript + antd 5 (dark theme, `ConfigProvider` in `src/App.tsx`), built by Vite and served by nginx
on :8080. No unit-test framework: check with `npx tsc -p tsconfig.json --noEmit --incremental false` and in the
browser (the `ui-check` skill: `.claude/skills/ui-check/SKILL.md`).

## Find it fast

```bash
grep -n "<Route" src/App.tsx                                          # every page and its path
grep -oE "^  [a-zA-Z]+:" src/trading/api.ts | tr -d ' :' | tr '\n' ' '  # every trading-service call
grep -rn "<text on screen>" src --include='*.tsx'                     # the component that renders a label
```

## Pages (`src/App.tsx` routes)

| Path | Page | Parts |
|---|---|---|
| `/` | `trading/TradingPage.tsx` (bot overview: totals, free cash per Alpaca account (`FreeCash`), broker filter, bot table `columns` incl. the Bot (kind) and Sectors filters and the Rating stars, paged, sorted and filtered by trading-service (`tradingApi.botTable`), activity feed, `MarketStatus`) | `trading/BotForm.tsx` (new single-symbol bot) |
| `/trading/:id` | `trading/BotPage.tsx` (one bot: chart, decisions, orders, events, Edit/Pause/Archive) | `dip/DipParts.tsx` (dip holdings, watchlist, rules, signals), `trading/RotationParts.tsx`, `trading/AddMoney.tsx`, `trading/WithdrawMoney.tsx` |
| `/dip` | `pages/DipPage.tsx` (dip backtest + Test results tab) | `dip/` (`DipBacktestForm`, `DipBotForm`, `RulesFields`, `PresetPicker`, `SymbolChart`, `useDip`), `studies/TestResults.tsx` |
| `/rotation` | `pages/RotationPage.tsx` | `rotation/` (`RotationForm`, `RotationResult`, `UniverseField`, `useRotation`), `trading/RotationBotForm.tsx` |
| `/backtest` | `pages/BacktestPage.tsx` | `components/ConfigForm.tsx`, `EquityChart`, `VerdictView`, `TradeThisButton`; `hooks/useBacktest.ts` |
| `/compare`, `/scan` | `pages/ComparePage.tsx`, `pages/ScanPage.tsx` | `compare/`, `scan/` |
| `/journal`, `/journal/trades/:id` | `pages/JournalPage.tsx`, `journal/TradePage.tsx` | `journal/` (own `api.ts`, `types.ts`) |
| `/alerts` | `pages/EmailAlertsPage.tsx` | |
| `/guide*` | `pages/GuidePage.tsx` → `public/guides/*.html` | |
| `/login`, `/signup`, `/admin/users` | `auth/` | `auth/AuthContext.tsx` (`RequireAuth`) |

## Shared code

- API: `http.ts` `request` (adds errors, 401 → sign-in); `trading/api.ts` `tradingApi.*` (`/api/trading/*`);
  `api.ts` (backtest-service runs/scans); `journal/api.ts`, `auth/api.ts`.
- Types: `trading/types.ts` (`Bot`, `Order`, `TradingStatus`, ...), `dip/types.ts`, `rotation/types.ts`,
  `studies/types.ts`, `types.ts` (backtests). Keep them in step with trading-service `schemas.py`.
- `trading/format.ts`: `usd`, `pct`, `pnlColor`, `nyTime`, `localTime`, `relative`, `botTitle`, `isDip`,
  `isRotation`, `holdsMany`, `slotTimes`, `checkTimes`, `STATUS_COLOR`, `accountLabel`.
- `trading/usePolling.ts` `usePolling(fetcher, ms, deps)` → `{data, error, loading, reload}` (pauses in a hidden tab).
- `trading/sectors.tsx`: `SectorTags` (colored badges), `SectorBadges` (bot page badges + edit dialog).
- `trading/rating.tsx`: `BotRating` (a bot's 0-5 stars, saved on click; bot table and bot page).
- `trading/setup.tsx`: `SetupButton` (a bot's tested setup: tag + popup with its results, the rules that differ, and Save as a saved setup), `FromSetupNote` (new-bot forms).
- `studies/SetupPicker.tsx`: saved setups (`onLoad(config, saved)`; `saved.setup` = the test it was saved from). A dip bot's Edit (`dip/DipParts.tsx` `DipEditForm`) can switch the bot to one: rules, watchlist, tested setup.
- `trading/confirm.tsx` `useConfirm()` (every risky action asks first; shows `AccountTag` LIVE/paper).
- `strategies.ts` (strategy list, kept in sync with the backends), `symbols.ts` (symbol groups, `symbolClash`),
  `periods.ts` (test windows).

## Conventions

- Muted grey text: inline `color: "#8b98b5"`; gains/losses: `pnlColor(v)`; primary `#4f8cff`.
- Phone: `const phone = Grid.useBreakpoint().sm === false` (undefined before the first measure = desktop layout);
  tables get `scroll={{ x: ... }}`; nothing may scroll the page sideways at 375px.
- Per-browser memory (filters, sorts) in `localStorage`, always inside try/catch (see `TradingPage.tsx` `rememberSort`).
- Messages and modals come from `AntApp.useApp()` (themed), not the static antd functions.
- `vite.config.js` is tracked and shadows `vite.config.ts` in dev: edit both. Don't run `npm run typecheck`
  (rewrites the tracked `tsconfig.tsbuildinfo`).
