"""Database engine + session factory. Every table lives in models.py.

SQLite is the default: one file on a Docker volume, zero setup, survives restarts. Point DATABASE_URL at
Postgres when you outgrow it -- the ORM code doesn't change.
"""

from collections.abc import Iterator
from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """All timestamps are stored in UTC; the UI converts to local / New York time for display."""
    return datetime.now(timezone.utc)


def make_engine(url: str):
    if url.startswith("sqlite"):
        # check_same_thread=False: the scheduler and API threads share the engine (each uses its own session)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(conn, _record):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer (UI polls while bots trade)
            cur.execute("PRAGMA foreign_keys=ON")  # SQLite ignores FOREIGN KEY constraints unless told otherwise
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Create any missing tables. Fine while the schema only grows; adopt Alembic migrations once
    you need to rename or change existing columns on a database that already holds history."""
    from . import models  # noqa: F401  (registers the tables on Base.metadata)

    Base.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    with SessionLocal() as session:
        yield session
