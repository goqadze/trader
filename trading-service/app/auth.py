"""Sign-in: user accounts, session tokens, and an admin approving each new account.

How it fits together:
  - Anyone can sign up. The account stays "pending" until an admin approves it (see models.User).
  - Signing in returns a JWT (a token signed with JWT_SECRET) in an HttpOnly cookie. Page scripts can't read
    that cookie, so an injected script can't steal the session; the browser sends it with each request by
    itself. "Keep me signed in" makes it last SESSION_DAYS, renewed on every visit. Without it, the cookie
    dies with the browser (and the token after 12 hours at most).
  - nginx asks GET /auth/check before it passes any API call to trading-service or backtest-service
    (auth_request in frontend/nginx.conf). Every service is behind the sign-in without knowing about it.
  - Each check also reads the user's row, so disabling someone, or changing a password, works on the very
    next request instead of when the token expires. token_version is what makes old tokens stop working.

The first admin is created from the command line (see manage.py):
    docker compose exec trading-service python -m app.manage create-admin <username>
"""

import logging
import secrets
import threading
import time
from collections import deque
from datetime import timedelta
from functools import cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, get_session, utcnow
from .models import User
from .schemas import LoginIn, PasswordChange, SignupIn, UserOut, UserUpdate

logger = logging.getLogger("trading-service")

COOKIE = "trading_session"
ALGORITHM = "HS256"
SHORT_SESSION = timedelta(hours=12)  # without "Keep me signed in"

# Why someone with the right password still can't sign in
NOT_ACTIVE = {
    "pending": "your account is waiting for an admin to approve it",
    "rejected": "an admin declined this account",
    "disabled": "this account is disabled; ask an admin",
}

_hasher = PasswordHasher()  # Argon2id with the library's recommended cost settings
_fallback_secret = secrets.token_hex(32)  # only if JWT_SECRET is missing: sessions then end at every restart


def _secret() -> str:
    return settings.jwt_secret or _fallback_secret


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@cache
def _dummy_hash() -> str:
    """Checked against when the username doesn't exist, so a wrong username takes as long as a wrong
    password and the response time doesn't reveal which usernames are real."""
    return _hasher.hash(secrets.token_hex(16))


