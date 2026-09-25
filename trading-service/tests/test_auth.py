"""Sign-in: sign-ups wait for an admin, session cookies, the nginx check, and managing accounts."""

import logging
from datetime import timedelta

import jwt
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import auth, main, manage
from app.db import utcnow

ADMIN_PW = "admin-pass-123"


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    """Real Argon2id, but cheap settings: the default ones are slow on purpose, which only slows tests down."""
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
    auth.throttle.reset()
    yield
    auth.throttle.reset()


def browser() -> TestClient:
    """A client with its own cookie jar, like a separate browser."""
    return TestClient(main.app)


@pytest.fixture
def admin() -> TestClient:
    manage.create_admin("boss", "The Boss", ADMIN_PW)
    c = browser()
    assert login(c, "boss", ADMIN_PW).status_code == 200
    return c


def signup(client, username, password="wonderland", name="Test User"):
    return client.post("/auth/signup", json={"username": username, "name": name, "password": password})


def login(client, username, password="wonderland", remember=True):
    return client.post("/auth/login", json={"username": username, "password": password, "remember": remember})


def approved(admin, username="alice", password="wonderland") -> TestClient:
    """A user who signed up, was approved, and is signed in, in a browser of their own."""
    c = browser()
    user_id = signup(c, username, password).json()["id"]
    assert admin.patch(f"/auth/users/{user_id}", json={"status": "active"}).status_code == 200
    assert login(c, username, password).status_code == 200
    return c


def token(user_id=1, tv=0, remember=True, issued=timedelta(0), lasts=timedelta(days=30), secret=None, alg="HS256"):
    """A hand-made session token: `issued` ago, expiring `lasts` after that."""
    iat = utcnow() - issued
    return jwt.encode({"sub": str(user_id), "tv": tv, "rem": remember, "iat": iat, "exp": iat + lasts},
                      secret or auth._secret(), algorithm=alg)


def with_cookie(value: str) -> dict:
    return {"Cookie": f"{auth.COOKIE}={value}"}


# --- Sign-up and approval ---

def test_a_sign_up_can_sign_in_only_after_an_admin_approves_it(admin):
    alice = browser()
    r = signup(alice, "  Alice ", name=" Alice Liddell ")
    assert r.status_code == 201
    assert r.json() | {"id": 0, "created_at": 0} == {
        "id": 0, "username": "alice", "name": "Alice Liddell", "role": "user", "status": "pending",
        "created_at": 0, "approved_at": None, "approved_by": None, "last_login_at": None}
    r = login(alice, "alice")
    assert r.status_code == 403 and "waiting for an admin" in r.json()["detail"]
    assert auth.COOKIE not in alice.cookies

    users = admin.get("/auth/users").json()
    assert [u["username"] for u in users] == ["alice", "boss"]  # waiting for approval comes first
    r = admin.patch(f"/auth/users/{users[0]['id']}", json={"status": "active"})
    assert r.json()["status"] == "active" and r.json()["approved_by"] == "boss"

    r = login(alice, "ALICE")  # usernames ignore case
    assert r.status_code == 200 and r.json()["last_login_at"] is not None
    assert alice.get("/auth/me").json()["username"] == "alice"
    assert alice.get("/auth/check").status_code == 204


def test_wrong_password_and_unknown_user_get_the_same_answer(admin):
    for username in ("boss", "nobody"):
        r = login(browser(), username, "not-the-password")
        assert r.status_code == 401 and r.json()["detail"] == "wrong username or password"


def test_usernames_are_unique_ignoring_case():
    assert signup(browser(), "Bob").status_code == 201
    r = signup(browser(), "bob")
    assert r.status_code == 409 and "taken" in r.text


@pytest.mark.parametrize("bad", [
    {"password": "short"},  # 8 characters at least
    {"username": "ab"},  # 3 at least
    {"username": "has space"},
    {"username": "émile"},
    {"name": "   "},
])
def test_invalid_sign_ups_are_rejected(bad):
    body = {"username": "carol", "name": "Carol", "password": "long-enough", **bad}
    assert browser().post("/auth/signup", json=body).status_code == 422


