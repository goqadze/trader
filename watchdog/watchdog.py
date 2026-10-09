"""Watchdog: emails you when a service of this stack is down, and again when it's back. Also when the bots' scheduler
stops (trading-service's /health), the disk is nearly full, or the nightly backup didn't run.

A container of its own on purpose: trading-service sends every other alert (trades, stops, the bots' errors), so it
can't be the one to say it's down. Python's standard library only, so there's little in it that can break.

Every CHECK_EVERY seconds it asks each service the server runs (by COMPOSE_PROFILES) whether it's fine. A service is
down after FAIL_AFTER failed checks in a row: a restart during a deploy takes seconds, not minutes. One email lists
what went down; another says when it's back and for how long it was out; a reminder goes out every REMIND_EVERY while
it stays down. An email that can't be sent is tried again at the next round.

Who gets it: the addresses on the dashboard's Email alerts page while alerts are on (switched off there = no email
from here either). They're read from trading-service at every round and remembered in /data, so they're still known
while trading-service is down; WATCHDOG_TO before it ever answered. Sent through the mail server the other alerts use
(SMTP_* from trading-service/.env).

The whole server down (power, network, Docker) can't be reported from inside it. With WATCHDOG_PING_URL (a free dead
man's switch such as healthchecks.io) it pings out after every round, and that service emails you when the pings stop.

    python watchdog.py           run for ever
    python watchdog.py --once    one round, printing what it found (nothing is emailed)
"""

import json
import logging
import os
import smtplib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("watchdog")
NY = ZoneInfo("America/New_York")

CHECK_EVERY = int(os.getenv("WATCHDOG_CHECK_SECONDS", "60"))
FAIL_AFTER = int(os.getenv("WATCHDOG_FAIL_AFTER", "3"))  # failed checks in a row: ~3 minutes at one a minute
REMIND_EVERY = timedelta(hours=float(os.getenv("WATCHDOG_REMIND_HOURS", "6")))
DISK_MIN_FREE = float(os.getenv("WATCHDOG_DISK_MIN_FREE_PCT", "10")) / 100
BACKUP_MAX_AGE = timedelta(hours=float(os.getenv("WATCHDOG_BACKUP_MAX_HOURS", "36")))  # nightly, plus slack
DATA = Path(os.getenv("WATCHDOG_DATA", "/data"))  # the remembered addresses
BACKUPS = Path(os.getenv("WATCHDOG_BACKUPS", "/backups"))  # ./backups, read-only; not mounted = no backup check
TRADING = os.getenv("WATCHDOG_TRADING_URL", "http://trading-service:8000")
OUTBOX_MAX = 20  # emails kept for another try while the mail server fails


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The checks: each answers None (fine) or what's wrong, in words
# ---------------------------------------------------------------------------

Probe = Callable[[], str | None]


@dataclass
class Check:
    name: str
    stops: str  # what doesn't work while it's down, for the email
    probe: Probe


def _why(e: BaseException) -> str:
    reason = getattr(e, "reason", e)
    if isinstance(reason, socket.gaierror):
        return "not running (its name doesn't resolve: the container is stopped or gone)"
    if isinstance(reason, ConnectionRefusedError):
        return "not answering (connection refused: starting, or crashed)"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "no answer within 10 seconds (stuck or overloaded)"
    return f"{type(reason).__name__}: {reason}"


def http(url: str) -> Probe:
    """Up = any answer under 500 (a 404 still means it's running). A 503 from a /health that lists its problems
    passes them on."""
    def probe() -> str | None:
        try:
            with urllib.request.urlopen(url, timeout=10):
                return None
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return None
            try:
                problems = json.loads(e.read() or b"{}").get("problems") or []
            except (ValueError, AttributeError):
                problems = []
            return "; ".join(problems) or f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001  (URLError, timeouts, resets: all mean "no proper answer")
            return _why(e)
    return probe


def tcp(host: str, port: int) -> Probe:
    """A database: it accepts connections."""
    def probe() -> str | None:
        try:
            with socket.create_connection((host, port), timeout=10):
                return None
        except OSError as e:
            return _why(e)
    return probe


def disk(path: str = "/", min_free: float = DISK_MIN_FREE) -> Probe:
    """The server's disk (a container's / is on it)."""
    def probe() -> str | None:
        st = os.statvfs(path)
        free = st.f_bavail / st.f_blocks
        if free >= min_free:
            return None
        return (f"only {free:.0%} free ({st.f_bavail * st.f_frsize / 1e9:.1f} GB): `docker image prune` clears old "
                "images, `df -h` and `du -sh ~/trading/backups` show what's big")
    return probe


def backups(folder: Path, max_age: timedelta = BACKUP_MAX_AGE, now: Callable[[], datetime] = utcnow) -> Probe:
    def probe() -> str | None:
        files = sorted(folder.glob("trading-*.sql.gz"), key=lambda f: f.stat().st_mtime)
        if not files:
            return "there's no backup in backups/: is the nightly cron job set up? (DEPLOY.md, step 9)"
        newest = files[-1]
        age = now() - datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc)
        if age <= max_age:
            return None
        return f"the newest, {newest.name}, is {age.total_seconds() / 3600:.0f} hours old: look at backups/backup.log"
    return probe


