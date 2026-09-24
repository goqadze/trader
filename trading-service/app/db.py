"""Database engine + session factory. Every table lives in models.py.

Production runs on Postgres (the trading-db container): real concurrent writers, strict column types,
and standard backup tools (pg_dump). SQLite is still accepted -- handy for a quick throwaway run or
`pytest` without Docker -- because the ORM code is identical for both.
"""

import logging
from collections.abc import Iterator
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

logger = logging.getLogger("trading-service")


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
    return create_engine(
        url,
        pool_pre_ping=True,  # test each pooled connection first, so a Postgres restart doesn't break the next request
        # Up to 4 scheduler threads + concurrent API requests each hold a connection; the default
        # (5 + 10 overflow) could make a busy moment wait. Postgres allows 100 connections by default.
        pool_size=10,
        max_overflow=10,
        pool_timeout=30,
    )


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Create any missing tables and columns. Fine while the schema only grows; adopt Alembic migrations
    once you need to rename or change existing columns on a database that already holds history."""
    from . import models  # noqa: F401  (registers the tables on Base.metadata)

    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """create_all() creates missing TABLES but never changes existing ones, so a column added to a model
    later would be missing from a database that already has history. Add those columns here.
    Additive only: a new column must be nullable or have a server_default (existing rows need a value)."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}"
                if col.server_default is not None:
                    ddl += " DEFAULT " + _sql_literal(col.server_default.arg)
                    if not col.nullable:
                        ddl += " NOT NULL"
                conn.execute(text(ddl))
                logger.info("database: added column %s.%s", table.name, col.name)


def _sql_literal(arg) -> str:
    """A server_default as SQL: plain strings are quoted, text("now()") etc. are used as written."""
    return "'" + arg.replace("'", "''") + "'" if isinstance(arg, str) else str(arg.text)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    with SessionLocal() as session:
        yield session
