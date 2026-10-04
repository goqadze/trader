import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from .models import RunConfig, RunSummary, ScanConfig
from .runner import RUNS, start_run
from .scan import SCANS, scan_summary, scan_view, start_scan

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
    return {"status": "ok"}


@app.post("/runs")
async def create_run(cfg: RunConfig):
    """Start a new backtest. Returns the run id the frontend then subscribes to over WebSocket.
    Must be async: start_run() schedules an asyncio task, which needs a running event loop."""
    if cfg.end <= cfg.start:
        raise HTTPException(422, "end must be after start")
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
            if ev["type"] in ("done", "error"):
                await ws.close()  # a finished run: nothing more will come (waiting would hang until the server stops)
                return
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


@app.post("/scans")
async def create_scan(cfg: ScanConfig):
    """Start a scan: every strategy on every symbol, on the practice window, then the passers on the exam window.
    Async for the same reason as POST /runs (it schedules tasks)."""
    scan = start_scan(cfg)
    return {"scan_id": scan.id}


@app.get("/scans")
def list_scans():
    """Every scan since the service started, newest first."""
    return [scan_summary(s) for s in sorted(SCANS.values(), key=lambda s: s.created_at, reverse=True)]


@app.get("/scans/{scan_id}")
def get_scan(scan_id: str):
    """A scan's progress and every combination's practice and exam result (summary numbers and recommendation)."""
    scan = SCANS.get(scan_id)
    if not scan:
        raise HTTPException(404, "scan not found (scans are forgotten when backtest-service restarts)")
    return scan_view(scan)


@app.post("/scans/{scan_id}/stop")
def stop_scan(scan_id: str):
    """Stop a scan: runs in progress end as "stopped", queued ones never start. Finished results stay."""
    scan = SCANS.get(scan_id)
    if not scan:
        raise HTTPException(404, "scan not found")
    scan.stop()
    return scan_view(scan)