def checks_for(profiles: set[str], backups_dir: Path = BACKUPS) -> list[Check]:
    """What this server runs: the bots always; the rest by docker-compose.server.yml's profiles."""
    def on(*names: str) -> bool:
        return bool(profiles & {*names, "full"})

    out = [
        Check("trading-service", "the bots: no checks, no trades, and no other email alerts", http(f"{TRADING}/health")),
        Check("trading-db", "the bots' database: they can't check or trade", tcp("trading-db", 5432)),
        Check("dashboard", "the dashboard (you can't see or control the bots; they keep trading)", http("http://frontend/")),
    ]
    if on("news", "backtest"):
        out += [Check("decision-service", "the dip buyer's News switch and the signal bots' decisions",
                      http("http://decision-service:8000/health")),
                Check("news-db", "the news store behind decision-service", tcp("news-db", 5432))]
    if on("backtest"):
        out.append(Check("backtest-service", "the backtest pages", http("http://backtest-service:8000/health")))
    if on("monitoring"):
        out += [Check("glitchtip", "error tracking (GlitchTip)", http("http://glitchtip-web:8080/_health/")),
                Check("langfuse", "LLM tracing (Langfuse)", http("http://langfuse:3000/api/public/health"))]
    if on("pgadmin"):
        out.append(Check("pgadmin", "pgAdmin", http("http://pgadmin/misc/ping")))
    if "public" in profiles:
        out.append(Check("caddy", "the dashboard's public address (HTTPS)", tcp("caddy", 443)))
    out.append(Check("disk", "the server's disk: when it's full the databases stop", disk()))
    if backups_dir.is_dir():
        out.append(Check("backups", "the nightly database backup", backups(backups_dir)))
    return out


# ---------------------------------------------------------------------------
# Who gets the emails, and sending them
# ---------------------------------------------------------------------------

def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


class Contacts:
    """The Email alerts page's addresses (none while alerts are off) and dashboard link, remembered in `folder`."""

    def __init__(self, folder: Path = DATA, fetch: Callable[[str], dict] = fetch_json,
                 fallback: str = os.getenv("WATCHDOG_TO", "")):
        self.file = folder / "contacts.json"
        self.fetch = fetch
        self.fallback = [a.strip() for a in fallback.split(",") if a.strip()]
        self.dashboard = os.getenv("DASHBOARD_URL", "").rstrip("/")

    def refresh(self) -> None:
        """Ask trading-service (while it's up) and remember the answer."""
        try:
            cfg = self.fetch(f"{TRADING}/email-alerts")
        except Exception as e:  # noqa: BLE001  down: the remembered ones stay
            log.debug("email alert settings unavailable: %s", e)
            return
        recipients = (cfg.get("recipients") or []) if cfg.get("enabled") else []  # alerts off: no email from here
        known = {"recipients": recipients, "dashboard_url": cfg.get("dashboard_url") or self.dashboard}
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(known))
        except OSError as e:
            log.warning("can't remember the addresses in %s: %s", self.file, e)

    def _known(self) -> dict | None:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return None

    def recipients(self) -> list[str]:
        known = self._known()
        return known["recipients"] if known is not None else self.fallback

    def dashboard_url(self) -> str:
        known = self._known()
        return (known or {}).get("dashboard_url") or self.dashboard


def smtp_send(msg: EmailMessage) -> None:
    """Through the mail server the other alerts use (trading-service/.env's SMTP_*, same meaning)."""
    host, port = os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587"))
    security = os.getenv("SMTP_SECURITY", "starttls").strip().lower()
    context = ssl.create_default_context()
    server = (smtplib.SMTP_SSL(host, port, timeout=30, context=context) if security == "ssl"
              else smtplib.SMTP(host, port, timeout=30))
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if os.getenv("SMTP_USERNAME"):
            server.login(os.environ["SMTP_USERNAME"], os.getenv("SMTP_PASSWORD", ""))
        server.send_message(msg)


def sender() -> str:
    return os.getenv("SMTP_FROM") or os.getenv("SMTP_USERNAME", "")


# ---------------------------------------------------------------------------
# The rounds
# ---------------------------------------------------------------------------

@dataclass
class Status:
    fails: int = 0
    since: datetime | None = None  # the first failed check of this outage
    error: str = ""
    alerted_at: datetime | None = None  # the last email about it (None: none yet, it isn't "down" to you)


def _at(t: datetime) -> str:
    return f"{t.astimezone(NY):%a %b %d %H:%M} New York"


def _for(d: timedelta) -> str:
    m = int(d.total_seconds() // 60)
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60} min"


