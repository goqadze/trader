"""Email alerts: what gets queued (and what doesn't), one email for everything that piled up, retries, and the settings
API. No mail server: the sending is faked."""

import smtplib
from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import FakeBroker, at, make_bot
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import dip, main, notify
from app.config import settings
from app.models import DIP, Notification
from app.trader import log_event, submit_order

T = at(15, 0)  # Monday June 2 2025, 11:00 New York


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(notify, "settings", replace(settings, smtp_host="smtp.test", smtp_username="bot@test.dev",
                                                    smtp_from="", dashboard_url="https://trading.test"))


def _on(session, categories=("trades", "risk", "problems"), recipients=("me@test.dev",), enabled=True):
    notify.save(session, {"enabled": enabled, "recipients": list(recipients), "categories": list(categories)}, T)
    session.commit()


def _queued(session) -> list[Notification]:
    session.expire_all()
    return list(session.scalars(select(Notification).order_by(Notification.id)))


def test_nothing_is_queued_without_a_mail_server_or_while_alerts_are_off(session, smtp, monkeypatch):
    bot = make_bot(session)
    log_event(session, bot.id, "error", "Signal failed: decision-service down", "error", T)  # alerts never switched on
    _on(session, enabled=False)
    log_event(session, bot.id, "error", "Signal failed: decision-service down", "error", T)
    monkeypatch.setattr(notify, "settings", settings)  # no SMTP_HOST
    _on(session)
    log_event(session, bot.id, "error", "Signal failed: decision-service down", "error", T)
    session.commit()
    assert _queued(session) == []


def test_fills_and_risk_events_are_queued_routine_events_are_not(session, smtp):
    _on(session)
    bot = make_bot(session, name="AAPL swing")
    broker = FakeBroker(price=100.0, now=T)
    submit_order(session, bot, broker, "BUY", 10, "signal", T)
    log_event(session, bot.id, "params", "Changed: stop_pct 0.04 → 0.05", now=T)  # routine: no email
    log_event(session, bot.id, "risk", "AAPL blacklisted: stop-loss at $95.00 (stop $96.00). No more buys until you "
              "re-enable it.", "warning", T)
    broker.price = 95.0
    submit_order(session, bot, broker, "SELL", 10, "stop-loss", T)
    session.commit()

    buy, blacklisted, stop = _queued(session)
    assert (buy.category, buy.kind, buy.subject) == ("trades", "buy", "BUY AAPL: 10 @ $100.00")
    assert "target $108.00 (+8.0%) or its stop-loss $96.00 (-4.0%)" in buy.body and "paper (built-in simulator)" in buy.body
    assert (blacklisted.category, blacklisted.subject) == ("risk", "AAPL swing: AAPL blacklisted: stop-loss at $95.00 (stop $96.00)")
    assert (stop.category, stop.kind, stop.subject) == ("risk", "sell", "STOP-LOSS AAPL -$50.00 (-5.0%)")  # risk, not just a trade
    assert "Profit/loss -$50.00 (-5.0% on $1,000.00), after fees." in stop.body and stop.bot_id == bot.id


def test_only_the_chosen_categories_are_queued(session, smtp):
    _on(session, categories=["risk"])
    bot = make_bot(session)
    submit_order(session, bot, FakeBroker(price=100.0, now=T), "BUY", 10, "signal", T)  # a trade: not chosen
    log_event(session, None, "halt", "HALT ALL: paused 3 bot(s)", "warning", T)
    session.commit()
    assert [(n.category, n.subject, n.bot_id) for n in _queued(session)] == [("risk", "HALT ALL: paused 3 bot(s)", None)]


def test_a_failing_bot_emails_once_an_hour(session, smtp):
    _on(session)
    a, b = make_bot(session, name="A"), make_bot(session, name="B")
    for minutes in (0, 15, 30):
        log_event(session, a.id, "error", "Signal failed: timeout", "error", T + timedelta(minutes=minutes))
    log_event(session, b.id, "error", "Signal failed: timeout", "error", T + timedelta(minutes=15))  # another bot: its own
    log_event(session, a.id, "error", "Signal failed: timeout", "error", T + timedelta(minutes=61))
    session.commit()
    assert [(n.bot_id, n.created_at) for n in _queued(session)] == [(a.id, T), (b.id, T + timedelta(minutes=15)),
                                                                     (a.id, T + timedelta(minutes=61))]


def test_dip_signals_only_for_those_who_chose_them(session, smtp):
    _on(session)
    bot = make_bot(session, strategy=DIP, symbol=DIP, name="Dips")
    dip.signal(session, bot, "AAPL", "down", T, "AAPL is 6.0% under its 5-day high: buy", "bought 48", 103.0, 110.0, -0.06)
    session.commit()
    assert _queued(session) == []
    _on(session, categories=["trades", "signals"])
    dip.signal(session, bot, "AAPL", "rebound", T, "AAPL turned up +1.2% from its low $101.80: buy", "bought 48", 103.0)
    session.commit()
    (n,) = _queued(session)
    assert (n.category, n.kind, n.subject) == ("signals", "signal-rebound", "AAPL turned up from its low")
    assert n.body.startswith("AAPL turned up +1.2% from its low $101.80: buy\nbought 48\nBot: Dips")


