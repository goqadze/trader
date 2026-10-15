"""The market's mood before the open (/news/market): which headlines the LLM sees, once a day, and what it answers.
No network or LLM: the news source and the LLM are replaced."""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import agent, main, market_news, news
from app.news import MARKET_TZ

DAY = date(2026, 10, 14)  # a Wednesday: the September CPI came out at 8:30


def _ts(day: int, hh: int, mm: int) -> int:
    return int(datetime(2026, 10, day, hh, mm, tzinfo=MARKET_TZ).timestamp())


ARTICLES = [
    {"headline": "CPI runs hotter than expected; futures slide", "ts": _ts(14, 8, 35)},
    {"headline": "CPI runs hotter  than expected; futures slide", "ts": _ts(14, 8, 40)},  # the same story again
    {"headline": "Stocks open lower", "ts": _ts(14, 9, 31)},  # after the cutoff: not seen
    {"headline": "Chip stocks slide as KOSPI plunges", "ts": _ts(13, 10, 0)},
    {"headline": "Tuesday pre-market movers", "ts": _ts(13, 8, 0)},  # before the previous open: not seen
]


class FakeLLM:
    def __init__(self, label="risk_off"):
        self.label, self.prompts = label, []

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt, config=None):
        self.prompts.append(prompt)
        return market_news.MarketMood(label=self.label, rationale="Hot CPI sank the futures.")


@pytest.fixture
def setup(monkeypatch):
    asked, llm = [], FakeLLM()

    def alpaca(symbols, start, end):
        asked.append((symbols, start, end))
        return [{"id": str(i), "summary": "", "source": "alpaca", **a} for i, a in enumerate(ARTICLES)]

    for k in ("OPENAI_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(news, "_alpaca", alpaca)
    monkeypatch.setattr(agent, "_chat", lambda **kw: llm)
    monkeypatch.setattr(market_news, "_memory", {})
    return asked, llm


def test_the_llm_reads_the_market_headlines_from_the_previous_open_to_925(setup):
    asked, llm = setup
    out = market_news.market_mood(DAY, now=datetime(2026, 10, 14, 9, 45, tzinfo=MARKET_TZ))
    assert out["mood"] == "risk_off" and out["reason"] == "Hot CPI sank the futures."
    assert asked == [("SPY,QQQ,DIA,IWM", date(2026, 10, 13), date(2026, 10, 14))]
    assert [h.split("] ", 1)[1] for h in out["headlines"]] == ["CPI runs hotter than expected; futures slide",
                                                              "Chip stocks slide as KOSPI plunges"]
    assert out["headlines"][0].startswith("[1h ago]")
    assert "Stocks open lower" not in llm.prompts[0] and "pre-market movers" not in llm.prompts[0]


def test_a_monday_reads_back_to_fridays_open():
    assert market_news.since(date(2026, 10, 12)).isoformat() == "2026-10-09T09:30:00-04:00"


def test_a_day_is_judged_once_after_the_cutoff_and_saved(setup, judgments):
    _, llm = setup
    after = datetime(2026, 10, 14, 10, 0, tzinfo=MARKET_TZ)
    market_news.market_mood(DAY, now=after)
    market_news._memory.clear()  # a restart: news-db still has it
    again = market_news.market_mood(DAY, now=after)
    assert len(llm.prompts) == 1 and again["mood"] == "risk_off"
    assert ("MARKET", market_news.cutoff(DAY), market_news.MARKET_MOOD_VERSION) in judgments.saved


def test_before_the_cutoff_the_answer_is_not_kept(setup, judgments):
    _, llm = setup
    early = datetime(2026, 10, 14, 9, 0, tzinfo=MARKET_TZ)
    market_news.market_mood(DAY, now=early)
    market_news.market_mood(DAY, now=early)
    assert len(llm.prompts) == 2 and judgments.saved == {}


def test_an_unknown_label_counts_as_neutral(setup):
    _, llm = setup
    llm.label = "cautious"
    assert market_news.market_mood(DAY, now=datetime(2026, 10, 15, tzinfo=timezone.utc))["mood"] == "neutral"


def test_failures_are_unavailable_and_not_saved(setup, judgments, monkeypatch):
    def broken(symbols, start, end):
        raise RuntimeError("Alpaca down")

    monkeypatch.setattr(news, "_alpaca", broken)
    out = market_news.market_mood(DAY, now=datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert out["mood"] == "unavailable" and "Alpaca down" in out["reason"] and judgments.saved == {}


def test_without_keys_it_is_unavailable(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert market_news.market_mood(DAY)["mood"] == "unavailable"


def test_the_endpoint(setup):
    r = TestClient(main.app).get("/news/market", params={"as_of": "2026-10-14"})
    assert r.status_code == 200
    body = r.json()
    assert body["as_of"] == "2026-10-14" and body["mood"] == "risk_off" and len(body["headlines"]) == 2


def test_a_sector_reads_its_own_tickers_news_and_is_saved_apart(setup, judgments):
    asked, llm = setup
    after = datetime(2026, 10, 14, 10, 0, tzinfo=MARKET_TZ)
    out = market_news.sector_mood("Technology", DAY, now=after)
    assert out["mood"] == "risk_off" and asked[0][0] == market_news.SECTOR_TAGS["Technology"]
    assert "any Technology stock today" in llm.prompts[0] and "US Technology stocks as a group" in llm.prompts[0]
    assert ("SECTOR Technology", market_news.cutoff(DAY), market_news.SECTOR_MOOD_VERSION) in judgments.saved
    market_news.market_mood(DAY, now=after)  # the market's own answer is a separate one
    assert len(llm.prompts) == 2 and ("MARKET", market_news.cutoff(DAY), market_news.MARKET_MOOD_VERSION) in judgments.saved
    market_news.sector_mood("Technology", DAY, now=after)
    assert len(llm.prompts) == 2  # kept


def test_a_sector_without_news_tickers_is_unavailable(setup):
    asked, llm = setup
    out = market_news.sector_mood("Trading--Leveraged Equity", DAY)
    assert out["mood"] == "unavailable" and asked == [] and llm.prompts == []


def test_the_sector_endpoint(setup):
    r = TestClient(main.app).get("/news/sector", params={"sector": "Energy", "as_of": "2026-10-14"})
    assert r.status_code == 200
    body = r.json()
    assert (body["sector"], body["mood"], len(body["headlines"])) == ("Energy", "risk_off", 2)


def test_etf_categories_read_their_sectors_news():
    tags = market_news.SECTOR_TAGS
    assert tags["Financial"] == tags["Financial Services"] and tags["Health"] == tags["Healthcare"]
    assert tags["Large Blend"] == market_news.MARKET_SYMBOLS and "TLT" in tags["Long Government"]
