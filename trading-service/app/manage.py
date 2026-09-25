"""Account chores the dashboard can't do for itself: creating the first admin, and resetting a password.

    docker compose exec trading-service python -m app.manage create-admin <username> [--name "Your Name"]
    docker compose exec trading-service python -m app.manage set-password <username>
    docker compose exec trading-service python -m app.manage list-users

The password is asked for twice and never shown. Piped input also works (one line), e.g. from a script.
"""

import argparse
import getpass
import sys

from pydantic import ValidationError
from sqlalchemy import select

from .auth import hash_password
from .db import SessionLocal, init_db, utcnow
from .models import User
from .schemas import SignupIn


def _read_password() -> str:
    if sys.stdin.isatty():
        password = getpass.getpass("Password: ")
        if getpass.getpass("Same password again: ") != password:
            sys.exit("the passwords don't match")
    else:
        password = sys.stdin.readline().rstrip("\n")
    return password


def create_admin(username: str, name: str, password: str) -> User:
    """A new, already approved admin account. Validated like a sign-up (username format, 8+ characters)."""
    body = SignupIn(username=username, name=name, password=password)
    with SessionLocal() as session:
        if session.scalar(select(User.id).where(User.username == body.username)) is not None:
            raise ValueError(f"{body.username} already exists (make it an admin on the Users page, or use set-password)")
        user = User(username=body.username, name=body.name, password_hash=hash_password(body.password),
                    role="admin", status="active", approved_at=utcnow(), approved_by="command line")
        session.add(user)
        session.commit()
        return user


def set_password(username: str, password: str) -> None:
    """Reset someone's password (e.g. a forgotten admin password). Signs them out of every browser."""
    SignupIn(username=username, name="-", password=password)  # same rules as signing up
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.username == username.strip().lower()))
        if user is None:
            raise ValueError(f"no user named {username}")
        user.password_hash = hash_password(password)
        user.token_version += 1
        session.commit()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.manage", description="Manage dashboard accounts")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-admin", help="create an approved admin account")
    p.add_argument("username")
    p.add_argument("--name", default="Admin", help="display name (default: Admin)")
    p = sub.add_parser("set-password", help="reset a password; signs that user out everywhere")
    p.add_argument("username")
    sub.add_parser("list-users", help="list every account")
    args = parser.parse_args(argv)

    init_db()  # the users table exists even if the service has never started on this database
    try:
        if args.command == "create-admin":
            user = create_admin(args.username, args.name, _read_password())
            print(f"Created admin {user.username}. Sign in at the dashboard.")
        elif args.command == "set-password":
            set_password(args.username, _read_password())
            print(f"Password changed; {args.username} is signed out of every browser.")
        else:
            with SessionLocal() as session:
                for u in session.scalars(select(User).order_by(User.id)):
                    print(f"{u.id:>4}  {u.username:<32} {u.role:<6} {u.status:<9} {u.name}")
    except ValidationError as e:
        sys.exit("; ".join(f"{err['loc'][-1]}: {err['msg']}" for err in e.errors()))
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
