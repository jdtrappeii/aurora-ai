"""SeatGeek Platform API. Free client id at platform.seatgeek.com. Events carry
a popularity `score` (0..1) and venues often carry `capacity`, which give a
better severity than Ticketmaster's venue-name heuristic."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import httpx

from app.integrations.events.common import EventDraft, StorePoint, get_json

BASE = "https://api.seatgeek.com/2/events"
PAGE_SIZE = 100
MAX_PAGES = 10
SOURCE = "seatgeek"
DEFAULT_HOURS = 3


def severity_for(score: float | None, capacity: int | None) -> str:
    if (capacity or 0) >= 15000 or (score or 0) >= 0.7:
        return "major"
    if (capacity or 0) >= 2000 or (score or 0) >= 0.4:
        return "moderate"
    return "minor"


def fetch_store_events(client: httpx.Client, client_id: str, client_secret: str, store: StorePoint, start: date,
                       end: date, radius_km: float, throttle_s: float = 0.25) -> list[EventDraft]:
    out: list[EventDraft] = []
    for page in range(1, MAX_PAGES + 1):
        params = {
            "client_id": client_id,
            "lat": store.latitude,
            "lon": store.longitude,
            "range": f"{int(round(radius_km))}km",
            "datetime_local.gte": start.isoformat(),
            "datetime_local.lte": (end + timedelta(days=1)).isoformat(),
            "per_page": PAGE_SIZE,
            "page": page,
            "sort": "datetime_local.asc",
        }
        if client_secret:
            params["client_secret"] = client_secret
        data = get_json(client, BASE, params)
        events = data.get("events") or []
        for ev in events:
            draft = to_draft(ev)
            if draft:
                out.append(draft)
        meta = data.get("meta") or {}
        if page * PAGE_SIZE >= int(meta.get("total", 0)) or not events:
            break
        if throttle_s:
            time.sleep(throttle_s)
    return out


def to_draft(ev: dict) -> EventDraft | None:
    venue = ev.get("venue") or {}
    loc = venue.get("location") or {}
    try:
        lat, lon = float(loc["lat"]), float(loc["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    when = ev.get("datetime_local")
    if not when:
        return None
    start = datetime.fromisoformat(when.replace("Z", ""))
    if ev.get("time_tbd") and start.hour == 3 and start.minute == 30:  # SeatGeek's TBD placeholder
        start = start.replace(hour=19, minute=0)
    return EventDraft(
        event_id=f"sg:{ev['id']}",
        event_type="local_event",
        source=SOURCE,
        start_time=start,
        end_time=start + timedelta(hours=DEFAULT_HOURS),
        severity=severity_for(ev.get("score"), venue.get("capacity")),
        description=f"{ev.get('title')} at {venue.get('name')}" + (f" ({ev.get('type')})" if ev.get("type") else ""),
        latitude=lat,
        longitude=lon,
        source_reference=ev.get("url"),
        is_forecast=1 if start.date() > date.today() else 0,
        venue=venue.get("name"),
        metadata={"type": ev.get("type"), "score": ev.get("score"), "capacity": venue.get("capacity")},
    )
