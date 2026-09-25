"""Shared test setup. news_rag saves news judgments in news-db; tests get an in-memory stand-in instead, so a
fake "bullish" for a real symbol and day can never end up in the real database (and be reused by backtests)."""

import pytest

from app import agent


class FakeJudgments:
    def __init__(self):
        self.saved: dict[tuple, dict] = {}

    def get_judgment(self, symbol, cutoff, version):
        return self.saved.get((symbol, cutoff, version))

    def put_judgment(self, symbol, cutoff, version, result):
        self.saved.setdefault((symbol, cutoff, version), result)


@pytest.fixture(autouse=True)
def judgments(monkeypatch):
    fake = FakeJudgments()
    monkeypatch.setattr(agent, "_judgments", lambda: fake)
    return fake
