---
name: ui-check
description: Check a frontend change in the built-in browser on real data, without signing in and without any write reaching a bot. Uses the committed Vite harness (fake admin, GET-only proxy) on the Mac's trading-service or on the server through an ssh tunnel, at desktop and 375px. Use after any change under frontend/.
---

# UI check

The harness is `frontend/vite.harness.config.mjs`. It serves a fake signed-in admin and proxies `/api/trading`. It
refuses every non-GET with a 403, so clicks can't pause, edit or trade. Don't test that refusal against the server:
it was checked on the Mac.

## Start

1. Make sure `.claude/launch.json` has these two entries. The file is git-ignored and other sessions add their own
   entries, so **edit it, never overwrite it**:
   ```json
   { "name": "harness", "runtimeExecutable": "frontend/node_modules/.bin/vite",
     "runtimeArgs": ["--config", "frontend/vite.harness.config.mjs"], "port": 5179 },
   { "name": "harness-server", "runtimeExecutable": "frontend/node_modules/.bin/vite",
     "runtimeArgs": ["--config", "frontend/vite.harness.config.mjs", "--mode", "server"], "port": 5178 }
   ```
2. Pick the data source:
   - **Mac data**: `preview_start name=harness`. That's the Mac's trading-service on :8002 (its own few bots) plus
     backtest-service on :8001.
   - **Server data**: open the tunnel first with Bash `run_in_background`:
     `ssh -N -o ExitOnForwardFailure=yes -o BatchMode=yes -L 127.0.0.1:8098:127.0.0.1:8002 <server>` (address in
     CLAUDE.local.md). Then `preview_start name=harness-server`. That's ~137 bots, 3 of them LIVE: read-only, click
     only filters and navigation.
   - **Writes** (forms, buttons): use a throwaway copy of the Mac DB served on :8099. Start the harness with
     `HARNESS_TARGET=http://localhost:8099 HARNESS_WRITES=1`. How to make the copy: the `testing-runbook` memory.
3. It's a dev server, so source edits show up at once (hot reload). Reload the page after an edit if an antd modal
   from the old module stays open.

## Check

- `resize_window` to 1280x800 first: the pane's default frame renders tiny.
- Read with `javascript_tool` / `find` / `get_page_text`. Take a screenshot only to look at the layout. A click by
  `coordinate` needs a screenshot earlier in the same batch, so prefer `ref`s from `find`.
- Screenshots can lag one step behind a click: confirm with `find`/`read_page`, or take a second screenshot.
- Phone: `resize_window preset=mobile`, reload, then check
  `document.documentElement.scrollWidth === innerWidth` (no sideways scroll), then look at a screenshot.
- At the end, `read_network_requests` with `urlPattern=/api/`: only GETs on server data.

## Clean up

`preview_stop` both servers, `TaskStop` the tunnel, `resize_window preset=desktop`.
