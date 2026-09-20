from datetime import date, datetime

from app.analytics.periods import four_week_window, previous_week, trailing_days, trailing_weeks, week_containing


def test_week_is_monday_to_sunday():
    # 2026-09-10 is a Thursday
    w = week_containing(date(2026, 9, 10))
    assert (w.start, w.end) == (date(2026, 9, 7), date(2026, 9, 13))
    assert w.days == 7
    assert w.start_dt == datetime(2026, 9, 7, 0, 0)
    assert w.end_dt_exclusive == datetime(2026, 9, 14, 0, 0)


def test_sunday_belongs_to_the_week_that_started_the_previous_monday():
    w = week_containing(date(2026, 9, 13))
    assert w.start == date(2026, 9, 7)


def test_previous_and_trailing_weeks_tile_without_gaps():
    cur = week_containing(date(2026, 9, 7))
    prev = previous_week(cur)
    assert (prev.start, prev.end) == (date(2026, 8, 31), date(2026, 9, 6))
    weeks = trailing_weeks(cur, 4)
    assert [w.start for w in weeks] == [date(2026, 8, 10), date(2026, 8, 17), date(2026, 8, 24), date(2026, 8, 31)]
    window = four_week_window(cur)
    assert (window.start, window.end, window.days) == (date(2026, 8, 10), date(2026, 9, 6), 28)


def test_trailing_days_inclusive():
    p = trailing_days(date(2026, 9, 11), 30)
    assert p.start == date(2026, 8, 13)
    assert p.days == 30