def test_only_a_pending_sign_up_can_be_rejected_and_deleting_frees_the_name(admin):
    alice = approved(admin)
    alice_id = alice.get("/auth/me").json()["id"]
    assert admin.patch(f"/auth/users/{alice_id}", json={"status": "rejected"}).status_code == 409

    dave_id = signup(browser(), "dave").json()["id"]
    assert admin.patch(f"/auth/users/{dave_id}", json={"status": "rejected"}).json()["status"] == "rejected"
    r = login(browser(), "dave")
    assert r.status_code == 403 and "declined" in r.text
    assert signup(browser(), "dave").status_code == 409  # still taken while the record exists
    assert admin.delete(f"/auth/users/{dave_id}").status_code == 204
    assert signup(browser(), "dave").status_code == 201


# --- Sessions ---

def test_keep_me_signed_in_makes_a_lasting_cookie_the_page_cannot_read(admin):
    cookie = login(browser(), "boss", ADMIN_PW, remember=True).headers["set-cookie"].lower()
    assert "max-age=2592000" in cookie  # 30 days
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie

    cookie = login(browser(), "boss", ADMIN_PW, remember=False).headers["set-cookie"].lower()
    assert "max-age" not in cookie and "expires" not in cookie  # gone when the browser closes


def test_the_nginx_check_turns_away_missing_forged_and_expired_tokens(admin):
    admin_id = admin.get("/auth/me").json()["id"]
    anyone = browser()
    assert anyone.get("/auth/check").status_code == 401
    assert anyone.get("/auth/check", headers=with_cookie(token(admin_id))).status_code == 204
    assert anyone.get("/auth/check", headers=with_cookie(token(admin_id, secret="guessed-" * 5))).status_code == 401
    expired = token(admin_id, issued=timedelta(days=31))
    assert anyone.get("/auth/check", headers=with_cookie(expired)).status_code == 401
    unsigned = jwt.encode({"sub": str(admin_id), "tv": 0, "iat": utcnow(), "exp": utcnow() + timedelta(days=1)},
                          None, algorithm="none")
    assert anyone.get("/auth/check", headers=with_cookie(unsigned)).status_code == 401
    assert anyone.get("/auth/check", headers=with_cookie("not-a-jwt")).status_code == 401
    assert anyone.get("/auth/check", headers=with_cookie(token(999))).status_code == 401  # no such user


def test_every_visit_renews_the_session(admin):
    admin_id = admin.get("/auth/me").json()["id"]
    old = token(admin_id, issued=timedelta(days=29))  # one day left
    r = browser().get("/auth/me", headers=with_cookie(old))
    assert r.status_code == 200
    renewed = r.cookies[auth.COOKIE]
    claims = jwt.decode(renewed, auth._secret(), algorithms=["HS256"])
    assert claims["exp"] - utcnow().timestamp() > timedelta(days=29).total_seconds()
    assert claims["rem"] is True


def test_sign_out_clears_the_cookie(admin):
    assert admin.post("/auth/logout").status_code == 204
    assert admin.get("/auth/me").status_code == 401


def test_disabling_someone_signs_them_out_at_once(admin):
    alice = approved(admin)
    alice_id = alice.get("/auth/me").json()["id"]
    old_cookie = alice.cookies[auth.COOKIE]
    assert admin.patch(f"/auth/users/{alice_id}", json={"status": "disabled"}).status_code == 200
    assert alice.get("/auth/check").status_code == 401
    r = login(browser(), "alice")
    assert r.status_code == 403 and "disabled" in r.text

    assert admin.patch(f"/auth/users/{alice_id}", json={"status": "active"}).status_code == 200
    assert browser().get("/auth/check", headers=with_cookie(old_cookie)).status_code == 401  # stays dead
    assert login(browser(), "alice").status_code == 200


