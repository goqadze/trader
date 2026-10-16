---
name: deploy
description: Deploy committed code to the Hetzner server with scripts/deploy.sh (DB backup, copy HEAD, build, server-image tests, restart at a quiet moment, verify). Use only when the user asks to deploy, usually "commit push and deploy".
---

# Deploy

Real money runs on the server. Deploy only on the user's request, and only what they asked for.

1. **Tests**: the suites of the changed services (`make test-trading`, ...), the frontend type check if `frontend/`
   changed. A failing test stops the deploy.
2. **Commit only the user's change**: `git add <its files>` (never `git add -A`: other sessions may have uncommitted
   work in this checkout), one plain-sentence message (CLAUDE.md), `git push`.
3. **Pick the services** from the changed paths: `frontend/` → `frontend`, `trading-service/` → `trading-service`,
   `decision-service/` → `decision-service`, `backtest-service/` → `backtest-service`, `watchdog/` → `watchdog`.
   Docs, `scripts/`, `CLAUDE.md` only: nothing to build (the copy alone updates them; the backup cron runs the
   checkout's script).
4. **Dry run**: `scripts/deploy.sh --dry-run <services>`. It shows the files that will change (only committed code
   goes: it copies a `git archive` of HEAD) and files the repo deleted since the server's commit (remove those on the
   server by hand: a stale `.tsx` breaks the frontend build).
5. **Deploy**: `scripts/deploy.sh --restart --expect "<a new string in the bundle>" <services>`. It does the backup,
   copy, build, the trading-service tests in the new server image, then the restart and the checks.
   - Restarting trading-service or decision-service waits for a quiet moment and refuses otherwise. Quiet means the
     market is closed, or: not 10:00-10:30 or 15:30-16:00 New York, 5-12 min past a quarter hour, and no open
     orders. If it refuses, run `scripts/deploy.sh restart <services>` in a few minutes; it rebuilds nothing.
     `scripts/deploy.sh moment` asks without doing anything.
   - Frontend, backtest-service and watchdog restart at any time.
   - Without `--restart` it stops after the build: `scripts/deploy.sh restart <services>` later.
6. **Report** with numbers from the output: files copied, tests passed, `verify: all ok` (14 containers, 0
   restarts, scheduler ticking, live trading allowed, 11 watchdog checks, git clean at the commit, bundle string).

## Notes

- The server address is in the git-ignored `.deploy.local` (`SERVER=user@host`, see CLAUDE.local.md). If it's
  missing: `printf 'SERVER=<from CLAUDE.local.md>\n' > .deploy.local`.
- `scripts/deploy.sh verify` alone is a read-only health check of the server.
- A new setting in a `.env.example` must be added to the server's `.env` by hand (ask the user for secrets), then
  the service recreated (`up -d`): env files are read only when a container is created.
- Logs of each step: `$TMPDIR/deploy-<sha>.log` (+ `.build`, `.tests`).
- Never `rsync --delete`, never restart other services, never touch bots (pause, edit, orders) during a deploy.
