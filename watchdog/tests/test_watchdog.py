"""The watchdog: when a service counts as down, what is emailed and to whom, and what each check calls a problem."""

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

import watchdog as wd

T0 = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)  # Friday 09:00 New York
REFUSED = "not answering (connection refused: starting, or crashed)"


@pytest.fixture
def env(tmp_path):
    """Two services whose checks answer what `state` says (None = fine), a clock, and the emails sent."""
    state = {"trading-service": None, "dashboard": None}
    clock = {"t": T0}
    sent, pings = [], []
    alerts = {"enabled": True, "recipients": ["me@example.com"], "dashboard_url": "https://dash.example"}

    def fetch(url):
        assert url.endswith("/email-alerts")
        if state["trading-service"] is not None:
            raise OSError("connection refused")
        return alerts

    contacts = wd.Contacts(tmp_path, fetch=fetch, fallback="")
    checks = [wd.Check(n, f"what {n} does", lambda n=n: state[n]) for n in state]
    dog = wd.Watchdog(checks, contacts, send=sent.append, now=lambda: clock["t"], ping_url="https://ping.example/x",
                      ping=pings.append)

    def rounds(n=1):
        for _ in range(n):
            dog.round()
            clock["t"] += timedelta(minutes=1)

    return SimpleNamespace(state=state, clock=clock, sent=sent, pings=pings, alerts=alerts, dog=dog, rounds=rounds,
                           contacts=contacts)


def test_down_after_three_failed_checks_in_a_row_then_a_reminder_then_back(env):
    env.rounds()  # all fine
    env.state["dashboard"] = REFUSED
    env.rounds(2)
    assert env.sent == []  # a restart takes seconds: two misses aren't an outage
    env.rounds()
    [msg] = env.sent
    assert (msg["Subject"], msg["To"]) == ("[Trading] DOWN: dashboard", "me@example.com")
    body = msg.get_content()
    assert "dashboard: DOWN since Fri Oct 09 09:01 New York (2 min, 3 failed checks in a row)" in body
    assert f"  What stops: what dashboard does\n  Problem: {REFUSED}\n\nEverything else is up." in body
    assert "Dashboard: https://dash.example" in body

    env.rounds(30)
    assert len(env.sent) == 1  # one email, not one a minute
    env.clock["t"] += timedelta(hours=6)
    env.rounds()
    assert env.sent[-1]["Subject"] == "[Trading] Still down: dashboard"

    env.state["dashboard"] = None
    env.rounds()
    assert env.sent[-1]["Subject"] == "[Trading] Back up: dashboard (down 6 h 34 min)"
    assert "dashboard is back: it was down for 6 h 34 min.\n\nEverything is up." in env.sent[-1].get_content()
    assert len(env.pings) == 36  # the dead man's switch hears from it every round


def test_one_email_for_everything_that_went_down_together_and_the_addresses_are_remembered(env):
    env.rounds()  # trading-service answers: its addresses are remembered
    env.state["trading-service"] = "the scheduler isn't running: the bots don't check or trade"
    env.state["dashboard"] = REFUSED
    env.rounds(3)
    [msg] = env.sent
    assert msg["Subject"] == "[Trading] DOWN: trading-service, dashboard" and msg["To"] == "me@example.com"
    assert "Problem: the scheduler isn't running" in msg.get_content()
    env.state["dashboard"] = None
    env.rounds()
    assert env.sent[-1]["Subject"] == "[Trading] Back up: dashboard (down 3 min)"
    assert "Still down: trading-service." in env.sent[-1].get_content()


def test_no_email_while_alerts_are_off_and_watchdog_to_until_trading_service_ever_answered(env, tmp_path):
    env.alerts["enabled"] = False
    env.state["dashboard"] = REFUSED
    env.rounds(3)
    assert env.sent == []  # switched off on the dashboard: off here too

    never = wd.Contacts(tmp_path / "new", fetch=lambda url: (_ for _ in ()).throw(OSError("down")), fallback="a@x.com, b@x.com")
    never.refresh()
    assert never.recipients() == ["a@x.com", "b@x.com"]


def test_an_email_that_fails_is_sent_at_the_next_round(env):
    tries = []

    def flaky(msg):
        tries.append(msg["Subject"])
        if len(tries) == 1:
            raise OSError("mail server unreachable")
        env.sent.append(msg)

    env.dog.send = flaky
    env.rounds()
    env.state["dashboard"] = REFUSED
    env.rounds(3)
    assert env.sent == [] and env.dog.outbox
    env.rounds()
    assert [m["Subject"] for m in env.sent] == ["[Trading] DOWN: dashboard"] and env.dog.outbox == []


def test_a_check_that_breaks_counts_as_a_problem(env):
    def boom():
        raise ValueError("bad")

    env.dog.checks[1] = wd.Check("dashboard", "x", boom)
    env.rounds(3)
    assert "Problem: the check itself failed: ValueError: bad" in env.sent[0].get_content()


class _Handler(BaseHTTPRequestHandler):
    answers = {"/ok": (200, b"{}"), "/missing": (404, b""), "/sick": (503, json.dumps({"problems": ["db gone", "late"]}).encode()),
               "/broken": (500, b"oops")}

    def do_GET(self):
        code, body = self.answers[self.path]
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_http_checks_read_the_health_answers(server):
    assert wd.http(f"{server}/ok")() is None
    assert wd.http(f"{server}/missing")() is None  # it answered: it's running
    assert wd.http(f"{server}/sick")() == "db gone; late"
    assert wd.http(f"{server}/broken")() == "HTTP 500"
    assert wd.http("http://no-such-service.invalid/")().startswith("not running")
    port = server.rsplit(":", 1)[1]
    assert wd.tcp("127.0.0.1", int(port))() is None


def test_closed_ports_are_refused():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert wd.tcp("127.0.0.1", port)() == REFUSED
    assert wd.http(f"http://127.0.0.1:{port}/")() == REFUSED


def test_disk_and_backups(tmp_path):
    assert wd.disk(str(tmp_path), min_free=0)() is None
    assert wd.disk(str(tmp_path), min_free=1.01)().startswith("only ")
    assert "no backup" in wd.backups(tmp_path)()
    old, new = tmp_path / "trading-20261007-223000.sql.gz", tmp_path / "trading-20261008-223000.sql.gz"
    for f, hours in ((old, 60), (new, 40)):
        f.write_bytes(b"x")
        t = (T0 - timedelta(hours=hours)).timestamp()
        os.utime(f, (t, t))
    probe = wd.backups(tmp_path, max_age=timedelta(hours=36), now=lambda: T0)
    assert probe() == "the newest, trading-20261008-223000.sql.gz, is 40 hours old: look at backups/backup.log"
    os.utime(new, (T0.timestamp(), T0.timestamp()))
    assert probe() is None


def test_the_checks_follow_the_servers_profiles(tmp_path):
    names = lambda profiles: [c.name for c in wd.checks_for(profiles, backups_dir=tmp_path / "none")]
    assert names(set()) == ["trading-service", "trading-db", "dashboard", "disk"]
    assert names({"news"}) == ["trading-service", "trading-db", "dashboard", "decision-service", "news-db", "disk"]
    assert names({"full"}) == ["trading-service", "trading-db", "dashboard", "decision-service", "news-db",
                               "backtest-service", "glitchtip", "langfuse", "pgadmin", "disk"]
    assert names({"full", "public"})[-2:] == ["caddy", "disk"]
    assert [c.name for c in wd.checks_for(set(), backups_dir=tmp_path)][-1] == "backups"
