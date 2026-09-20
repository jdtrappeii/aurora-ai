"""Road511: commercial aggregator of the state 511 feeds (Florida's from FL511).

Why it exists next to the two free FL511 clients: it tracks each event's
lifecycle (start_time, end_time, archived_at) and keeps archived events, so
Aurora can backfill months of past closures near each store instead of only
seeing what is open at sync time. API key from portal.road511.com (14-day
trial, then paid). Auth is the X-API-Key header. One radius query per store
(Free plan requires a jurisdiction filter; a store's state is used). Plan
gates come back as HTTP 403 with a stable `code` (plan_archived_event,
plan_event_status, trial_expired, ...); they are reported, never raised past
the sync.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.integrations.events.common import EventDraft, ProviderError, StorePoint

DEFAULT_URL = "https://api.road511.com/api/v1/events"
SOURCE = "road511"
SEVERITY = {"minor": "minor", "moderate": "moderate", "major": "major", "critical": "severe", "info": "minor"}
TYPES_KEPT = {"incident", "construction", "closure", "special_event", "weather"}  # advisory / restriction are not traffic impacts


def _local(value: str | None, tz: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return (dt.astimezone(ZoneInfo(tz)) if tz else dt.astimezone(ZoneInfo("UTC"))).replace(tzinfo=None)


class PlanGate(ProviderError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _get(client: httpx.Client, url: str, params: dict, api_key: str) -> dict:
    resp = client.get(url, params=params, headers={"X-API-Key": api_key, "Accept": "application/json"})
    if resp.status_code == 403:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        raise PlanGate(body.get("code") or "forbidden", body.get("error") or resp.text[:200])
    if resp.status_code == 401:
        raise ProviderError("road511: invalid API key")
    if resp.status_code == 429:
        raise ProviderError(f"road511: rate limited (retry after {resp.headers.get('Retry-After', '?')}s)")
    if resp.status_code >= 400:
        raise ProviderError(f"road511: HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def to_draft(ev: dict, store: StorePoint, radius_km: float, now: datetime, status: str) -> EventDraft | None:
    if (ev.get("type") or "") not in TYPES_KEPT:
        return None
    try:
        lat, lon = float(ev["latitude"]), float(ev["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    start = _local(ev.get("start_time") or ev.get("created_at"), store.timezone)
    if start is None:
        return None
    end = _local(ev.get("end_time"), store.timezone) or _local(ev.get("archived_at"), store.timezone) \
        or _local(ev.get("estimated_end_time"), store.timezone) or _local(ev.get("last_updated"), store.timezone) or now
    if end < start:
        end = start
    roads = ", ".join(ev.get("affected_roads") or [])
    return EventDraft(
        event_id=f"{SOURCE}:{ev['id']}",
        event_type="traffic", source=SOURCE, start_time=start, end_time=end,
        severity=SEVERITY.get((ev.get("severity") or "").lower(), "moderate"),
        description=(f"{roads}: " if roads else "") + (ev.get("title") or ev.get("description") or ev.get("type") or ""),
        latitude=lat, longitude=lon, affected_radius_km=radius_km, source_reference=ev.get("source_id") or ev.get("id"),
        is_forecast=1 if start.date() > now.date() else 0,
        metadata={"type": ev.get("type"), "status": ev.get("status") or status, "lanes": ev.get("lanes_affected"),
                  "direction": ev.get("direction"), "road_class": ev.get("road_class"), "archive_reason": ev.get("archive_reason"),
                  "end_known": ev.get("end_time") is not None, "nearest_store": store.code},
    )


def fetch_events(client: httpx.Client, api_key: str, stores: list[StorePoint], radius_km: float, start: date, end: date,
                 url: str = DEFAULT_URL, statuses: tuple[str, ...] = ("active",), per_page: int = 100,
                 max_pages: int = 20, now: datetime | None = None, store_states: dict[str, str] | None = None) -> tuple[list[EventDraft], dict]:
    """One radius query per store per status, paged by offset. Duplicates across
    stores are kept once (first store wins). Plan gates stop that status only."""
    now = now or datetime.utcnow()
    stats = {"requests": 0, "rows": 0, "kept": 0, "duplicates": 0, "gates": [], "stores": len(stores)}
    seen: dict[str, EventDraft] = {}
    for status in statuses:
        gated = False
        for s in stores:
            if gated:
                break
            offset = 0
            for _ in range(max_pages):
                params = {"lat": s.latitude, "lng": s.longitude, "radius_km": radius_km, "limit": per_page, "offset": offset, "status": status}
                juris = (store_states or {}).get(s.code)
                if juris:
                    params["jurisdiction"] = juris
                try:
                    data = _get(client, url, params, api_key)
                except PlanGate as e:
                    stats["gates"].append({"status": status, "code": e.code, "message": str(e)})
                    gated = True
                    break
                stats["requests"] += 1
                rows = data.get("data") or []
                stats["rows"] += len(rows)
                for ev in rows:
                    d = to_draft(ev, s, radius_km, now, status)
                    if d is None:
                        continue
                    if d.event_id in seen:
                        stats["duplicates"] += 1
                        continue
                    if d.start_time.date() <= end and d.end_time.date() >= start:
                        seen[d.event_id] = d
                        stats["kept"] += 1
                if not data.get("has_more") or not rows:
                    break
                offset += len(rows)
    return list(seen.values()), stats
