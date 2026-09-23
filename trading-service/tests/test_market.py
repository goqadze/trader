"""NYSE calendar helpers: holidays, early closes, counting trading days, next decision time."""

from datetime import date, datetime, timezone

from app.market import decision_time, is_open, next_decision_time, ny_date, session_bounds, sessions_since


def test_regular_session_is_930_to_1600_new_york():
    open_, close = session_bounds(date(2025, 6, 2))
    assert open_ == datetime(2025, 6, 2, 13, 30, tzinfo=timezone.utc)  # 09:30 EDT
    assert close == datetime(2025, 6, 2, 20, 0, tzinfo=timezone.utc)  # 16:00 EDT


def test_holidays_and_weekends_have_no_session():
    assert session_bounds(date(2025, 12, 25)) is None  # Christmas
    assert session_bounds(date(2025, 6, 7)) is None  # Saturday


def test_early_close_moves_the_decision_earlier():
    # Day after Thanksgiving closes at 13:00 New York (18:00 UTC), so bots decide at 12:30
    assert decision_time(date(2025, 11, 28), 30) == datetime(2025, 11, 28, 17, 30, tzinfo=timezone.utc)


def test_is_open():
    assert is_open(datetime(2025, 6, 2, 14, 0, tzinfo=timezone.utc))
    assert not is_open(datetime(2025, 6, 2, 20, 0, tzinfo=timezone.utc))  # the close itself is closed
    assert not is_open(datetime(2025, 6, 7, 15, 0, tzinfo=timezone.utc))  # Saturday


def test_ny_date_uses_new_york_not_utc():
    # 01:00 UTC on Tuesday is still Monday evening in New York
    assert ny_date(datetime(2025, 6, 3, 1, 0, tzinfo=timezone.utc)) == date(2025, 6, 2)


def test_sessions_since_skips_weekends_and_holidays():
    assert sessions_since(None, date(2025, 6, 2)) > 1000  # never decided = always due
    assert sessions_since(date(2025, 6, 2), date(2025, 6, 2)) == 0
    assert sessions_since(date(2025, 5, 30), date(2025, 6, 2)) == 1  # Fri -> Mon
    assert sessions_since(date(2025, 12, 24), date(2025, 12, 26)) == 1  # Christmas skipped


def test_next_decision_time_rolls_over_the_weekend():
    friday_evening = datetime(2025, 6, 6, 22, 0, tzinfo=timezone.utc)
    assert next_decision_time(friday_evening, 30) == datetime(2025, 6, 9, 19, 30, tzinfo=timezone.utc)  # Monday 15:30