class Watchdog:
    def __init__(self, checks: list[Check], contacts: Contacts, send: Callable[[EmailMessage], None] = smtp_send,
                 now: Callable[[], datetime] = utcnow, ping_url: str = os.getenv("WATCHDOG_PING_URL", ""),
                 ping: Callable[[str], object] = lambda url: urllib.request.urlopen(url, timeout=10).close()):
        self.checks, self.contacts, self.send, self.now = checks, contacts, send, now
        self.ping_url, self.ping = ping_url, ping
        self.status = {c.name: Status() for c in checks}
        self.outbox: list[tuple[str, str]] = []  # (subject, body) not sent yet

    def round(self) -> None:
        now = self.now()
        down, reminders, back = [], [], []
        for c in self.checks:
            try:
                error = c.probe()
            except Exception as e:  # noqa: BLE001  a check that breaks is itself worth knowing about
                error = f"the check itself failed: {type(e).__name__}: {e}"
            s = self.status[c.name]
            if error is None:
                if s.alerted_at is not None:
                    back.append((c, now - s.since))
                self.status[c.name] = Status()
                continue
            s.fails, s.error = s.fails + 1, error
            s.since = s.since or now
            if s.alerted_at is None and s.fails >= FAIL_AFTER:
                s.alerted_at = now
                down.append(c)
            elif s.alerted_at is not None and now - s.alerted_at >= REMIND_EVERY:
                s.alerted_at = now
                reminders.append(c)
        if self.status.get("trading-service", Status()).fails == 0:  # it answers: the addresses may have changed
            self.contacts.refresh()
        if down or reminders:
            self.outbox.append(self._down_email(down, reminders))
        if back:
            self.outbox.append(self._back_email(back))
        self.flush()
        if self.ping_url:
            try:
                self.ping(self.ping_url)
            except Exception as e:  # noqa: BLE001
                log.warning("ping %s failed: %s", self.ping_url, e)

    def _down_email(self, down: list[Check], reminders: list[Check]) -> tuple[str, str]:
        names = [c.name for c in down + reminders]
        subject = f"[Trading] {'DOWN' if down else 'Still down'}: {', '.join(names)}"
        lines = []
        for c in down + reminders:
            s = self.status[c.name]
            lines += [f"{c.name}: {'DOWN' if c in down else 'still down'} since {_at(s.since)} "
                      f"({_for(self.now() - s.since)}, {s.fails} failed checks in a row)",
                      f"  What stops: {c.stops}", f"  Problem: {s.error}", ""]
        others = [n for n, s in self.status.items() if s.alerted_at is not None and n not in names]
        lines.append(f"Also still down: {', '.join(others)}." if others else "Everything else is up.")
        return subject, "\n".join(lines + self._footer())

    def _back_email(self, back: list[tuple[Check, timedelta]]) -> tuple[str, str]:
        subject = "[Trading] Back up: " + ", ".join(f"{c.name} (down {_for(d)})" for c, d in back)
        lines = [f"{c.name} is back: it was down for {_for(d)}." for c, d in back] + [""]
        still = [n for n, s in self.status.items() if s.alerted_at is not None]
        lines.append(f"Still down: {', '.join(still)}." if still else "Everything is up.")
        return subject, "\n".join(lines + self._footer())

    def _footer(self) -> list[str]:
        url = self.contacts.dashboard_url()
        return ["", *([f"Dashboard: {url}"] if url else []),
                "On the server: cd ~/trading && docker compose ps, then docker compose logs --tail 100 <service>",
                f"-- the watchdog on {socket.gethostname()}, checking every {CHECK_EVERY} s"]

    def flush(self) -> None:
        """Send what's waiting, one email each; what fails waits for the next round."""
        if not self.outbox:
            return
        to = self.contacts.recipients()
        if not to:
            for subject, _ in self.outbox:
                log.warning("not emailed (no address, or email alerts are off on the dashboard): %s", subject)
            self.outbox.clear()
            return
        left = []
        for subject, body in self.outbox:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, sender(), ", ".join(to)
            msg.set_content(body)
            try:
                self.send(msg)
                log.info("emailed: %s", subject)
            except Exception as e:  # noqa: BLE001
                log.warning("email not sent, trying again next round: %s (%s: %s)", subject, type(e).__name__, e)
                left.append((subject, body))
        self.outbox = left[-OUTBOX_MAX:]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    profiles = {p.strip() for p in os.getenv("COMPOSE_PROFILES", "").split(",") if p.strip()}
    checks = checks_for(profiles)
    if "--once" in sys.argv:
        for c in checks:
            print(f"{c.name:18s} {c.probe() or 'ok'}")
        return
    contacts = Contacts()
    contacts.refresh()
    if not os.getenv("SMTP_HOST"):
        log.warning("no SMTP_HOST: problems are only logged, not emailed")
    log.info("watching %s every %ss; down after %s failed checks; emails to %d address(es)%s",
             ", ".join(c.name for c in checks), CHECK_EVERY, FAIL_AFTER, len(contacts.recipients()),
             "; pinging WATCHDOG_PING_URL" if os.getenv("WATCHDOG_PING_URL") else "")
    dog = Watchdog(checks, contacts, send=smtp_send if os.getenv("SMTP_HOST") else
                   lambda msg: log.warning("would email: %s", msg["Subject"]))
    while True:
        started = time.monotonic()
        try:
            dog.round()
        except Exception:  # noqa: BLE001  never die: the next round may work
            log.exception("round failed")
        time.sleep(max(1.0, CHECK_EVERY - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
