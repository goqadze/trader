"""The HTTP / WebSocket API around the runs."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main
from app.models import RunConfig
from app.runner import RUNS, Run


def test_watching_a_finished_run_replays_it_and_closes():
    """A late watcher of a finished run gets its events, then the socket closes instead of waiting forever
    (a handler left waiting kept uvicorn from ever finishing a reload)."""
    run = Run(RunConfig(start=date(2025, 1, 1), end=date(2025, 2, 1)))
    run.events = [{"type": "start", "seq": 0}, {"type": "done", "seq": 1, "result": {}}]
    run.status = "done"
    RUNS[run.id] = run
    try:
        with TestClient(main.app).websocket_connect(f"/ws/{run.id}") as ws:
            assert [ws.receive_json()["type"], ws.receive_json()["type"]] == ["start", "done"]
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        RUNS.pop(run.id, None)
