"""Where the stock splits come from: Alpaca's corporate actions, else Yahoo; once a day per symbol. What a split does to
a bot's records is tested with the dip bots (test_dip.py)."""

import dataclasses
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app import splits
from app.splits import fetch as real_fetch  # imported before conftest stubs splits.fetch for every test

TODAY = date(2025, 6, 10)
NOW = datetime(2025, 6, 10, 14, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    splits._cache.clear()
    splits._failed_at.clear()
    monkeypatch.setattr(splits, "settings", dataclasses.replace(splits.settings, alpaca_paper_key_id="key",
                                                                alpaca_paper_secret_key="secret"))


def test_alpaca_splits_are_read_page_by_page_as_new_shares_per_old():
    pages = [
        {"corporate_actions": {"forward_splits": [{"symbol": "NVDA", "ex_date": "2024-06-10", "new_rate": 10, "old_rate": 1}]},
         "next_page_token": "p2"},
        {"corporate_actions": {"reverse_splits": [{"symbol": "XYZ", "ex_date": "2024-07-01", "new_rate": 1, "old_rate": 50}],
                               "forward_splits": [{"symbol": "OTHER", "ex_date": "2024-07-01", "new_rate": 2, "old_rate": 1}]},
         "next_page_token": None},
    ]
    asked = []

    def get(url, params, headers, timeout):
        asked.append(params)
        return httpx.Response(200, json=pages[len(asked) - 1])

    found = splits.alpaca_splits(["NVDA", "XYZ"], date(2024, 1, 1), date(2024, 12, 31), get=get)
    assert found == {"NVDA": [(date(2024, 6, 10), 10.0)], "XYZ": [(date(2024, 7, 1), 0.02)]}
    assert asked[0]["types"] == "forward_split,reverse_split" and asked[0]["symbols"] == "NVDA,XYZ"
    assert asked[1]["page_token"] == "p2"


def test_an_alpaca_error_is_an_error():
    with pytest.raises(RuntimeError, match="403"):
        splits.alpaca_splits(["NVDA"], TODAY, TODAY, get=lambda *a, **kw: httpx.Response(403, text="forbidden"))


def test_fetched_once_a_day_from_alpaca_else_yahoo(monkeypatch):
    calls = []
    monkeypatch.setattr(splits, "alpaca_splits", lambda symbols, start, end: calls.append(("alpaca", symbols))
                        or {"NVDA": [(TODAY, 10.0)]})
    assert real_fetch(["NVDA"], TODAY, NOW) == {"NVDA": [(TODAY, 10.0)]}
    assert real_fetch(["NVDA"], TODAY, NOW) == {"NVDA": [(TODAY, 10.0)]}  # from the cache
    assert calls == [("alpaca", ["NVDA"])]

    def down(*a):
        raise RuntimeError("Alpaca down")

    monkeypatch.setattr(splits, "alpaca_splits", down)
    monkeypatch.setattr(splits, "yahoo_splits", lambda symbols, start, end: {"AVGO": [(TODAY, 10.0)]})
    assert real_fetch(["AVGO", "NVDA"], TODAY, NOW) == {"AVGO": [(TODAY, 10.0)], "NVDA": [(TODAY, 10.0)]}
    real_fetch(["NVDA"], TODAY + timedelta(days=1), NOW)  # a new day: asked again
    assert splits._cache["NVDA"][0] == TODAY + timedelta(days=1)


def test_when_nothing_answers_it_waits_before_asking_again(monkeypatch):
    asked = []

    def down(*a):
        asked.append(1)
        raise RuntimeError("down")

    monkeypatch.setattr(splits, "alpaca_splits", down)
    monkeypatch.setattr(splits, "yahoo_splits", down)
    with pytest.raises(RuntimeError, match="split data unavailable"):
        real_fetch(["NVDA"], TODAY, NOW)
    with pytest.raises(RuntimeError, match="retried later"):
        real_fetch(["NVDA"], TODAY, NOW + timedelta(minutes=5))
    assert len(asked) == 2  # Alpaca and Yahoo once, not again within FAIL_RETRY
    monkeypatch.setattr(splits, "alpaca_splits", lambda symbols, start, end: {})
    assert real_fetch(["NVDA"], TODAY, NOW + timedelta(minutes=16)) == {"NVDA": []}
