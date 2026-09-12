"""Reporting periods. Weeks run Monday..Sunday (ISO)."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


@dataclass(frozen=True)
class Period:
    label: str
    start: date  # inclusive
    end: date  # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def start_dt(self) -> datetime:
        return datetime.combine(self.start, time.min)

    @property
    def end_dt_exclusive(self) -> datetime:
        return datetime.combine(self.end + timedelta(days=1), time.min)

    def to_dict(self) -> dict:
        return {"label": self.label, "start": self.start.isoformat(), "end": self.end.isoformat(), "days": self.days}


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_containing(d: date, label: str = "current_week") -> Period:
    start = week_start(d)
    return Period(label, start, start + timedelta(days=6))


def previous_week(current: Period) -> Period:
    return Period("previous_week", current.start - timedelta(days=7), current.start - timedelta(days=1))


def trailing_weeks(current: Period, n: int = 4) -> list[Period]:
    """The n full weeks immediately before `current`, oldest first."""
    weeks = []
    for i in range(n, 0, -1):
        start = current.start - timedelta(days=7 * i)
        weeks.append(Period(f"week_minus_{i}", start, start + timedelta(days=6)))
    return weeks


def four_week_window(current: Period) -> Period:
    weeks = trailing_weeks(current, 4)
    return Period("four_week_window", weeks[0].start, weeks[-1].end)


def trailing_days(as_of: date, n: int) -> Period:
    return Period(f"trailing_{n}_days", as_of - timedelta(days=n - 1), as_of)
