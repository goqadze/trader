"""The trading journal: trades step by step with their emotions, check-ins, day pages, screenshots, statistics, the
CSV export, importing the bots' trades -- and that each user only ever sees their own journal."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
from conftest import make_bot
from fastapi.testclient import TestClient

from app import auth, journal, main
from app.db import SessionLocal
from app.models import JournalImage, JournalNote, JournalTrade, Order, User

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # the first bytes are what makes it a PNG


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
    auth.login_attempts.reset()
    yield
    auth.login_attempts.reset()


def signed_in(username: str) -> TestClient:
    """An active user signed in, in a browser of their own."""
    with SessionLocal() as s:
        s.add(User(username=username, name=username.title(), password_hash=auth.hash_password("password-123"),
                   role="user", status="active"))
        s.commit()
    c = TestClient(main.app)
    assert c.post("/auth/login", json={"username": username, "password": "password-123"}).status_code == 200
    return c


@pytest.fixture
def alice() -> TestClient:
    return signed_in("alice")


@pytest.fixture
def bob() -> TestClient:
    return signed_in("bob")


def iso(day: int, hour: int, minute: int = 0) -> str:
    """A UTC moment in June 2025 (New York is UTC-4: 13:30 UTC is the open)."""
    return datetime(2025, 6, day, hour, minute, tzinfo=timezone.utc).isoformat()


def closed(client, symbol="AAPL", entry=100.0, exit=110.0, qty=10, day=2, **kw):
    """A trade entered and exited on June <day> (New York), long unless side says otherwise."""
    body = {"status": "closed", "symbol": symbol, "entry_price": entry, "exit_price": exit, "quantity": qty,
            "opened_at": iso(day, 14), "closed_at": iso(day, 19), **kw}
    r = client.post("/journal/trades", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- Privacy ---

def test_the_journal_needs_a_signed_in_user():
    anon = TestClient(main.app)
    for path in ("/journal/trades", "/journal/stats", "/journal/options", "/journal/days/2025-06-02", "/journal/notes"):
        assert anon.get(path).status_code == 401, path
    assert anon.post("/journal/trades", json={"symbol": "AAPL"}).status_code == 401


def test_nobody_sees_or_changes_someone_elses_journal(alice, bob):
    t = closed(alice, thesis="secret plan")
    note = alice.post("/journal/notes", json={"stage": "checkin", "emotions": ["anxious"]}).json()
    image = alice.post(f"/journal/trades/{t['id']}/images", content=PNG).json()
    alice.put("/journal/days/2025-06-02", json={"plan": "my day"})

    assert bob.get("/journal/trades").json() == {"total": 0, "items": []}
    assert bob.get(f"/journal/trades/{t['id']}").status_code == 404
    assert bob.patch(f"/journal/trades/{t['id']}", json={"thesis": "x"}).status_code == 404
    assert bob.delete(f"/journal/trades/{t['id']}").status_code == 404
    assert bob.post("/journal/notes", json={"stage": "during", "text": "x", "trade_id": t["id"]}).status_code == 404
    assert bob.patch(f"/journal/notes/{note['id']}", json={"text": "x"}).status_code == 404
    assert bob.delete(f"/journal/notes/{note['id']}").status_code == 404
    assert bob.post(f"/journal/trades/{t['id']}/images", content=PNG).status_code == 404
    assert bob.get(f"/journal/images/{image['id']}").status_code == 404
    assert bob.delete(f"/journal/images/{image['id']}").status_code == 404
    assert bob.get("/journal/notes").json() == []
    assert bob.get("/journal/days/2025-06-02").json()["plan"] == ""
    assert bob.get("/journal/calendar?start=2025-06-01&end=2025-06-30").json() == []
    assert bob.get("/journal/stats").json()["trades"] == 0
    assert bob.get("/journal/options").json()["symbols"] == []
    assert bob.get("/journal/trades.csv").text.count("\n") == 1  # the header only
    # ... and alice's journal is untouched
    assert alice.get(f"/journal/trades/{t['id']}").json()["thesis"] == "secret plan"


def test_deleting_an_account_deletes_its_journal(alice):
    t = closed(alice)
    alice.post("/journal/notes", json={"stage": "checkin", "text": "hi"})
    alice.post(f"/journal/trades/{t['id']}/images", content=PNG)
    alice.put("/journal/days/2025-06-02", json={"plan": "p"})
    with SessionLocal() as s:
        s.delete(s.query(User).filter_by(username="alice").one())
        s.commit()
        assert s.query(JournalTrade).count() == s.query(JournalNote).count() == s.query(JournalImage).count() == 0


# --- A trade, step by step ---

def test_a_trade_goes_from_plan_to_entry_to_exit_to_review_with_a_note_at_each_step(alice):
    r = alice.post("/journal/trades", json={
        "symbol": " aapl ", "setup": "Breakout", "tags": ["swing", "Swing", " earnings  week "], "entry_price": 100,
        "stop_price": 95, "target_price": 115, "quantity": 10, "confidence": 4, "thesis": "Base breakout on volume",
        "note": {"stage": "plan", "emotions": ["Confident", "fomo", "FOMO"], "intensity": 3, "text": "  looks great  ",
                 "at": iso(2, 13)}})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["status"] == "planned" and t["symbol"] == "AAPL" and t["tags"] == ["swing", "earnings week"]
    assert t["risk"] == 50.0 and t["reward_risk"] == 3.0 and t["pnl"] is None and t["source"] == "manual"
    assert [(n["stage"], n["emotions"], n["text"]) for n in t["note_list"]] == [("plan", ["confident", "fomo"], "looks great")]

    # Entering needs the fill, the size and the time
    r = alice.patch(f"/journal/trades/{t['id']}", json={"status": "open", "quantity": None})
    assert r.status_code == 422 and "opened at" in r.json()["detail"] and "quantity" in r.json()["detail"]
    r = alice.patch(f"/journal/trades/{t['id']}", json={
        "status": "open", "entry_price": 100.5, "opened_at": iso(2, 14, 5),
        "note": {"stage": "entry", "emotions": ["anxious"], "at": iso(2, 14, 5)}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open" and r.json()["entry_price"] == 100.5

    alice.post("/journal/notes", json={"trade_id": t["id"], "stage": "during", "emotions": ["impatient"], "at": iso(2, 16)})

    r = alice.patch(f"/journal/trades/{t['id']}", json={
        "status": "closed", "exit_price": 110.5, "closed_at": iso(2, 19, 35), "fees": 2, "exit_reason": "target-ish",
        "note": {"stage": "exit", "emotions": ["relieved"], "at": iso(2, 19, 35)}})
    assert r.status_code == 200, r.text
    t = r.json()
    # (110.5 - 100.5) x 10 - 2 fees = 98; one R = (100.5 - 95) x 10 = 55
    assert t["pnl"] == 98.0 and t["r_multiple"] == round(98 / 55, 2) and t["return_pct"] == round(98 / 1005 * 100, 2)
    assert t["hold_minutes"] == 5 * 60 + 30 and t["day"] == "2025-06-02"

    r = alice.patch(f"/journal/trades/{t['id']}", json={
        "followed_plan": False, "grade": 3, "mistakes": ["moved-stop", "Moved-Stop"], "review": "held fine",
        "lessons": "size down", "note": {"stage": "review", "text": "ok", "at": iso(2, 21)}})
    t = r.json()
    assert t["followed_plan"] is False and t["grade"] == 3 and t["mistakes"] == ["moved-stop"]
    assert [n["stage"] for n in t["note_list"]] == ["plan", "entry", "during", "exit", "review"]
    assert t["emotions"] == ["confident", "fomo", "anxious", "impatient", "relieved"] and t["notes"] == 5
    listed = alice.get("/journal/trades").json()["items"][0]
    assert listed["emotions"] == t["emotions"] and listed["notes"] == 5 and listed["pnl"] == 98.0


def test_a_short_trade_wins_when_the_price_falls(alice):
    t = closed(alice, symbol="TSLA", side="short", entry=50, exit=45, qty=10, fees=1, stop_price=52, target_price=40)
    assert t["pnl"] == 49.0 and t["risk"] == 20.0 and t["r_multiple"] == 2.45 and t["reward_risk"] == 5.0


@pytest.mark.parametrize("body, problem", [
    ({"symbol": "AAPL", "entry_price": 100, "stop_price": 105}, "stop is below its entry"),
    ({"symbol": "AAPL", "side": "short", "entry_price": 100, "stop_price": 95}, "stop is above its entry"),
    ({"symbol": "AAPL", "entry_price": 100, "target_price": 90}, "target is above its entry"),
    ({"symbol": "AAPL", "status": "closed", "entry_price": 100, "quantity": 1, "opened_at": iso(2, 14)}, "exit price"),
    ({"symbol": "AAPL", "status": "closed", "entry_price": 100, "exit_price": 101, "quantity": 1, "opened_at": iso(2, 14),
      "closed_at": iso(2, 13)}, "exit can't be before the entry"),
    ({"symbol": "AAPL", "status": "open", "entry_price": 100, "quantity": 1, "opened_at": "2025-06-02T14:00:00"}, "time zone"),
    ({"symbol": "no spaces"}, "not a symbol"),
    ({"symbol": "AAPL", "quantity": 0}, "greater than 0"),
    ({"symbol": "AAPL", "confidence": 6}, "less than or equal to 5"),
    ({"symbol": "AAPL", "tags": ["x" * 33]}, "longer than 32"),
    ({"symbol": "AAPL", "note": {"stage": "plan"}}, "an emotion or some words"),
])
def test_a_trade_that_doesnt_add_up_is_refused(alice, body, problem):
    r = alice.post("/journal/trades", json=body)
    assert r.status_code == 422 and problem in r.text, r.text


def test_a_stop_on_the_wrong_side_is_no_risk():
    trade = lambda **kw: SimpleNamespace(**({"entry_price": 100.0, "quantity": 10, "side": "long"} | kw))
    assert journal._risk(trade(stop_price=95.0)) == 50.0
    assert journal._risk(trade(stop_price=105.0)) is None  # above a long's entry: not the first stop's risk
    assert journal._risk(trade(stop_price=105.0, side="short")) == 50.0
    assert journal._risk(trade(stop_price=95.0, side="short")) is None
    assert journal._risk(trade(stop_price=None)) is None and journal._risk(trade(stop_price=95.0, quantity=None)) is None


def test_symbols_beyond_stocks_are_fine(alice):
    for s in ("BRK.B", "BTC-USD", "EUR/USD", "ES=F", "^GSPC", "MNQZ6"):
        assert alice.post("/journal/trades", json={"symbol": s}).status_code == 201, s


def test_a_trade_keeps_only_what_its_status_has(alice):
    t = closed(alice)
    r = alice.patch(f"/journal/trades/{t['id']}", json={"status": "open"})  # reopened: no exit, no P&L
    assert r.json()["exit_price"] is None and r.json()["closed_at"] is None and r.json()["pnl"] is None
    r = alice.patch(f"/journal/trades/{t['id']}", json={"status": "skipped"})  # not taken after all: no entry time either
    assert r.json()["opened_at"] is None and r.json()["entry_price"] == 100.0  # the plan's price stays


def test_an_update_takes_only_known_fields(alice):
    t = closed(alice)
    assert alice.patch(f"/journal/trades/{t['id']}", json={"pnl": 1_000_000}).status_code == 422
    assert alice.patch(f"/journal/trades/{t['id']}", json={"user_id": 99}).status_code == 422
    assert alice.get(f"/journal/trades/{t['id']}").json()["pnl"] == 100.0


def test_deleting_a_trade_deletes_its_notes_and_screenshots(alice):
    t = closed(alice, note={"stage": "entry", "text": "in"})
    alice.post(f"/journal/trades/{t['id']}/images", content=PNG)
    checkin = alice.post("/journal/notes", json={"stage": "checkin", "text": "a day note"}).json()
    assert alice.delete(f"/journal/trades/{t['id']}").status_code == 204
    assert alice.get(f"/journal/trades/{t['id']}").status_code == 404
    with SessionLocal() as s:
        assert s.query(JournalImage).count() == 0
        assert [n.id for n in s.query(JournalNote)] == [checkin["id"]]  # the check-in isn't the trade's


# --- Notes and check-ins ---

def test_a_check_in_belongs_to_the_day_and_a_trade_note_says_its_stage(alice):
    t = closed(alice)
    r = alice.post("/journal/notes", json={"stage": "checkin", "emotions": ["Tired"], "intensity": 2, "at": iso(3, 1)})
    assert r.status_code == 201 and r.json()["trade_id"] is None and r.json()["emotions"] == ["tired"]
    assert alice.post("/journal/notes", json={"stage": "entry", "text": "x"}).status_code == 422  # a stage needs a trade
    assert alice.post("/journal/notes", json={"stage": "checkin", "text": "x", "trade_id": t["id"]}).status_code == 422
    assert alice.post("/journal/notes", json={"stage": "checkin"}).status_code == 422  # says nothing
    note = alice.post("/journal/notes", json={"stage": "during", "text": "holding", "trade_id": t["id"]}).json()
    assert note["symbol"] == "AAPL"

    r = alice.patch(f"/journal/notes/{note['id']}", json={"emotions": ["calm"], "text": ""})
    assert r.status_code == 200 and r.json()["emotions"] == ["calm"] and r.json()["text"] == ""
    assert alice.patch(f"/journal/notes/{note['id']}", json={"emotions": []}).status_code == 422  # would say nothing
    assert alice.patch(f"/journal/notes/{note['id']}", json={"stage": "checkin"}).status_code == 422
    assert alice.delete(f"/journal/notes/{note['id']}").status_code == 204

    # June 3 01:00 UTC is still June 2 in New York
    assert [n["stage"] for n in alice.get("/journal/notes?start=2025-06-02&end=2025-06-02").json()] == ["checkin"]
    assert alice.get("/journal/notes?start=2025-06-03").json() == []
    assert len(alice.get("/journal/notes?stage=checkin").json()) == 1


# --- The list: filters, search, sorting ---

def test_filters_search_and_sorting(alice):
    a = closed(alice, symbol="AAPL", exit=110, tags=["Swing"], setup="Breakout", day=2,
               note={"stage": "entry", "emotions": ["fomo"], "text": "chased the gap"})
    b = closed(alice, symbol="MSFT", exit=90, mistakes=["no-stop"], setup="breakout", day=3, account="Alpaca live")
    c = closed(alice, symbol="NVDA", exit=100, day=4)  # breakeven
    d = alice.post("/journal/trades", json={"symbol": "AMD", "thesis": "watch the 50-day"}).json()

    def ids(query=""):
        r = alice.get(f"/journal/trades?{query}")
        assert r.status_code == 200, r.text
        return [t["id"] for t in r.json()["items"]]

    assert ids() == [d["id"], c["id"], b["id"], a["id"]]  # newest trading day first (the plan's: the day it was written)
    assert ids("status=planned") == [d["id"]]
    assert ids("result=win") == [a["id"]] and ids("result=loss") == [b["id"]] and ids("result=breakeven") == [c["id"]]
    assert ids("tag=swing") == [a["id"]] and ids("mistake=NO-STOP") == [b["id"]] and ids("emotion=FOMO") == [a["id"]]
    assert ids("setup=BREAKOUT") == [b["id"], a["id"]] and ids("account=alpaca%20live") == [b["id"]]
    assert ids("q=gap") == [a["id"]]  # in a note
    assert ids("q=50-day") == [d["id"]] and ids("q=msft") == [b["id"]]
    assert ids("start=2025-06-03&end=2025-06-03") == [b["id"]]
    assert ids("sort=pnl&order=desc") == [a["id"], c["id"], b["id"], d["id"]]  # no P&L yet: last
    assert ids("sort=pnl&order=asc") == [b["id"], c["id"], a["id"], d["id"]]
    assert ids("order=asc")[0] == a["id"]
    page = alice.get("/journal/trades?limit=2&offset=2").json()
    assert page["total"] == 4 and [t["id"] for t in page["items"]] == [b["id"], a["id"]]


# --- Statistics ---

def test_statistics_of_the_closed_trades(alice):
    # Mon June 2: +100 (R 2.0, fomo at the entry), Tue June 3: -50 (R -1.0), Wed June 4: +30, Thu June 5: -20
    # (broke the plan), Fri June 6: +40; plus an open and a planned trade, which only count as such
    closed(alice, exit=110, stop_price=95, day=2, followed_plan=True, grade=5, setup="Breakout",
           note={"stage": "entry", "emotions": ["fomo"]})
    closed(alice, exit=95, stop_price=95, day=3, followed_plan=True, setup="Breakout", mistakes=["late-entry"])
    t3 = closed(alice, exit=103, day=4, setup="Pullback", tags=["swing"])
    closed(alice, exit=98, day=5, followed_plan=False, mistakes=["late-entry"], side="long")
    closed(alice, exit=104, day=6, symbol="MSFT")
    alice.post("/journal/trades", json={"symbol": "AMD", "status": "open", "entry_price": 1, "quantity": 1,
                                        "opened_at": iso(6, 14)})
    alice.post("/journal/trades", json={"symbol": "AMD"})
    # Felt after the exit: not an entry emotion. A check-in counts in how often each emotion came up
    alice.post("/journal/notes", json={"trade_id": t3["id"], "stage": "exit", "emotions": ["greedy"]})
    alice.post("/journal/notes", json={"stage": "checkin", "emotions": ["fomo", "calm"], "at": iso(4, 15)})

    s = alice.get("/journal/stats").json()
    assert (s["trades"], s["open"], s["planned"], s["wins"], s["losses"], s["breakeven"]) == (5, 1, 1, 3, 2, 0)
    assert s["win_rate"] == 60.0 and s["net_pnl"] == 100.0 and s["gross_profit"] == 170.0 and s["gross_loss"] == -70.0
    assert s["profit_factor"] == round(170 / 70, 2) and s["expectancy"] == 20.0
    assert s["avg_win"] == round(170 / 3, 2) and s["avg_loss"] == -35.0 and s["payoff_ratio"] == round((170 / 3) / 35, 2)
    assert (s["avg_r"], s["total_r"], s["r_trades"]) == (0.5, 1.0, 2)
    assert s["best"]["pnl"] == 100.0 and s["worst"]["pnl"] == -50.0 and s["worst"]["day"] == "2025-06-03"
    assert (s["max_win_streak"], s["max_loss_streak"], s["current_streak"]) == (1, 1, 1)
    assert s["max_drawdown"] == -50.0  # +100, then 50 of it given back
    assert [d["cumulative"] for d in s["daily"]] == [100.0, 50.0, 80.0, 60.0, 100.0]
    assert [(g["key"], g["trades"], g["pnl"]) for g in s["by_setup"]] == [("Breakout", 2, 50.0), ("Pullback", 1, 30.0)]
    assert [(g["key"], g["trades"], g["pnl"]) for g in s["by_mistake"]] == [("late-entry", 2, -70.0)]
    assert [(g["key"], g["pnl"], g["win_rate"]) for g in s["by_emotion"]] == [("fomo", 100.0, 100.0)]
    assert [g["key"] for g in s["by_weekday"]] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert [(g["key"], g["pnl"]) for g in s["by_plan"]] == [("followed", 50.0), ("broke", -20.0)]
    assert [g["key"] for g in s["by_hour"]] == ["10:00"]  # 14:00 UTC = 10:00 New York
    assert s["emotions"] == [{"emotion": "fomo", "count": 2}, {"emotion": "calm", "count": 1}, {"emotion": "greedy", "count": 1}]

    # Filtered to some trades: only their notes count (no check-ins), and the counts follow the filter
    s = alice.get("/journal/stats?setup=breakout").json()
    assert s["trades"] == 2 and s["net_pnl"] == 50.0 and s["open"] == 0
    assert s["emotions"] == [{"emotion": "fomo", "count": 1}]
    s = alice.get("/journal/stats?start=2025-06-05").json()
    assert s["trades"] == 2 and s["emotions"] == [] and s["max_drawdown"] == -20.0


def test_streaks_and_an_empty_journal():
    assert journal._streaks(["win", "win", "loss", "loss", "loss", "breakeven", "win"]) == (2, 3, 1)
    assert journal._streaks(["loss", "loss"]) == (0, 2, -2)
    assert journal._streaks(["loss", "breakeven", "loss"]) == (0, 1, -1)  # a breakeven trade ends a run
    s = journal.stats([], {}, journal.Counter())
    assert s.trades == 0 and s.win_rate is None and s.profit_factor is None and s.best is None and s.max_drawdown == 0


# --- Days and the calendar ---

def test_a_day_page_with_its_notes_and_trades(alice):
    t = closed(alice, day=2)
    alice.post("/journal/notes", json={"stage": "checkin", "emotions": ["calm"], "at": iso(2, 12)})
    alice.post("/journal/notes", json={"stage": "checkin", "text": "late", "at": iso(3, 3, 59)})  # 23:59 New York, June 2
    alice.post("/journal/notes", json={"stage": "checkin", "text": "next day", "at": iso(3, 4, 0)})
    r = alice.put("/journal/days/2025-06-02", json={"mood": 4, "sleep": 2, "plan": " watch AAPL ", "grade": 5,
                                                   "followed_rules": True})
    assert r.status_code == 200, r.text
    d = alice.get("/journal/days/2025-06-02").json()
    assert d["plan"] == "watch AAPL" and d["mood"] == 4 and d["sleep"] == 2 and d["energy"] is None and d["updated_at"]
    assert [n["text"] or n["emotions"][0] for n in d["notes"]] == ["calm", "late"]
    assert [x["id"] for x in d["trades"]] == [t["id"]] and d["pnl"] == 100.0
    assert alice.put("/journal/days/2025-06-02", json={"mood": 0}).status_code == 422
    blank = alice.get("/journal/days/2025-06-09").json()
    assert blank["updated_at"] is None and blank["notes"] == [] and blank["trades"] == []


def test_the_calendar_shows_each_days_pnl_notes_and_page(alice):
    closed(alice, exit=110, day=2)
    closed(alice, exit=95, day=2)
    closed(alice, exit=120, day=20)  # outside
    alice.post("/journal/notes", json={"stage": "checkin", "text": "x", "at": iso(3, 15)})
    alice.put("/journal/days/2025-06-04", json={"mood": 2, "grade": 3})
    alice.put("/journal/days/2025-06-05", json={})  # nothing written: not a page
    days = alice.get("/journal/calendar?start=2025-06-01&end=2025-06-10").json()
    assert [(d["day"], d["pnl"], d["closed"], d["wins"], d["losses"], d["opened"], d["notes"], d["page"]) for d in days] == [
        ("2025-06-02", 50.0, 2, 1, 1, 2, 0, False),
        ("2025-06-03", 0.0, 0, 0, 0, 0, 1, False),
        ("2025-06-04", 0.0, 0, 0, 0, 0, 0, True),
    ]
    assert days[2]["mood"] == 2 and days[2]["grade"] == 3
    assert alice.get("/journal/calendar?start=2025-06-10&end=2025-06-01").status_code == 422
    assert alice.get("/journal/calendar?start=2024-01-01&end=2025-06-01").status_code == 422


# --- Screenshots ---

def test_screenshots_are_checked_kept_and_served_as_images(alice):
    t = closed(alice)
    r = alice.post(f"/journal/trades/{t['id']}/images?caption=%20the%20entry%20", content=PNG)
    assert r.status_code == 201, r.text
    image = r.json()
    assert image["content_type"] == "image/png" and image["size"] == len(PNG) and image["caption"] == "the entry"
    got = alice.get(f"/journal/images/{image['id']}")
    assert got.content == PNG and got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff" and "default-src 'none'" in got.headers["content-security-policy"]
    jpeg = alice.post(f"/journal/trades/{t['id']}/images", content=b"\xff\xd8\xff\xe0" + b"\x00" * 10).json()
    webp = alice.post(f"/journal/trades/{t['id']}/images", content=b"RIFF\x00\x00\x00\x00WEBPVP8 ").json()
    assert (jpeg["content_type"], webp["content_type"]) == ("image/jpeg", "image/webp")
    detail = alice.get(f"/journal/trades/{t['id']}").json()
    assert [i["id"] for i in detail["image_list"]] == [image["id"], jpeg["id"], webp["id"]] and detail["images"] == 3

    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert alice.post(f"/journal/trades/{t['id']}/images", content=svg).status_code == 415
    assert alice.post(f"/journal/trades/{t['id']}/images", content=b"").status_code == 415
    too_big = PNG + b"\x00" * journal.MAX_IMAGE_BYTES
    assert alice.post(f"/journal/trades/{t['id']}/images", content=too_big).status_code == 413
    assert alice.delete(f"/journal/images/{jpeg['id']}").status_code == 204
    assert alice.get(f"/journal/images/{jpeg['id']}").status_code == 404


def test_a_trade_keeps_a_limited_number_of_screenshots(alice, monkeypatch):
    monkeypatch.setattr(journal, "MAX_IMAGES", 2)
    t = closed(alice)
    for _ in range(2):
        assert alice.post(f"/journal/trades/{t['id']}/images", content=PNG).status_code == 201
    assert alice.post(f"/journal/trades/{t['id']}/images", content=PNG).status_code == 409


# --- Export, suggestions ---

def test_the_csv_export_has_every_trade_and_quotes_formulas(alice):
    closed(alice, symbol="AAPL", thesis="=HYPERLINK(\"http://evil\")", tags=["a", "b"], day=2,
           note={"stage": "entry", "emotions": ["calm"]})
    closed(alice, symbol="MSFT", exit=90, day=3, review="-oops")
    r = alice.get("/journal/trades.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith('attachment; filename="journal-')
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("id,day,status,symbol,side,") and len(lines) == 3
    assert lines[1].split(",")[1:4] == ["2025-06-02", "closed", "AAPL"]  # oldest first
    assert "'=HYPERLINK" in lines[1] and "a; b" in lines[1] and ",calm," in lines[1]
    assert "'-oops" in lines[2] and ",-100.0," in lines[2]  # a negative number stays a number
    assert alice.get("/journal/trades.csv?symbol=x&result=loss").text.count("MSFT") == 1


def test_suggestions_are_what_you_used_most(alice):
    closed(alice, symbol="MSFT", setup="Pullback", tags=["swing"], account="Alpaca live")
    closed(alice, symbol="AAPL", setup="Breakout", tags=["Swing", "earnings"], account="alpaca live",
           note={"stage": "entry", "emotions": ["calm", "fomo"]})
    closed(alice, symbol="AAPL", setup="breakout", mistakes=["no-stop"], note={"stage": "entry", "emotions": ["fomo"]})
    o = alice.get("/journal/options").json()
    assert o["symbols"] == ["AAPL", "MSFT"] and o["setups"] == ["Breakout", "Pullback"]
    assert o["tags"] == ["swing", "earnings"] and o["accounts"] == ["Alpaca live"]
    assert o["mistakes"] == ["no-stop"] and o["emotions"] == ["fomo", "calm"]


# --- Importing the bots' trades ---

def _order(session, bot, side, qty, price, minute, fee=0.0, pnl=None, symbol=None, reason="dip", status="filled"):
    when = datetime(2025, 6, 2, 14, minute, tzinfo=timezone.utc)
    o = Order(bot_id=bot.id, side=side, symbol=symbol, qty=qty, filled_qty=qty if status != "canceled" else 0,
              avg_price=price, fee=fee, pnl=pnl, reason=reason, status=status, created_at=when, updated_at=when,
              client_order_id=f"c{bot.id}-{side}-{minute}-{symbol}")
    session.add(o)
    session.commit()
    return o


def test_importing_a_bots_trades_matches_its_pnl_to_the_cent_and_never_twice(alice, bob, session):
    bot = make_bot(session, name="Dips", strategy="dip_buyer", symbol="DIP", broker="alpaca-live", stop_pct=0.05)
    other = make_bot(session, name="Nothing sold", symbol="MSFT")
    _order(session, bot, "BUY", 10, 100.0, 0, fee=1.0, symbol="XLE")
    _order(session, bot, "BUY", 5, 50.0, 1, symbol="GLD")
    _order(session, bot, "SELL", 10, 110.0, 2, fee=1.0, pnl=98.0, symbol="XLE", reason="target")  # 1100 - 1 - 1001
    _order(session, bot, "SELL", 5, 50.0, 3, symbol="GLD", status="canceled")  # never filled: not a trade
    _order(session, bot, "SELL", 2, 45.0, 4, pnl=-10.0, symbol="GLD", reason="stop-loss")  # part of the position
    _order(session, bot, "SELL", 3, 47.0, 5, pnl=-9.0, symbol="GLD", reason="manual")  # the rest
    _order(session, bot, "BUY", 4, 40.0, 6, symbol="GLD")  # a new position, still open
    _order(session, other, "BUY", 1, 300.0, 7)

    choices = alice.get("/journal/import/bots").json()
    assert [(b["id"], b["closed_trades"], b["imported"], b["broker"]) for b in choices] == [(bot.id, 3, 0, "alpaca-live")]

    r = alice.post("/journal/import", json={"bot_ids": [bot.id, bot.id]})
    assert r.json() == {"imported": 3, "already": 0}
    trades = {(t["symbol"], t["pnl"]): t for t in alice.get("/journal/trades?sort=pnl").json()["items"]}
    xle = trades[("XLE", 98.0)]
    assert xle["status"] == "closed" and xle["source"] == "bot" and xle["bot_id"] == bot.id and xle["tags"] == ["bot"]
    assert xle["entry_price"] == pytest.approx(100.1) and xle["exit_price"] == 110.0 and xle["fees"] == 1.0
    assert xle["stop_price"] == pytest.approx(95.095) and xle["exit_reason"] == "Target reached"
    assert xle["account"] == "Alpaca live" and xle["setup"] == "Dip buyer" and "bot #" in xle["thesis"]
    assert xle["opened_at"].startswith("2025-06-02T14:00") and xle["closed_at"].startswith("2025-06-02T14:02")
    gld = [trades[("GLD", -10.0)], trades[("GLD", -9.0)]]
    assert [t["quantity"] for t in gld] == [2, 3] and all(t["opened_at"].startswith("2025-06-02T14:01") for t in gld)
    assert gld[0]["entry_price"] == pytest.approx(50.0) and gld[1]["exit_reason"] == "Sold by hand from the dashboard"

    # Again: nothing new. Another user imports their own copy
    assert alice.post("/journal/import", json={"bot_ids": [bot.id]}).json() == {"imported": 0, "already": 3}
    assert alice.get("/journal/import/bots").json()[0]["imported"] == 3
    assert bob.post("/journal/import", json={"bot_ids": [bot.id]}).json() == {"imported": 3, "already": 0}
    assert alice.post("/journal/import", json={"bot_ids": [999]}).status_code == 404
    assert alice.post("/journal/import", json={"bot_ids": []}).status_code == 422


def test_a_new_position_after_a_full_sale_starts_its_own_entry_time(session):
    bot = make_bot(session, symbol="AAPL", stop_pct=0.04)
    _order(session, bot, "BUY", 10, 100.0, 0, reason="signal")
    _order(session, bot, "SELL", 10, 105.0, 1, pnl=50.0, reason="signal")
    _order(session, bot, "BUY", 10, 101.0, 2, reason="signal")
    _order(session, bot, "SELL", 10, 99.0, 3, pnl=-20.0, reason="stop-loss")
    trips = journal.round_trips(session, bot)
    assert [(t["symbol"], t["opened_at"].minute, t["closed_at"].minute) for t in trips] == [("AAPL", 0, 1), ("AAPL", 2, 3)]
    assert [round(t["entry_price"], 2) for t in trips] == [100.0, 101.0]  # what each position cost per share
    rotation = make_bot(session, symbol="ROT", strategy="momentum_rotation")
    _order(session, rotation, "BUY", 10, 100.0, 4, symbol="XLK", reason="rotation")
    _order(session, rotation, "SELL", 10, 101.0, 5, pnl=10.0, symbol="XLK", reason="rotation")
    assert journal.round_trips(session, rotation)[0]["stop_price"] is None  # a rotation has no stop: no R
    assert [journal._setup_name(b) for b in (bot, rotation)] == ["SMA trend + RSI", "Momentum rotation"]
