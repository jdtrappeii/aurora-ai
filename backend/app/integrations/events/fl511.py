"""FL511 (FDOT) traffic events. One call returns every active event statewide;
we keep the ones within traffic_radius_km of a store. Register for a key at
fl511.com/developers. The endpoint follows the 511 platform used by several
states (documented publicly for 511GA): GET /api/v2/get/event?key=&format=json
returning ID, RoadwayName, Description, StartDate / PlannedEndDate (Unix),
Latitude / Longitude, EventType (roadwork | closures | accidentsAndIncidents |
specialEvents), IsFullClosure, Severity, Subtype.

Roadwork that runs for months is skipped (max_days): the engine compares
expected vs actual over the whole event window and a six-month window says
nothing. A lane closure that starts and ends inside a fortnight is kept."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app.integrations.events.common import EventDraft, StorePoint, get_json, nearest_store

SOURCE = "fl511"
MAX_DAYS = 14
TYPE_SEVERITY = {"closures": "major", "accidentsAndIncidents": "moderate", "specialEvents": "moderate", "roadwork": "minor"}


def _to_local(unix: int | None, tz: str | None) -> datetime | None:
    if not unix:
        return None
    dt = datetime.fromtimestamp(int(unix), tz=timezone.utc)
    if tz:
        dt = dt.astimezone(ZoneInfo(tz))
    return dt.replace(tzinfo=None)


def severity_for(ev: dict) -> str:
    if ev.get("IsFullClosure"):
        return "major"
    return TYPE_SEVERITY.get(ev.get("EventType") or "", "minor")


def fetch_events(client: httpx.Client, api_key: str, stores: list[StorePoint], radius_km: float,
                 url: str, max_days: int = MAX_DAYS, now: datetime | None = None) -> tuple[list[EventDraft], dict]:
    data = get_json(client, url, {"key": api_key, "format": "json"})
    rows = data if isinstance(data, list) else (data.get("events") or [])
    stats = {"total": len(rows), "near_store": 0, "too_long": 0, "kept": 0}
    now = now or datetime.utcnow()
    out: list[EventDraft] = []
    for ev in rows:
        try:
            lat, lon = float(ev["Latitude"]), float(ev["Longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        store, km = nearest_store(stores, lat, lon)
        if store is None or km > radius_km:
            continue
        stats["near_store"] += 1
        start = _to_local(ev.get("StartDate") or ev.get("Reported"), store.timezone)
        end = _to_local(ev.get("PlannedEndDate"), store.timezone) or _to_local(ev.get("LastUpdated"), store.timezone) or now
        if start is None:
            continue
        if end < start:
            end = start
        if (end - start) > timedelta(days=max_days):
            stats["too_long"] += 1
            continue
        stats["kept"] += 1
        out.append(EventDraft(
            event_id=f"fl511:{ev.get('SourceId') or ev.get('ID')}",
            event_type="traffic",
            source=SOURCE,
            start_time=start,
            end_time=end,
            severity=severity_for(ev),
            description=f"{ev.get('RoadwayName') or ''}: {ev.get('Description') or ev.get('Subtype') or ev.get('EventType')}".strip(": "),
            latitude=lat,
            longitude=lon,
            affected_radius_km=radius_km,
            source_reference=str(ev.get("ID")),
            is_forecast=1 if start.date() > (now.date() if now else date.today()) else 0,
            metadata={"event_type": ev.get("EventType"), "subtype": ev.get("Subtype"), "full_closure": ev.get("IsFullClosure"),
                      "severity_raw": ev.get("Severity"), "nearest_store": store.code, "distance_km": round(km, 2)},
        ))
    return out, stats
