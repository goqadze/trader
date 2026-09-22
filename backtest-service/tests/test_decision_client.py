"""Tests for get_signal — must degrade to a safe HOLD, never raise, on any failure."""

import asyncio
from datetime import date

from app.decision_client import get_signal


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _Client:
    """Stand-in for httpx.AsyncClient: returns a canned response or raises."""

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def post(self, url, **kwargs):
        if self._exc:
            raise self._exc
        return self._resp


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


def test_non_200_returns_hold_with_status_in_reason():
    client = _Client(resp=_Resp(422, None, "not enough price history"))
    out = asyncio.run(get_signal(client, "AAPL", date(2025, 1, 1)))
    assert out["action"] == "HOLD"
    assert "422" in out["reasoning"]
