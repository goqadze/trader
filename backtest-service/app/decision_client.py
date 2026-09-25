import os
from datetime import date

import httpx

# Inside docker-compose the services reach each other by name on the shared network.
# Override with DECISION_SERVICE_URL when running outside compose.
BASE = os.getenv("DECISION_SERVICE_URL", "http://decision-service:8000")


async def get_signal(client: httpx.AsyncClient, symbol: str, as_of: date, strategy: str = "sma_rsi",
                     stop_pct: float | None = None, target_pct: float | None = None) -> dict:
    """Ask decision-service for a signal on a given date. Never raises: on any failure we
    return a safe HOLD so one bad day can't crash the whole backtest. The failure reason is
    put in `reasoning` so it shows up in the activity log, and `error` marks it so the run can count failures.
    stop_pct / target_pct are only sent when set, so decision-service's env defaults apply otherwise."""
    params = {"symbol": symbol, "as_of": as_of.isoformat(), "strategy": strategy}
    if stop_pct is not None:
        params["stop_pct"] = stop_pct
    if target_pct is not None:
        params["target_pct"] = target_pct
    try:
        r = await client.post(f"{BASE}/signal", params=params, timeout=60)
    except Exception as e:
        return {"action": "HOLD", "confidence": 0.0, "reasoning": f"request failed: {e}", "steps": [], "error": True}
    if r.status_code != 200:
        # 422 usually means "not enough price history yet" for early dates; 500 shows the real error text
        return {"action": "HOLD", "confidence": 0.0, "reasoning": f"decision-service {r.status_code}: {r.text[:200]}", "steps": [],
                "error": True}
    return r.json()
