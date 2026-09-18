"""FL511 incidents from FDOT's public ArcGIS feature service. No key.

Layer 0 "FL511_Unified_Incidents" of the FL511_2026_feed_view service carries
the live incident list behind fl511.com: incident_type (Crash, Planned
Construction, Road Closed, ...), Severity (minor | intermediate | major),
description, TimeReported / LastUpdated (Eastern, "09/01/2026 7:29:11 AM"),
status, county, highway, direction, IncidentID and a point. The service
reprojects to WGS84 when asked (outSR=4326) and pages 1000 rows at a time.

The feed is the *current* list: an incident vanishes once cleared, so its end
time is the last time we saw it. A nightly sync therefore records incidents
as they stood at sync time; run `events-sync` hourly (cron) to catch short
ones. Same event shape and radius rule as the keyed FL511 feed."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.integrations.events.common import EventDraft, StorePoint, get_json, nearest_store

DEFAULT_URL = "https://services.arcgis.com/3wFbqsFPLeKqOlIK/arcgis/rest/services/FL511_2026_feed_view/FeatureServer/0/query"
SOURCE = "fl511-gis"
PAGE = 1000
FEED_TZ = "America/New_York"
SEVERITY = {"minor": "minor", "intermediate": "moderate", "major": "major", "severe": "severe"}
TYPE_FLOOR = {"road closed": "major", "closure": "major", "crash": "moderate", "vehicle fire": "moderate"}
RANK = {"minor": 1, "moderate": 2, "major": 3, "severe": 4}


def _parse_time(value: str | None, tz: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value.strip(), fmt).replace(tzinfo=ZoneInfo(FEED_TZ))
            return (dt.astimezone(ZoneInfo(tz)) if tz else dt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def severity_for(attrs: dict) -> str:
    sev = SEVERITY.get((attrs.get("Severity") or "").strip().lower(), "minor")
    text = f"{attrs.get('incident_type') or ''} {attrs.get('description') or ''}".lower()
    if "all lanes blocked" in text or "all lanes closed" in text:
        sev = max(sev, "major", key=RANK.get)
    for needle, floor in TYPE_FLOOR.items():
        if needle in text:
            sev = max(sev, floor, key=RANK.get)
    return sev


def fetch_events(client: httpx.Client, stores: list[StorePoint], radius_km: float, url: str = DEFAULT_URL,
                 now: datetime | None = None, max_pages: int = 10) -> tuple[list[EventDraft], dict]:
    now = now or datetime.utcnow()
    stats = {"total": 0, "near_store": 0, "kept": 0, "pages": 0}
    out: list[EventDraft] = []
    offset = 0
    for _ in range(max_pages):
        data = get_json(client, url, {"where": "1=1", "outFields": "*", "outSR": "4326", "f": "json",
                                      "resultRecordCount": PAGE, "resultOffset": offset})
        feats = data.get("features") or []
        stats["pages"] += 1
        stats["total"] += len(feats)
        for f in feats:
            a = f.get("attributes") or {}
            g = f.get("geometry") or {}
            try:
                lon, lat = float(g["x"]), float(g["y"])
            except (KeyError, TypeError, ValueError):
                continue
            if abs(lon) > 180 or abs(lat) > 90:  # not reprojected: skip rather than guess
                continue
            store, km = nearest_store(stores, lat, lon)
            if store is None or km > radius_km:
                continue
            stats["near_store"] += 1
            start = _parse_time(a.get("TimeReported"), store.timezone)
            last = _parse_time(a.get("LastUpdated"), store.timezone) or start
            if start is None:
                continue
            end = max(last, start)
            iid = a.get("IncidentID") or a.get("OBJECTID")
            stats["kept"] += 1
            out.append(EventDraft(
                event_id=f"{SOURCE}:{iid}",
                event_type="traffic", source=SOURCE, start_time=start, end_time=end, severity=severity_for(a),
                description=f"{a.get('primarylocation_highway') or ''} {a.get('primarylocation_direction') or ''}: {a.get('description') or a.get('incident_type') or ''}".strip(": "),
                latitude=lat, longitude=lon, affected_radius_km=radius_km, source_reference=str(iid),
                metadata={"incident_type": a.get("incident_type"), "severity_raw": a.get("Severity"), "status": a.get("status"),
                          "county": a.get("primarylocation_county"), "nearest_store": store.code, "distance_km": round(km, 2),
                          "last_seen": now.isoformat(timespec="minutes")},
            ))
        if not data.get("exceededTransferLimit") or not feats:
            break
        offset += len(feats)
    return out, stats
