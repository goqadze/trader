"""Tests for get_signal — must degrade to a safe HOLD, never raise, on any failure."""

import asyncio
from datetime import date

import pytest

from app import decision_client
from app.decision_client import get_signal


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch):
    monkeypatch.setattr(decision_client, "RETRY_DELAYS", (0, 0))


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _Client:
    """Stand-in for httpx.AsyncClient: returns a canned response or raises. `script` gives one outcome
    per call instead (a _Resp or an exception); the last one repeats."""

    def __init__(self, resp=None, exc=None, script=None):
        self._script = script or [exc or resp]
        self.calls = 0

    async def post(self, url, **kwargs):
        self.kwargs = kwargs
        out = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if isinstance(out, Exception):
            raise out
        return out


def test_success_returns_the_json_body():
    client = _Client(resp=_Resp(200, {"action": "BUY", "confidence": 0.9}))
    out = asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1)))
    assert out["action"] == "BUY"
    assert out["confidence"] == 0.9


def test_request_exception_returns_hold():
    client = _Client(exc=RuntimeError("connection refused"))
    out = asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1)))
    assert out["action"] == "HOLD"
    assert out["confidence"] == 0.0
    assert "connection refused" in out["reasoning"]
    assert out["error"] is True  # counted as a failed decision, not a real HOLD


def test_non_200_returns_hold_with_status_in_reason():
    client = _Client(resp=_Resp(422, None, "not enough price history"))
    out = asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1)))
    assert out["action"] == "HOLD"
    assert "422" in out["reasoning"]
    assert out["error"] is True


def test_stop_and_target_are_sent_only_when_set():
    client = _Client(resp=_Resp(200, {"action": "HOLD", "confidence": 0.5}))
    asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1)))
    assert "stop_pct" not in client.kwargs["params"]  # decision-service env defaults apply
    asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1), "breakout", 0.05, 0.1))
    assert client.kwargs["params"]["stop_pct"] == 0.05 and client.kwargs["params"]["target_pct"] == 0.1
    assert client.kwargs["params"]["strategy"] == "breakout"


def test_temporary_failures_are_retried():
    ok = _Resp(200, {"action": "BUY", "confidence": 0.8})
    client = _Client(script=[_Resp(500, None, "RateLimitError"), RuntimeError("reset"), ok])
    out = asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1)))
    assert out["action"] == "BUY" and client.calls == 3


def test_retries_are_limited():
    client = _Client(resp=_Resp(429, None, "slow down"))
    out = asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1)))
    assert out["error"] is True and "429" in out["reasoning"]
    assert client.calls == 1 + len(decision_client.RETRY_DELAYS)


def test_a_422_is_not_retried():
    client = _Client(resp=_Resp(422, None, "not enough price history"))
    asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1)))
    assert client.calls == 1


def test_backtests_ask_for_no_llm_explanation():
    client = _Client(resp=_Resp(200, {"action": "HOLD", "confidence": 0.5}))
    asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1)))
    assert client.kwargs["params"]["explain"] == "false"


def test_technical_only_runs_ask_for_no_news():
    client = _Client(resp=_Resp(200, {"action": "HOLD", "confidence": 0.5}))
    asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1)))
    assert "news" not in client.kwargs["params"]  # news on: decision-service's default
    asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1), news=False))
    assert client.kwargs["params"]["news"] == "false"
