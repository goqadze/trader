# backtest-service: code map

Replays history: daily strategies (through decision-service), dip buyer, momentum rotation, intraday plans, and
scans. FastAPI on :8001. Find a symbol with `grep -nE "^(async def|def|class) " app/<file>.py`.

| Module | What | Key symbols |
|---|---|---|
| `main.py` | `POST /runs /runs/rotation /runs/dip`, `GET /runs /runs/{id} /runs/{id}/prices`, `WS /ws/{id}`, `POST/GET /scans`, `POST /scans/{id}/stop` | `create_run`, `create_dip_run`, `create_rotation_run`, `create_scan` |
| `models.py` | Request shapes + validation | `RunConfig`, `RunSettings`, `DipConfig`, `RotationConfig`, `ScanConfig`, `Period`, `RunSummary`, `Strategy` list |
| `runner.py` | Runs in the background, streams progress | `Run`, `start_run`, `start_dip_run`, `start_rotation_run` |
| `engines/simple.py` | Daily strategy portfolio (fees, slippage, stops on the day's range) | `SimplePortfolioEngine` |
| `engines/dip.py` | Dip buyer replay (same rules as trading-service `dip.py`, synced by hand) | `DipEngine`, `references`, `daily_moves`, `uptrend` |
| `engines/rotation.py` | Momentum rotation (same `momentum()` as trading-service `rotation.py`) | `RotationEngine`, `momentum` |
| `engines/intraday.py` | Intraday plans bar by bar | `IntradayEngine` |
| `scan.py`, `verdict.py` | Practice/exam scans; plain-words grade of a result | `start_scan`, `scan_summary`, `judge` |
| `decision_client.py` | Calls decision-service `/signal` | `get_signal` |

- Tests: `tests/test_<engine>_engine.py`, `test_runner.py`, `test_scan.py`, ...; `make test-backtest`.
- A rule change in a bot needs the same change here (dip, rotation), or the backtest stops matching the bot.
