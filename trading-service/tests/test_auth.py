"""Sign-in: sign-ups wait for an admin, session cookies, the nginx check, and managing accounts."""

import logging
import threading
from datetime import timedelta

import jwt
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import auth, main, manage
from app.db import SessionLocal, utcnow
from app.models import User

ADMIN_PW = "admin-pass-123"


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    """Real Argon2id, but cheap settings: the default ones are slow on purpose, which only slows tests down."""
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
    auth.login_attempts.reset()
    auth.signups.reset()
    yield
    auth.login_attempts.reset()
    auth.signups.reset()


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
    # "jo" and "has space" could never be sign-up names; signing in doesn't care, it's just a wrong username
    for username in ("boss", "nobody", "jo", "has space"):
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


def test_signing_in_clears_earlier_failures(admin):
    for _ in range(4):
        login(browser(), "boss", "guess")
    assert login(browser(), "boss", ADMIN_PW).status_code == 200
    for _ in range(4):  # a fresh allowance, not 1 left
        assert login(browser(), "boss", "guess").status_code == 401


def test_throttle_counts_parallel_attempts_before_any_finishes():
    """The check and the count are one step: 40 guesses arriving together get exactly `limit` through."""
    t = auth.Throttle(limit=5, window=60)
    start = threading.Barrier(40)
    allowed = []

    def guess():
        start.wait()
        allowed.append(t.attempt(("1.2.3.4", "boss")))

    threads = [threading.Thread(target=guess) for _ in range(40)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert allowed.count(True) == 5


def test_throttle_forgets_attempts_after_the_window():
    now = {"t": 0.0}
    t = auth.Throttle(limit=2, window=60, clock=lambda: now["t"])
    key = ("1.2.3.4", "boss")
    assert t.attempt(key) and t.attempt(key)
    assert not t.attempt(key) and t.attempt(("5.6.7.8", "boss"))
    now["t"] = 61
    assert t.attempt(key)
    t.forget(key)
    assert t.attempt(key) and t.attempt(key)


def test_throttle_drops_keys_that_have_aged_out():
    now = {"t": 0.0}
    t = auth.Throttle(limit=5, window=60, clock=lambda: now["t"])
    for i in range(100):
        t.attempt(("1.2.3.4", f"user{i}"))
    now["t"] = 120  # all aged out, and a minute since the last clean-up
    t.attempt(("1.2.3.4", "new"))
    assert list(t._hits) == [("1.2.3.4", "new")]


def test_sign_ups_are_limited_per_address():
    for i in range(10):
        assert signup(browser(), f"user{i}").status_code == 201
    r = signup(browser(), "user10")
    assert r.status_code == 429 and "too many sign-ups" in r.text


def test_password_hashing_waits_its_turn_then_gives_up(admin, monkeypatch):
    """At most 2 hashes run at once; a sign-in that can't get a turn in time is told to retry (503)."""
    monkeypatch.setattr(auth, "HASH_WAIT_SECONDS", 0.05)
    assert auth._hash_slots.acquire() and auth._hash_slots.acquire()  # both slots busy
    try:
        r = login(browser(), "boss", ADMIN_PW)
        assert r.status_code == 503 and "try again" in r.text
    finally:
        auth._hash_slots.release()
        auth._hash_slots.release()
    assert login(browser(), "boss", ADMIN_PW).status_code == 200


def test_sign_in_upgrades_a_password_hashed_with_older_settings(admin):
    old = PasswordHasher(time_cost=1, memory_cost=2048, parallelism=2).hash(ADMIN_PW)
    with SessionLocal() as s:
        s.get(User, 1).password_hash = old
        s.commit()
    assert login(browser(), "boss", ADMIN_PW).status_code == 200
    with SessionLocal() as s:
        new = s.get(User, 1).password_hash
    assert new != old and not auth._hasher.check_needs_rehash(new)
    assert login(browser(), "boss", ADMIN_PW).status_code == 200


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


def test_an_admin_demoted_mid_request_can_no_longer_change_others(admin):
    """Two admins demoting each other at once: the second must fail, or no admin would be left.
    Simulated by demoting the caller between its sign-in check and the change itself."""
    boss = admin.get("/auth/me").json()
    other = approved(admin, "carol")
    carol_id = other.get("/auth/me").json()["id"]
    assert admin.patch(f"/auth/users/{carol_id}", json={"role": "admin"}).status_code == 200

    real = auth.admin_user

    def demoted_right_after_the_check(user=auth.Depends(auth.current_user)):
        real(user)
        with SessionLocal() as s:  # carol's request lands first and demotes boss
            s.get(User, boss["id"]).role = "user"
            s.commit()
        return user

    main.app.dependency_overrides[auth.admin_user] = demoted_right_after_the_check
    try:
        assert admin.patch(f"/auth/users/{carol_id}", json={"role": "user"}).status_code == 403
        assert admin.delete(f"/auth/users/{carol_id}").status_code == 403
    finally:
        main.app.dependency_overrides.clear()
    with SessionLocal() as s:
        assert s.get(User, carol_id).role == "admin"  # carol is still an admin: one is always left


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