def test_changing_the_password_signs_out_every_other_browser(admin):
    laptop = approved(admin)
    phone = browser()
    assert login(phone, "alice").status_code == 200

    r = laptop.post("/auth/password", json={"current_password": "wrong", "new_password": "new-password"})
    assert r.status_code == 400  # not 401: that would sign the laptop out
    r = laptop.post("/auth/password", json={"current_password": "wonderland", "new_password": "new-password"})
    assert r.status_code == 204
    assert laptop.get("/auth/check").status_code == 204  # this browser got a fresh token
    assert phone.get("/auth/check").status_code == 401
    assert login(browser(), "alice").status_code == 401
    assert login(browser(), "alice", "new-password").status_code == 200


def test_repeated_wrong_passwords_are_throttled(admin):
    for _ in range(5):
        assert login(browser(), "boss", "guess").status_code == 401
    r = login(browser(), "boss", ADMIN_PW)  # even the right password waits
    assert r.status_code == 429
    assert login(browser(), "boss", ADMIN_PW, remember=False).status_code == 429
    signup(browser(), "erin")
    assert login(browser(), "erin").status_code == 403  # other usernames aren't affected


def test_throttle_forgets_failures_after_the_window():
    now = {"t": 0.0}
    t = auth.LoginThrottle(limit=2, window=60, clock=lambda: now["t"])
    key = ("1.2.3.4", "boss")
    t.failed(key)
    t.failed(key)
    assert t.blocked(key) and not t.blocked(("5.6.7.8", "boss"))
    now["t"] = 61
    assert not t.blocked(key)
    t.failed(key)
    t.failed(key)
    t.succeeded(key)
    assert not t.blocked(key)


# --- Admin ---

def test_only_admins_see_and_change_accounts(admin):
    alice = approved(admin)
    bob_id = signup(browser(), "bob").json()["id"]
    for client, code in ((browser(), 401), (alice, 403)):
        assert client.get("/auth/users").status_code == code
        assert client.patch(f"/auth/users/{bob_id}", json={"status": "active"}).status_code == code
        assert client.delete(f"/auth/users/{bob_id}").status_code == code


def test_a_role_change_works_without_signing_in_again(admin):
    alice = approved(admin)
    alice_id = alice.get("/auth/me").json()["id"]
    assert admin.patch(f"/auth/users/{alice_id}", json={"role": "admin"}).status_code == 200
    assert alice.get("/auth/users").status_code == 200
    assert admin.patch(f"/auth/users/{alice_id}", json={"role": "user"}).status_code == 200
    assert alice.get("/auth/users").status_code == 403


def test_admins_cannot_lock_themselves_out(admin):
    me = admin.get("/auth/me").json()["id"]
    assert admin.patch(f"/auth/users/{me}", json={"status": "disabled"}).status_code == 409
    assert admin.patch(f"/auth/users/{me}", json={"role": "user"}).status_code == 409
    assert admin.delete(f"/auth/users/{me}").status_code == 409
    assert admin.patch("/auth/users/999", json={"status": "active"}).status_code == 404


# --- Command line (app.manage) ---

def test_command_line_creates_the_admin_and_resets_passwords(capsys):
    manage.create_admin("Root", "Root", "first-password")
    with pytest.raises(ValueError, match="already exists"):
        manage.create_admin("root", "Root", "first-password")
    with pytest.raises(ValidationError):
        manage.create_admin("shorty", "Shorty", "short")

    c = browser()
    assert login(c, "root", "first-password").json()["role"] == "admin"
    manage.set_password("root", "second-password")
    assert c.get("/auth/check").status_code == 401  # signed out everywhere
    assert login(browser(), "root", "second-password").status_code == 200
    with pytest.raises(ValueError, match="no user"):
        manage.set_password("ghost", "whatever-password")

    manage.main(["list-users"])
    assert "root" in capsys.readouterr().out


def test_startup_warns_when_nobody_can_approve_sign_ups(caplog):
    with caplog.at_level(logging.WARNING, logger="trading-service"):
        auth.startup_checks()
    assert "no admin account yet" in caplog.text
    caplog.clear()
    manage.create_admin("boss", "Boss", ADMIN_PW)
    with caplog.at_level(logging.WARNING, logger="trading-service"):
        auth.startup_checks()
    assert "no admin account" not in caplog.text
