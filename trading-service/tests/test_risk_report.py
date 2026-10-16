"""The daily risk report: the levels (a sector's news, what moves with it, the macro calendar), the email's text, when it
is sent, and its API. No network: the news, the prices and the sectors are stand-ins."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from conftest import make_bot
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, notify, risk_report
from app.config import settings
from app.models import DIP, Holding, Notification, SymbolSector, WatchItem

TODAY = date(2025, 6, 2)  # a Monday
NOW = datetime(2025, 6, 2, 13, 30, tzinfo=timezone.utc)  # 9:30 New York
SECTORS = {"MRVL": "Technology", "VRT": "Industrials", "XOM": "Energy", "AAPL": "Technology"}


def closes(symbols, start, end=TODAY):
    """130 trading days before `end`: VRT moves with XLK, MRVL with SMH, XOM with XLE; the other ETFs on their own."""
    rng = np.random.default_rng(7)
    idx = pd.bdate_range(end=end - timedelta(days=1), periods=130)
    walk = {t: rng.normal(0, 0.01, len(idx)) for t in ("XLK", "SMH", "XLE", "OTHER")}
    follow = {"VRT": "XLK", "MRVL": "SMH", "XOM": "XLE", "AAPL": "XLK", "JPM": "OTHER", "XLF": "OTHER", "SPY": "OTHER"}
    out = {}
    for s in symbols:
        r = walk[follow[s]] + rng.normal(0, 0.002, len(idx)) if s in follow else walk.get(s, rng.normal(0, 0.01, len(idx)))
        out[s] = 100 * np.cumprod(1 + r)
    out["SMH"] = out.get("SMH", 100 * np.cumprod(1 + walk["SMH"]))
    return pd.DataFrame(out, index=idx).reindex(columns=symbols)


def tech_off(sector, day):
    return {"mood": "risk_off", "reason": "Chips sold off in Seoul."} if sector == "Technology" else {"mood": "neutral", "reason": "calm"}


def calm(day):
    return {"mood": "neutral", "reason": "Futures mixed."}


@pytest.fixture
def bots(session):
    live = make_bot(session, name="live dip", symbol="DIP", strategy=DIP, broker="alpaca-live", cash=50.0, max_positions=4)
    for s in ("MRVL", "VRT", "XOM"):
        session.add(WatchItem(bot_id=live.id, symbol=s, status="watching"))
    session.add(Holding(bot_id=live.id, symbol="MRVL", shares=1.0, cost_basis=250.0, last_price=240.0, entry_price=250.0,
                        stop_price=230.0))
    session.add(Holding(bot_id=live.id, symbol="VRT", shares=2.0, cost_basis=400.0, last_price=200.0, entry_price=200.0))
    paper = make_bot(session, name="paper one", symbol="AAPL", shares=10, last_price=100.0)
    session.commit()
    return live, paper


def build(session, today=TODAY, mood=calm, sector_mood=tech_off, lookup=SECTORS.get):
    return risk_report.build(session, today, NOW, mood_fn=mood, sector_mood_fn=sector_mood,
                             closes_fn=lambda symbols, start: closes(symbols, start, today), sector_lookup=lookup)


def test_a_risk_off_sector_and_what_moves_with_it_are_high(session, bots):
    live, _ = bots
    r = build(session)
    sym = r["symbols"]
    assert sym["MRVL"]["level"] == "HIGH" and sym["MRVL"]["why"] == ["Technology news reads risk-off"]
    assert sym["VRT"]["level"] == "HIGH" and sym["VRT"]["why"][0].startswith("moves with Technology (0.9")  # an Industrial
    assert sym["XOM"]["level"] == "NORMAL" and r["sectors"]["Industrials"]["level"] == "NORMAL"
    assert r["overall"] == "HIGH"  # a live bot holds HIGH-risk symbols
    text = r["text"]
    assert "OVERALL: HIGH" in text and "Market news: NEUTRAL — Futures mixed." in text
    assert "(news up to 09:25 New York)" in text  # built at 9:30: the news stops at decision-service's 9:25
    assert "Macro: nothing due today · this week: Fri Jun 6 jobs report" in text
    assert "HIGH      Technology — news risk-off" in text and "Chips sold off in Seoul." in text
    assert "Moving with it (correlation 0.6+): VRT 0.9" in text
    assert f"#{live.id} live dip: MRVL $240 HIGH, VRT $400 HIGH · cash $50.00" in text
    assert "Held by live bots: $640 HIGH, $0 elevated, $0 normal" in text
    assert "PAPER bots: $1,000 HIGH" in text  # AAPL, Technology
    assert "the bots don't follow it" in text
    assert r["subject"].startswith("Risk report Jun 2: HIGH · market neutral · 1 of ")


def test_a_risk_off_market_and_a_macro_day_raise_the_rest(session, bots):
    r = build(session, today=date(2025, 6, 11), mood=lambda d: {"mood": "risk_off", "reason": "Hot CPI."},
              sector_mood=lambda s, d: {"mood": "neutral"})  # May's CPI came out on June 11, 2025
    assert r["symbols"]["XOM"]["level"] == "ELEVATED"
    assert r["symbols"]["XOM"]["why"] == ["the market's news reads risk-off", "CPI today"]
    assert "Macro: CPI TODAY" in r["text"] and r["subject"].endswith("· CPI today")
    eve = build(session, today=date(2025, 6, 10), sector_mood=lambda s, d: {"mood": "neutral"})
    assert "Macro: nothing due today · CPI tomorrow · this week: Wed Jun 11 CPI\n" in eve["text"]  # the Fed: Jun 18
    assert eve["overall"] == "NORMAL"


def test_sectors_are_looked_up_once_and_kept(session, bots):
    asked = []

    def lookup(s):
        asked.append(s)
        return SECTORS.get(s)

    build(session, lookup=lookup)
    session.commit()
    build(session, lookup=lookup)
    assert sorted(asked) == ["AAPL", "MRVL", "VRT", "XOM"]
    assert session.get(SymbolSector, "VRT").sector == "Industrials"


def test_without_prices_or_news_it_still_reports(session, bots):
    def broken(symbols, start):
        raise RuntimeError("Yahoo down")

    r = risk_report.build(session, TODAY, NOW, mood_fn=lambda d: {"mood": "unavailable"},
                          sector_mood_fn=lambda s, d: {"mood": "unavailable"}, closes_fn=broken, sector_lookup=SECTORS.get)
    assert r["overall"] == "NORMAL" and "Market news: UNAVAILABLE" in r["text"]
    later = risk_report.build(session, date(2025, 6, 19), NOW, mood_fn=lambda d: {"mood": "neutral"},
                              sector_mood_fn=lambda s, d: {"mood": "neutral"}, closes_fn=broken, sector_lookup=SECTORS.get)
    assert "Macro: nothing due today · next: Thu Jul 3 jobs report" in later["text"]  # nothing in the week after Jun 19


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(notify, "settings", replace(settings, smtp_host="smtp.test", smtp_username="bot@test.dev",
                                                    smtp_from="", dashboard_url="https://trading.test"))


def _wanted(session, categories=("report",)):
    notify.save(session, {"enabled": True, "recipients": ["me@test.dev"], "categories": list(categories)}, NOW)
    session.commit()


SOURCES = dict(mood_fn=calm, sector_mood_fn=tech_off, closes_fn=lambda symbols, start: closes(symbols, start),
               sector_lookup=SECTORS.get)


def test_it_is_sent_once_a_trading_day_at_8_45_when_chosen(session, bots, smtp):
    _wanted(session)
    before = datetime(2025, 6, 2, 12, 40, tzinfo=timezone.utc)  # 8:40 New York
    assert not risk_report.run_once(before, **SOURCES)
    at = datetime(2025, 6, 2, 12, 45, 30, tzinfo=timezone.utc)  # 8:45 New York
    assert risk_report.run_once(at, **SOURCES)
    assert not risk_report.run_once(NOW + timedelta(minutes=5), **SOURCES)  # once a day
    session.expire_all()
    rows = list(session.scalars(select(Notification).where(Notification.category == "report")))
    assert len(rows) == 1 and rows[0].subject.startswith("Risk report Jun 2: HIGH") and "OVERALL: HIGH" in rows[0].body
    assert "(news up to 08:45 New York)" in rows[0].body  # what was published by then
    saturday = datetime(2025, 6, 7, 13, 30, tzinfo=timezone.utc)
    assert not risk_report.run_once(saturday, **SOURCES)
    late = datetime(2025, 6, 3, 19, 40, tzinfo=timezone.utc)  # 15:40 New York: too late for that day
    assert not risk_report.run_once(late, **SOURCES)


def test_it_isnt_sent_unless_chosen(session, bots, smtp):
    _wanted(session, categories=("trades",))
    assert not risk_report.run_once(NOW, **SOURCES)


def test_the_report_goes_out_in_an_email_of_its_own(session, bots, smtp):
    _wanted(session, categories=("report", "trades"))
    risk_report.queue(session, NOW, **SOURCES)
    notify.enqueue(session, "trades", "buy", "BUY AAPL 1 sh", "bought", now=NOW)
    session.commit()
    sent = []
    assert notify.send_pending(NOW, sent.append) == 2
    subjects = sorted(m["Subject"] for m in sent)
    assert len(sent) == 2 and subjects[1].startswith("[Trading] Risk report Jun 2") and "BUY AAPL" in subjects[0]


def test_the_api_previews_and_sends(session, bots, smtp, monkeypatch):
    monkeypatch.setattr(risk_report, "get_market_mood", calm)
    monkeypatch.setattr(risk_report, "get_sector_mood", tech_off)
    monkeypatch.setattr(risk_report, "yahoo_closes", lambda symbols, start: closes(symbols, start, date.today()))
    monkeypatch.setattr(risk_report, "yahoo_sector", SECTORS.get)
    c = TestClient(main.app)
    r = c.get("/risk-report")
    assert r.status_code == 200, r.text
    assert r.json()["overall"] == "HIGH" and "OVERALL: HIGH" in r.json()["text"]
    assert c.post("/risk-report/send").status_code == 422  # alerts off
    _wanted(session, categories=("trades",))
    r = c.post("/risk-report/send")  # answers at once; the test client runs the background build right after
    assert r.status_code == 202 and r.json() == {"status": "building"}
    session.expire_all()
    rows = list(session.scalars(select(Notification).where(Notification.category == "report")))
    assert len(rows) == 1 and rows[0].status == "pending" and "OVERALL: HIGH" in rows[0].body


def test_etf_categories_join_their_sectors_and_index_funds_follow_the_market(session, bots):
    live, _ = bots
    for s in ("XLF", "SPY", "JPM"):
        session.add(WatchItem(bot_id=live.id, symbol=s, status="watching"))
    session.commit()
    asked = []

    def sector_mood(sector, day):
        asked.append(sector)
        return {"mood": "neutral"}

    lookup = {**SECTORS, "XLF": "Financial", "SPY": "Large Blend", "JPM": "Financial Services"}.get
    r = risk_report.build(session, TODAY, NOW, mood_fn=lambda d: {"mood": "risk_off", "reason": "Hot CPI."},
                          sector_mood_fn=sector_mood, closes_fn=lambda symbols, start: closes(symbols, start),
                          sector_lookup=lookup)
    assert r["symbols"]["XLF"]["sector"] == "Financial Services" == r["symbols"]["JPM"]["sector"]
    assert r["symbols"]["SPY"] == {"level": "HIGH", "why": ["the market's news reads risk-off"], "sector": "Whole market"}
    assert r["symbols"]["JPM"]["level"] == "ELEVATED"  # the market's risk-off raises the rest a step
    assert "Financial" not in asked and "Whole market" not in asked and asked.count("Financial Services") == 1


def test_the_live_bots_slots_held_and_free(session, bots):
    live, _ = bots
    session.add(WatchItem(bot_id=live.id, symbol="AAPL", status="blacklisted"))
    session.commit()
    r = build(session)
    (row,) = r["live"]
    assert (row["id"], row["slots"], row["free_slots"], row["cash"]) == (live.id, 4, 2, 50.0)
    mrvl, vrt = row["held"]
    assert (mrvl["symbol"], mrvl["sector"], mrvl["level"], mrvl["pnl_pct"], mrvl["stop_gap"]) == ("MRVL", "Technology", "HIGH", -0.04, -0.0417)
    assert vrt["stop_gap"] is None and vrt["why"][0].startswith("moves with Technology")
    assert row["watch"] == [{"symbol": "AAPL", "level": "HIGH", "sector": "Technology", "blacklisted": True},
                            {"symbol": "XOM", "level": "NORMAL", "sector": "Energy", "blacklisted": False}]  # held ones left out
    assert "MRVL: Technology news reads risk-off; -4.2% to its stop" in r["text"]
    assert "2 free slots: of what it can buy today, 1 NORMAL" in r["text"]  # AAPL is blacklisted
    tech = next(s for s in r["sector_info"] if s["sector"] == "Technology")
    assert tech["level"] == "HIGH" and tech["reason"] == "Chips sold off in Seoul." and tech["moves_with_it"][0].startswith("VRT 0.9")
    assert [e["ticker"] for e in tech["etf"]] == ["XLK", "SMH"]
    assert r["exposure"]["live"]["HIGH"] == 640.0 and r["exposure"]["paper"]["HIGH"] == 1000.0


def test_the_live_endpoint_is_small_and_has_the_slots(session, bots, monkeypatch):
    import json

    monkeypatch.setattr(risk_report, "get_market_mood", calm)
    monkeypatch.setattr(risk_report, "get_sector_mood", tech_off)
    monkeypatch.setattr(risk_report, "yahoo_closes", lambda symbols, start: closes(symbols, start, date.today()))
    monkeypatch.setattr(risk_report, "yahoo_sector", SECTORS.get)
    r = TestClient(main.app).get("/risk-report/live")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"subject", "overall", "news_until", "market", "macro", "sectors", "live_bots", "paper_exposure"}
    assert body["live_bots"][0]["free_slots"] == 2 and body["sectors"][0]["sector"] == "Technology"
    assert len(json.dumps(body)) < 20_000
