import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from .engines import ENGINES
from .models import RunConfig, RunSummary
from .runner import RUNS, start_run

logger = logging.getLogger("backtest-service")
logging.basicConfig(level=logging.INFO)

# Optional error monitoring (Sentry). Activates only if SENTRY_DSN is set.
_dsn = os.getenv("SENTRY_DSN")
if _dsn:
    import sentry_sdk

    sentry_sdk.init(dsn=_dsn, traces_sample_rate=0.1)
    logger.info("Sentry error monitoring enabled")

# API only. The user-facing UI is the separate React app in ../frontend,
# which proxies these routes through nginx.
app = FastAPI(title="Backtest Service")


@app.get("/health")
def health():
    return {"status": "ok", "engines": list(ENGINES)}


@app.post("/runs")
async def create_run(cfg: RunConfig):
    """Start a new backtest. Returns the run id the frontend then subscribes to over WebSocket.
    Must be async: start_run() schedules an asyncio task, which needs a running event loop."""
    if cfg.end <= cfg.start:
        raise HTTPException(422, "end must be after start")
    if cfg.engine not in ENGINES:
        raise HTTPException(422, f"engine must be one of {list(ENGINES)}")
    run = start_run(cfg)
    return {"run_id": run.id}


@app.get("/runs", response_model=list[RunSummary])
def list_runs():
    """List every run (newest first) for the monitoring UI."""
    return [
        RunSummary(run_id=r.id, symbol=r.cfg.symbol, status=r.status, created_at=r.created_at)
        for r in sorted(RUNS.values(), key=lambda r: r.created_at, reverse=True)
    ]


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    """Full detail of one run, including the final result once it's done."""
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return {"run_id": run.id, "config": run.cfg, "status": run.status, "result": run.result}


@app.websocket("/ws/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    """Live event stream for one run. Replays past events, then streams new ones as they happen."""
    await ws.accept()
    run = RUNS.get(run_id)
    if not run:
        await ws.send_json({"type": "error", "message": "run not found"})
        await ws.close()
        return

    q: asyncio.Queue = asyncio.Queue()
    run.subscribers.add(q)
    sent: set[int] = set()
    try:
        # Catch up on anything that already happened before this client connected
        for ev in list(run.events):
            await ws.send_json(ev)
            sent.add(ev["seq"])
        # Then stream live; de-dupe by sequence number in case of overlap
        while True:
            ev = await q.get()
            if ev["seq"] in sent:
                continue
            await ws.send_json(ev)
            if ev["type"] in ("done", "error"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        run.subscribers.discard(q)