def test_what_piled_up_goes_out_in_one_email_and_a_failed_send_is_retried(session, smtp):
    _on(session, recipients=["me@test.dev", "you@test.dev"])
    bot = make_bot(session, name="AAPL swing")
    submit_order(session, bot, FakeBroker(price=100.0, now=T), "BUY", 10, "signal", T)
    log_event(session, bot.id, "error", "Signal failed: timeout", "error", T)
    session.commit()
    sent = []

    def down(msg):
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    assert notify.send_pending(T, down) == 0
    buy, err = _queued(session)
    assert (buy.status, buy.attempts, buy.next_attempt_at) == ("pending", 1, T + timedelta(minutes=1))
    assert buy.error == "SMTPServerDisconnected: Connection unexpectedly closed"
    assert notify.send_pending(T + timedelta(seconds=30), sent.append) == 0  # not yet due again
    assert notify.send_pending(T + timedelta(minutes=2), sent.append) == 2

    (msg,) = sent
    assert msg["Subject"] == "[Trading] 2 alerts: BUY AAPL: 10 @ $100.00 · AAPL swing: Signal failed: timeout"
    assert (msg["From"], msg["To"]) == ("bot@test.dev", "me@test.dev, you@test.dev")
    text = msg.get_body(("plain",)).get_content()
    assert "Mon Jun 02, 11:00 New York (15:00 UTC)" in text and f"https://trading.test/trading/{bot.id}" in text
    assert "Signal failed: timeout" in msg.get_body(("html",)).get_content()
    assert {(n.status, n.attempts, n.error) for n in _queued(session)} == {("sent", 2, None)}


def test_it_gives_up_after_the_retries_and_skips_alerts_turned_off_meanwhile(session, smtp):
    _on(session)
    bot = make_bot(session)
    log_event(session, bot.id, "error", "Signal failed: timeout", "error", T)
    session.commit()

    def refused(msg):
        raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    now = T
    for _ in range(len(notify.RETRY_AFTER) + 1):
        notify.send_pending(now, refused)
        now += timedelta(hours=3)
    (n,) = _queued(session)
    assert (n.status, n.attempts) == ("failed", 5) and "Username and Password not accepted" in n.error

    log_event(session, bot.id, "order", "BUY 10 AAPL rejected: insufficient buying power", "warning", now)
    session.commit()
    _on(session, enabled=False)
    assert notify.send_pending(now, refused) == 0
    assert _queued(session)[-1].status == "skipped"


@pytest.fixture
def api(smtp):
    with TestClient(main.app) as c:
        yield c


def test_the_settings_api(api, monkeypatch):
    c = api
    cfg = c.get("/email-alerts").json()
    assert (cfg["enabled"], cfg["recipients"], cfg["categories"]) == (False, [], ["trades", "risk", "problems"])
    assert cfg["smtp_configured"] and cfg["smtp_server"] == "smtp.test:587 (starttls)" and cfg["sender"] == "bot@test.dev"
    assert [x["key"] for x in cfg["available"]] == ["trades", "risk", "problems", "signals"]

    assert c.put("/email-alerts", json={"enabled": True, "recipients": [], "categories": []}).status_code == 422
    r = c.put("/email-alerts", json={"enabled": True, "recipients": ["me@test.dev", "nope"], "categories": ["trades"]})
    assert r.status_code == 422 and "not an email address: nope" in r.text
    r = c.put("/email-alerts", json={"enabled": True, "recipients": [" me@test.dev ", "me@test.dev"],
                                     "categories": ["trades", "signals", "trades"]})
    assert r.status_code == 200 and (r.json()["recipients"], r.json()["categories"]) == (["me@test.dev"], ["trades", "signals"])

    sent = []
    monkeypatch.setattr(notify, "deliver", sent.append)
    r = c.post("/email-alerts/test")
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert "buys and sells, dip signals" in sent[0].get_body(("plain",)).get_content()

    def refused(msg):
        raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    monkeypatch.setattr(notify, "deliver", refused)
    r = c.post("/email-alerts/test")
    assert r.status_code == 502 and "Username and Password not accepted" in r.text
    assert [(n["kind"], n["status"]) for n in c.get("/email-alerts/history").json()] == [("test", "failed"), ("test", "sent")]


def test_a_test_email_needs_a_mail_server_and_an_address(api, monkeypatch):
    c = api
    r = c.post("/email-alerts/test")
    assert r.status_code == 422 and "save an address" in r.text
    monkeypatch.setattr(notify, "settings", settings)
    r = c.post("/email-alerts/test")
    assert r.status_code == 422 and "SMTP_HOST" in r.text
