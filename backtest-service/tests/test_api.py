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


def test_a_dip_run_starts_and_is_listed(monkeypatch):
    """POST /runs/dip validates the config and starts a run in the background (the run itself is replaced here)."""
    from app import runner
    from app.models import DipConfig

    async def nothing(run):
        run.status = "done"

    monkeypatch.setattr(runner, "_execute_dip", nothing)
    client = TestClient(main.app)
    body = {"symbols": ["aapl", "msft"], "start": "2025-01-02", "end": "2025-06-30", "interval": "15m", "drop_pct": 0.05}
    run_id = client.post("/runs/dip", json=body).json()["run_id"]
    try:
        assert isinstance(RUNS[run_id].cfg, DipConfig) and RUNS[run_id].cfg.symbols == ["AAPL", "MSFT"]
        listed = {r["run_id"]: r["symbol"] for r in client.get("/runs").json()}
        assert listed[run_id] == "dip buyer on 2"
        bad = client.post("/runs/dip", json=body | {"interval": "1d", "lookback_unit": "hours"})
        assert bad.status_code == 422
    finally:
        RUNS.pop(run_id, None)


def test_a_dip_runs_prices_for_its_charts():
    import pandas as pd

    from app.models import DipConfig

    run = Run(DipConfig(symbols=["AAPL"], start=date(2024, 1, 3), end=date(2024, 1, 4), interval="15m"))
    idx = pd.date_range("2024-01-02 09:45", "2024-01-05 16:00", freq="15min", tz="America/New_York")
    run.prices = pd.DataFrame({"AAPL": range(len(idx))}, index=idx, dtype=float)
    RUNS[run.id] = run
    try:
        client = TestClient(main.app)
        pts = client.get(f"/runs/{run.id}/prices", params={"symbol": "aapl"}).json()["points"]
        assert pts[0]["t"].startswith("2024-01-03T00:00") or pts[0]["t"].startswith("2024-01-03")  # the run's window ...
        assert pts[-1]["t"].startswith("2024-01-04")  # ... both days whole
        zoom = client.get(f"/runs/{run.id}/prices", params={"symbol": "AAPL", "start": "2024-01-02T10:00:00-05:00",
                                                             "end": "2024-01-02T11:00:00-05:00"}).json()["points"]
        assert [p["t"][11:16] for p in zoom] == ["10:00", "10:15", "10:30", "10:45", "11:00"]
        assert len(client.get(f"/runs/{run.id}/prices", params={"symbol": "AAPL", "start": "2024-01-02", "end": "2024-01-05",
                                                                "points": 50}).json()["points"]) <= 52
        assert client.get(f"/runs/{run.id}/prices", params={"symbol": "MSFT"}).status_code == 404
        assert client.get("/runs/nope/prices", params={"symbol": "AAPL"}).status_code == 404
    finally:
        RUNS.pop(run.id, None)