class LoginThrottle:
    """Slows down password guessing: after `limit` failed sign-ins for one username from one address within
    `window` seconds, more tries are refused until the oldest failure ages out. Keyed by address AND username,
    so someone guessing can't lock the real user out. In memory: fine for this single-process service."""

    def __init__(self, limit: int = 5, window: float = 15 * 60, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._fails: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: tuple[str, str], now: float) -> deque[float]:
        fails = self._fails.get(key, deque())
        while fails and fails[0] <= now - self.window:
            fails.popleft()
        return fails

    def blocked(self, key: tuple[str, str]) -> bool:
        with self._lock:
            return len(self._recent(key, self.clock())) >= self.limit

    def failed(self, key: tuple[str, str]) -> None:
        with self._lock:
            now = self.clock()
            self._fails[key] = self._recent(key, now)
            self._fails[key].append(now)
            if len(self._fails) > 10_000:  # don't grow forever: drop everything that has aged out
                self._fails = {k: q for k, q in self._fails.items() if self._recent(k, now)}

    def succeeded(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()


throttle = LoginThrottle()


def _client_ip(request: Request) -> str:
    # nginx sets X-Real-IP; without it (a direct call on localhost:8002) use the socket's address
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------

def issue_session(response: Response, user: User, remember: bool) -> None:
    """Sign the browser in: a JWT naming the user, in an HttpOnly cookie."""
    lifetime = timedelta(days=settings.session_days) if remember else SHORT_SESSION
    now = utcnow()
    token = jwt.encode({"sub": str(user.id), "tv": user.token_version, "rem": remember, "iat": now,
                        "exp": now + lifetime}, _secret(), algorithm=ALGORITHM)
    response.set_cookie(
        COOKIE, token,
        max_age=int(lifetime.total_seconds()) if remember else None,  # None = gone when the browser closes
        httponly=True,  # invisible to JavaScript
        samesite="strict",  # never sent by requests other sites trigger, so they can't act as you (CSRF)
        secure=settings.cookie_secure,
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict", secure=settings.cookie_secure)


def read_session(session: Session, token: str | None) -> tuple[User, dict] | None:
    """The user behind a session cookie and the token's claims, or None if it isn't (or no longer) valid."""
    if not token:
        return None
    try:
        claims = jwt.decode(token, _secret(), algorithms=[ALGORITHM], options={"require": ["sub", "exp", "iat"]})
        user = session.get(User, int(claims["sub"]))
    except (jwt.InvalidTokenError, ValueError):
        return None
    if user is None or user.status != "active" or user.token_version != claims.get("tv"):
        return None
    return user, claims


def _signed_in(request: Request, session: Session) -> tuple[User, dict]:
    found = read_session(session, request.cookies.get(COOKIE))
    if found is None:
        raise HTTPException(401, "not signed in")
    return found


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    return _signed_in(request, session)[0]


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "admins only")
    return user


def startup_checks() -> None:
    """Warn in the log about a setup nobody can use."""
    if not settings.jwt_secret:
        logger.warning("auth: JWT_SECRET is not set in trading-service/.env, so everyone is signed out at every restart")
    with SessionLocal() as session:
        if session.scalar(select(User.id).where(User.role == "admin", User.status == "active").limit(1)) is None:
            logger.warning("auth: no admin account yet, so nobody can sign in or approve sign-ups. Create one with: "
                           "docker compose exec trading-service python -m app.manage create-admin <username>")


# ---------------------------------------------------------------------------
# API: /auth/*  (the dashboard reaches it at /api/auth/*, see frontend/nginx.conf)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=UserOut, status_code=201)
def signup(body: SignupIn, session: Session = Depends(get_session)):
    """Ask for an account. It can sign in once an admin approves it."""
    if session.scalar(select(User.id).where(User.username == body.username)) is not None:
        raise HTTPException(409, "that username is taken")
    user = User(username=body.username, name=body.name, password_hash=hash_password(body.password),
                role="user", status="pending")
    session.add(user)
    try:
        session.commit()
    except IntegrityError:  # two sign-ups raced for the same name
        session.rollback()
        raise HTTPException(409, "that username is taken")
    logger.info("auth: %s signed up and is waiting for approval", user.username)
    return user


@router.post("/login", response_model=UserOut)
def login(body: LoginIn, request: Request, response: Response, session: Session = Depends(get_session)):
    key = (_client_ip(request), body.username)
    if throttle.blocked(key):
        raise HTTPException(429, "too many failed sign-ins; wait 15 minutes and try again")
    user = session.scalar(select(User).where(User.username == body.username))
    password_ok = verify_password(user.password_hash if user else _dummy_hash(), body.password)
    if user is None or not password_ok:
        throttle.failed(key)
        raise HTTPException(401, "wrong username or password")
    throttle.succeeded(key)
    if user.status != "active":  # only said to someone who knows the password
        raise HTTPException(403, NOT_ACTIVE[user.status])
    user.last_login_at = utcnow()
    session.commit()
    issue_session(response, user, body.remember)
    return user


@router.post("/logout", status_code=204)
def logout(response: Response):
    clear_session(response)


@router.get("/me", response_model=UserOut)
def me(request: Request, response: Response, session: Session = Depends(get_session)):
    """Who is signed in. The dashboard asks on every load, which also renews the session: someone who
    opens it at least once every SESSION_DAYS never has to sign in again."""
    user, claims = _signed_in(request, session)
    issue_session(response, user, bool(claims.get("rem")))
    return user


@router.get("/check", status_code=204)
def check(_user: User = Depends(current_user)):
    """nginx's auth_request: 204 lets an API call through, 401 stops it."""


@router.post("/password", status_code=204)
def change_password(body: PasswordChange, request: Request, response: Response,
                    session: Session = Depends(get_session)):
    """Change your own password. Signs out every other browser; this one stays signed in."""
    user, claims = _signed_in(request, session)
    if not verify_password(user.password_hash, body.current_password):
        # 400, not 401: the session is fine, and a 401 would make the dashboard sign you out
        raise HTTPException(400, "current password is wrong")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1
    session.commit()
    issue_session(response, user, bool(claims.get("rem")))


# --- Admin: approve and manage accounts ---

def _get_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(_admin: User = Depends(admin_user), session: Session = Depends(get_session)):
    """Every account, sign-ups waiting for approval first."""
    waiting_first = case((User.status == "pending", 0), else_=1)
    return list(session.scalars(select(User).order_by(waiting_first, User.created_at.desc())))


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserUpdate, admin: User = Depends(admin_user),
                session: Session = Depends(get_session)):
    """Approve, reject, disable, re-enable, or change someone's role."""
    user = _get_user(session, user_id)
    if user.id == admin.id:  # also guarantees there is always an active admin left
        raise HTTPException(409, "you can't change your own account")
    status, role = body.status, body.role
    if status and status != user.status:
        if status == "rejected" and user.status != "pending":
            raise HTTPException(409, "only a pending sign-up can be rejected; disable the account instead")
        if status == "active" and user.approved_at is None:
            user.approved_at, user.approved_by = utcnow(), admin.username
        if status != "active":
            # Signed out now, and a token from before stays invalid even if the account is re-enabled later
            user.token_version += 1
        user.status = status
    if role:
        user.role = role
    session.commit()
    logger.info("auth: %s set %s to %s", admin.username, user.username, body.model_dump(exclude_none=True))
    return user


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, admin: User = Depends(admin_user), session: Session = Depends(get_session)):
    """Remove an account for good (frees the username). Rejecting or disabling keeps a record instead."""
    user = _get_user(session, user_id)
    if user.id == admin.id:
        raise HTTPException(409, "you can't delete your own account")
    session.delete(user)
    session.commit()
    logger.info("auth: %s deleted the account %s", admin.username, user.username)
