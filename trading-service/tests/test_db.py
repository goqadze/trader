"""Database behaviour that differs between SQLite and Postgres (production runs on Postgres)."""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import IS_POSTGRES, FakeBroker, at, make_bot, signal
from sqlalchemy import select, text

from app.db import SessionLocal, engine, init_db
from app.models import Decision, Event, Order
from app.trader import evaluate


def test_make_test_trading_really_runs_on_postgres():
    """Guards the Makefile: if TEST_DATABASE_URL is set we must not have silently fallen back to SQLite."""
    if os.getenv("TEST_DATABASE_URL"):
        assert IS_POSTGRES
        assert engine.url.database.endswith("_test")


def test_timestamps_round_trip_as_the_same_instant_in_utc(session):
    bot = make_bot(session)
    ny_evening = datetime(2025, 6, 2, 17, 45, tzinfo=ZoneInfo("America/New_York"))  # stored from a non-UTC zone
    session.add(Event(bot_id=bot.id, kind="test", message="tz", created_at=ny_evening))
    session.commit()

    with SessionLocal() as fresh:  # new session: the value really comes back from the database
        ev = fresh.scalar(select(Event).where(Event.kind == "test"))
    assert ev.created_at.tzinfo == timezone.utc
    assert ev.created_at == ny_evening  # same instant...
    assert ev.created_at.hour == 21  # ...expressed in UTC (17:45 EDT = 21:45 UTC)


def test_naive_datetimes_are_refused(session):
    bot = make_bot(session)
    session.add(Event(bot_id=bot.id, kind="test", message="naive", created_at=datetime(2025, 6, 2, 12, 0)))
    with pytest.raises(Exception, match="naive datetime"):
        session.commit()
    session.rollback()


@pytest.mark.skipif(not IS_POSTGRES, reason="column types only matter on Postgres")
def test_postgres_columns_use_timestamptz():
    with engine.connect() as conn:
        dtype = conn.scalar(text(
            "SELECT data_type FROM information_schema.columns WHERE table_name = 'orders' AND column_name = 'created_at'"
        ))
    assert dtype == "timestamp with time zone"


def test_odd_llm_output_is_normalized_before_it_reaches_the_database(session):
    """Postgres enforces column lengths (SQLite silently didn't). An LLM that answers with a
    sentence instead of a label must not crash the decision -- and an unknown action must mean HOLD."""
    bot = make_bot(session)
    t = at(19, 30)
    weird = signal("strong buy", 1.7, sentiment="cautiously optimistic with some bearish undertones overall")
    d = evaluate(session, bot, FakeBroker(now=t), t, "scheduled", weird)

    with SessionLocal() as fresh:
        saved = fresh.get(Decision, d.id)
    assert saved.action == "HOLD"
    assert saved.confidence == 1.0
    assert len(saved.sentiment) == 32
    assert bot.shares == 0


def test_history_survives_a_new_connection_pool(session):
    """What a service restart looks like to the database: a brand-new engine sees committed history."""
    bot = make_bot(session)
    t = at(19, 30)
    evaluate(session, bot, FakeBroker(now=t), t, "scheduled", signal("BUY", 0.9))
    engine.dispose()  # drop every pooled connection, like a restart
    with SessionLocal() as fresh:
        rows = list(fresh.scalars(select(Decision).where(Decision.bot_id == bot.id)))
    assert len(rows) == 1 and rows[0].action == "BUY"
    assert rows[0].created_at > t - timedelta(seconds=1)


def test_new_columns_are_added_to_a_database_that_already_has_history(session):
    """create_all() never alters an existing table; init_db must add columns introduced later
    (here: the order type and stop price), giving old rows the default."""
    bot = make_bot(session)
    session.add(Order(bot_id=bot.id, side="BUY", qty=1, reason="signal", client_order_id="old-1"))
    session.commit()
    with engine.begin() as conn:  # make the table look like it did before those columns existed
        conn.execute(text("ALTER TABLE orders DROP COLUMN order_type"))
        conn.execute(text("ALTER TABLE orders DROP COLUMN stop_price"))

    init_db()
    with engine.connect() as conn:
        assert tuple(conn.execute(text("SELECT order_type, stop_price FROM orders")).one()) == ("market", None)
