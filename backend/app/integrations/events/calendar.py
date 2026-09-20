"""Calendar events, generated offline.

  - Federal and state holidays from the `holidays` package (country / subdivision
    from settings), severity moderate.
  - The cannabis retail calendar: 4/20 and Green Wednesday (the day before
    Thanksgiving) are major; 7/10 (dab day), Black Friday and New Year's Eve are
    moderate. Edit CANNABIS_CALENDAR to taste; it is a plain list.

School calendars vary by county and are not machine-readable; import them
through external_events.csv with event_type=calendar (see README)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import holidays as holidays_lib

from app.integrations.events.common import EventDraft

SOURCE = "calendar"


def thanksgiving(year: int) -> date:
    d = date(year, 11, 1)
    while d.weekday() != 3:  # Thursday
        d += timedelta(days=1)
    return d + timedelta(weeks=3)


def cannabis_calendar(year: int) -> list[tuple[date, str, str]]:
    tg = thanksgiving(year)
    return [
        (date(year, 4, 20), "4/20", "major"),
        (date(year, 7, 10), "7/10 (dab day)", "moderate"),
        (tg - timedelta(days=1), "Green Wednesday", "major"),
        (tg + timedelta(days=1), "Black Friday", "moderate"),
        (date(year, 12, 31), "New Year's Eve", "moderate"),
    ]


def calendar_events(start: date, end: date, country: str = "US", subdivision: str | None = "FL",
                    latitude: float | None = None, longitude: float | None = None,
                    store_code: str | None = None) -> list[EventDraft]:
    """All-day events between start and end. Either a centroid (lat/lon) with a
    statewide radius, or an explicit store_code, decides who they apply to."""
    years = list(range(start.year, end.year + 1))
    hol = holidays_lib.country_holidays(country, subdiv=subdivision, years=years) if subdivision \
        else holidays_lib.country_holidays(country, years=years)
    # One event per date. The cannabis calendar wins the name (Black Friday over
    # "Friday After Thanksgiving") and the higher severity is kept.
    from app.analytics.external import SEVERITY_RANK

    by_date: dict[date, tuple[str, str, str]] = {}
    for y in years:
        for d, name, sev in cannabis_calendar(y):
            by_date[d] = (name, sev, "cannabis")
    for d, name in sorted(hol.items()):
        if d in by_date:
            cname, csev, _ = by_date[d]
            sev = csev if SEVERITY_RANK[csev] >= SEVERITY_RANK["moderate"] else "moderate"
            by_date[d] = (f"{cname} ({name})", sev, "cannabis+holiday")
        else:
            by_date[d] = (name, "moderate", "holiday")
    out = []
    today = date.today()
    for d, (name, sev, kind) in sorted(by_date.items()):
        if d < start or d > end:
            continue
        slug = "".join(c if c.isalnum() else "-" for c in name.casefold()).strip("-")
        out.append(EventDraft(
            event_id=f"cal:{d.isoformat()}:{slug}",
            event_type="calendar",
            source=SOURCE,
            start_time=datetime.combine(d, time.min),
            end_time=datetime.combine(d, time(23, 59)),
            severity=sev,
            description=name,
            latitude=latitude,
            longitude=longitude,
            store_code=store_code,
            is_forecast=1 if d > today else 0,
            metadata={"kind": kind},
        ))
    return out
