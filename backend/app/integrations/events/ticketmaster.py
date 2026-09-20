"""Ticketmaster Discovery API v2. Free key at developer.ticketmaster.com:
5,000 calls/day, 5/second. One call per store per page (200 events a page)."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import httpx

from app.integrations.events.common import EventDraft, ProviderError, StorePoint, geohash, get_json

BASE = "https://app.ticketmaster.com/discovery/v2/events.json"
PAGE_SIZE = 200
MAX_PAGES = 5  # the API refuses page * size > 1000
SOURCE = "ticketmaster"
DEFAULT_HOURS = 3  # a listing has a start but no end; a show is assumed to run this long
BIG_VENUE_WORDS = ("stadium", "arena", "amphitheat", "speedway", "coliseum", "field", "park", "center", "centre")


def severity_for(segment: str | None, venue_name: str | None) -> str:
    """Ticketmaster has no attendance figure, so severity is a venue heuristic:
    stadiums / arenas / amphitheatres and any pro-sports fixture are major."""
    seg = (segment or "").casefold()
    venue = (venue_name or "").casefold()
    if seg == "sports" or any(w in venue for w in BIG_VENUE_WORDS):
        return "major"
    return "moderate"


def _local_start(ev: dict) -> datetime | None:
    start = (ev.get("dates") or {}).get("start") or {}
    d, t = start.get("localDate"), start.get("localTime")
    if not d:
        return None
    return datetime.fromisoformat(f"{d}T{t or '19:00:00'}")


def fetch_store_events(client: httpx.Client, api_key: str, store: StorePoint, start: date, end: date,
                       radius_km: float, throttle_s: float = 0.25) -> list[EventDraft]:
    out: list[EventDraft] = []
    for page in range(MAX_PAGES):
        params = {
            "apikey": api_key,
            "geoPoint": geohash(store.latitude, store.longitude),
            "radius": str(int(round(radius_km))),
            "unit": "km",
            "startDateTime": f"{start.isoformat()}T00:00:00Z",
            "endDateTime": f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z",
            "size": PAGE_SIZE,
            "page": page,
            "sort": "date,asc",
        }
        data = get_json(client, BASE, params)
        events = ((data.get("_embedded") or {}).get("events")) or []
        for ev in events:
            draft = to_draft(ev)
            if draft:
                out.append(draft)
        total_pages = (data.get("page") or {}).get("totalPages", 1)
        if page + 1 >= total_pages or not events:
            break
        if throttle_s:
            time.sleep(throttle_s)
    return out


def to_draft(ev: dict) -> EventDraft | None:
    start = _local_start(ev)
    venue = ((ev.get("_embedded") or {}).get("venues") or [{}])[0]
    loc = venue.get("location") or {}
    try:
        lat, lon = float(loc["latitude"]), float(loc["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if start is None:
        return None
    cls = (ev.get("classifications") or [{}])[0]
    segment = (cls.get("segment") or {}).get("name")
    genre = (cls.get("genre") or {}).get("name")
    return EventDraft(
        event_id=f"tm:{ev['id']}",
        event_type="local_event",
        source=SOURCE,
        start_time=start,
        end_time=start + timedelta(hours=DEFAULT_HOURS),
        severity=severity_for(segment, venue.get("name")),
        description=f"{ev.get('name')} at {venue.get('name')}" + (f" ({segment}/{genre})" if segment else ""),
        latitude=lat,
        longitude=lon,
        source_reference=ev.get("url"),
        is_forecast=1 if start.date() > date.today() else 0,
        venue=venue.get("name"),
        metadata={"segment": segment, "genre": genre, "venue_id": venue.get("id")},
    )
