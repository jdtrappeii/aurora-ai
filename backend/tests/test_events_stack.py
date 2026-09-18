"""Free external-event stack: providers against httpx.MockTransport, calendar,
de-duplication, FL511 radius / duration filters, geocoding, heartbeats, and
the sync end to end. No test touches the network."""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.integrations import heartbeat as hb
from app.integrations.events import calendar as cal
from app.integrations.events import fl511, seatgeek, ticketmaster
from app.integrations.events.common import StorePoint, dedupe, geohash
from app.integrations.events.sync import events_sync, upsert_events
from app.integrations.geocode import geocode_stores
from app.models import ExternalEvent, HeartbeatRun, Store

PACE = StorePoint("HS10136", "FL - Demo - Pace", 30.60, -87.16, "America/Chicago")
TAMPA = StorePoint("HS10132", "FL - Demo - Tampa", 27.94, -82.48, "America/New_York")


def settings(**over):
    base = dict(ticketmaster_api_key="", seatgeek_client_id="", seatgeek_client_secret="", fl511_api_key="",
                fl511_api_url="https://fl511.test/api/v2/get/event", fl511_arcgis_url="", road511_api_key="", road511_url="https://r511.test/events", road511_history=True,
                events_radius_km=15.0, traffic_radius_km=5.0,
                holiday_country="US", holiday_subdivision="FL")
    base.update(over)
    return SimpleNamespace(**base)


def mock_client(router):
    """router(request) -> (status, json)."""
    def handler(request: httpx.Request) -> httpx.Response:
        status, body = router(request)
        return httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


TM_EVENT = {
    "id": "G5v0Z9Jke0B-b", "name": "Big Country Night", "url": "https://tm.example/e1",
    "dates": {"start": {"localDate": "2026-09-19", "localTime": "19:30:00"}},
    "classifications": [{"segment": {"name": "Music"}, "genre": {"name": "Country"}}],
    "_embedded": {"venues": [{"id": "V1", "name": "Pensacola Bay Center", "location": {"latitude": "30.41", "longitude": "-87.21"}}]},
}
TM_SPORTS = {
    "id": "SPORT1", "name": "Blue Wahoos vs Biscuits", "dates": {"start": {"localDate": "2026-09-20"}},
    "classifications": [{"segment": {"name": "Sports"}}],
    "_embedded": {"venues": [{"name": "Blue Wahoos Stadium", "location": {"latitude": "30.40", "longitude": "-87.22"}}]},
}
SG_DUP = {"id": 7001, "title": "Big Country Night", "url": "https://sg.example/e1", "datetime_local": "2026-09-19T19:30:00",
          "type": "concert", "score": 0.81, "venue": {"name": "Pensacola Bay Center", "capacity": 10000, "location": {"lat": 30.41, "lon": -87.21}}}
SG_NEW = {"id": 7002, "title": "Comedy Night", "datetime_local": "2026-09-21T20:00:00", "type": "comedy", "score": 0.2,
          "venue": {"name": "Vinyl Music Hall", "capacity": 700, "location": {"lat": 30.41, "lon": -87.21}}}
FL511_ROWS = [
    {"ID": 1, "SourceId": "A1", "RoadwayName": "US-90", "Description": "Crash on US-90 at Pace Blvd. All lanes blocked.",
     "StartDate": 1789731600, "PlannedEndDate": 1789738800, "LastUpdated": 1789735200, "Latitude": 30.605, "Longitude": -87.17,
     "EventType": "accidentsAndIncidents", "IsFullClosure": True, "Severity": "3", "Subtype": "crash"},
    {"ID": 2, "SourceId": "A2", "RoadwayName": "I-10", "Description": "Road construction for six months.",
     "StartDate": 1781536000, "PlannedEndDate": 1797536000, "Latitude": 30.61, "Longitude": -87.15,
     "EventType": "roadwork", "IsFullClosure": False, "Severity": "1", "Subtype": "road construction"},
    {"ID": 3, "SourceId": "A3", "RoadwayName": "I-4", "Description": "Far away in Orlando.",
     "StartDate": 1789731600, "PlannedEndDate": 1789738800, "Latitude": 28.53, "Longitude": -81.38,
     "EventType": "closures", "IsFullClosure": True, "Severity": "4", "Subtype": "closure"},
]


def test_geohash_matches_known_value():
    assert geohash(57.64911, 10.40744, 11) == "u4pruydqqvj"


def test_ticketmaster_mapping_and_severity():
    d = ticketmaster.to_draft(TM_EVENT)
    assert d.event_id == "tm:G5v0Z9Jke0B-b" and d.event_type == "local_event"
    assert d.start_time == datetime(2026, 9, 19, 19, 30) and d.end_time == datetime(2026, 9, 19, 22, 30)
    assert d.severity == "major"  # "Center" is a big-venue word
    assert d.venue == "Pensacola Bay Center" and (d.latitude, d.longitude) == (30.41, -87.21)
    s = ticketmaster.to_draft(TM_SPORTS)
    assert s.severity == "major" and s.start_time.hour == 19  # no localTime -> 19:00 default
    assert ticketmaster.to_draft({"id": "x", "dates": {"start": {}}, "_embedded": {"venues": [{}]}}) is None


def test_ticketmaster_pages_until_total_pages():
    calls = []

    def router(req):
        calls.append(dict(req.url.params))
        page = int(req.url.params["page"])
        ev = {**TM_EVENT, "id": f"E{page}"}
        return 200, {"_embedded": {"events": [ev]}, "page": {"totalPages": 2}}

    with mock_client(router) as http:
        out = ticketmaster.fetch_store_events(http, "KEY", PACE, date(2026, 9, 18), date(2026, 9, 25), 15.0, throttle_s=0)
    assert [d.event_id for d in out] == ["tm:E0", "tm:E1"]
    assert calls[0]["geoPoint"] == geohash(PACE.latitude, PACE.longitude)
    assert calls[0]["radius"] == "15" and calls[0]["unit"] == "km"
    assert calls[0]["endDateTime"] == "2026-09-26T00:00:00Z"


def test_seatgeek_mapping_and_dedupe_against_ticketmaster():
    dup, new = seatgeek.to_draft(SG_DUP), seatgeek.to_draft(SG_NEW)
    assert dup.severity == "major" and new.severity == "minor"
    kept, dropped = dedupe([ticketmaster.to_draft(TM_EVENT)], [dup, new])
    assert dropped == 1 and [k.event_id for k in kept] == ["sg:7002"]


def test_fl511_filters_by_distance_and_duration():
    with mock_client(lambda req: (200, FL511_ROWS)) as http:
        drafts, stats = fl511.fetch_events(http, "KEY", [PACE, TAMPA], 5.0, "https://fl511.test/api/v2/get/event",
                                           now=datetime(2026, 9, 18, 12, 0))
    assert stats == {"total": 3, "near_store": 2, "too_long": 1, "kept": 1}
    d = drafts[0]
    assert d.event_id == "fl511:A1" and d.event_type == "traffic" and d.severity == "major"
    # 1789731600 = 2026-09-18 11:40 UTC -> 06:40 Central (Pace)
    assert d.start_time == datetime(2026, 9, 18, 6, 40) and d.end_time == datetime(2026, 9, 18, 8, 40)
    assert d.metadata["nearest_store"] == "HS10136" and d.affected_radius_km == 5.0


def test_calendar_holidays_and_cannabis_days():
    out = cal.calendar_events(date(2026, 11, 20), date(2026, 12, 1), "US", "FL", 28.0, -82.0)
    names = {d.description: d.severity for d in out}
    assert names["Thanksgiving Day"] == "moderate"
    assert names["Green Wednesday"] == "major"
    assert names["Black Friday (Friday After Thanksgiving)"] == "moderate"  # merged, not two events
    assert len([d for d in out if d.start_time.date() == date(2026, 11, 27)]) == 1
    gw = next(d for d in out if d.description == "Green Wednesday")
    assert gw.start_time.date() == date(2026, 11, 25)  # Thanksgiving 2026 is Nov 26
    assert gw.event_id == "cal:2026-11-25:green-wednesday"
    assert cal.thanksgiving(2025) == date(2025, 11, 27)
    april = cal.calendar_events(date(2026, 4, 1), date(2026, 4, 30), "US", "FL", store_code="HS1")
    assert [d.description for d in april] == ["4/20"] and april[0].store_code == "HS1"


def test_upsert_events_is_idempotent_and_validates(session):
    session.add(Store(code="HS1", name="One", latitude=1.0, longitude=1.0))
    session.commit()
    drafts = cal.calendar_events(date(2026, 4, 1), date(2026, 4, 30), "US", "FL", store_code="HS1")
    assert upsert_events(session, drafts, "calendar").inserted == 1
    r = upsert_events(session, drafts, "calendar")
    assert (r.inserted, r.updated) == (0, 1)
    bad = cal.calendar_events(date(2026, 4, 1), date(2026, 4, 30), "US", "FL", store_code="NOPE")
    r = upsert_events(session, bad, "calendar")
    assert r.skipped == 1 and "unknown store" in r.errors[0]
    ev = session.execute(select(ExternalEvent)).scalar_one()
    assert ev.event_type == "calendar" and ev.store_id is not None and json.loads(ev.raw_source_metadata)["kind"] == "cannabis"


def test_events_sync_end_to_end(session):
    session.add(Store(code=PACE.code, name=PACE.name, latitude=PACE.latitude, longitude=PACE.longitude, timezone=PACE.timezone))
    session.add(Store(code="HS0", name="Unlocated"))
    session.commit()

    def router(req):
        host = req.url.host
        if host == "app.ticketmaster.com":
            return 200, {"_embedded": {"events": [TM_EVENT, TM_SPORTS]}, "page": {"totalPages": 1}}
        if host == "api.seatgeek.com":
            return 200, {"events": [SG_DUP, SG_NEW], "meta": {"total": 2}}
        if host == "fl511.test":
            return 200, FL511_ROWS
        return 404, {}

    cfg = settings(ticketmaster_api_key="tm", seatgeek_client_id="sg", fl511_api_key="fl")
    with mock_client(router) as http:
        report = events_sync(session, date(2026, 9, 14), date(2026, 9, 27), http, cfg, now=datetime(2026, 9, 18, 12, 0))
    assert report.stores_with_coordinates == 1 and report.stores_without_coordinates == ["HS0"]
    assert report.providers["ticketmaster"] == {"events": 2, "errors": 0}
    assert report.providers["seatgeek"] == {"events": 1, "duplicates_of_ticketmaster": 1, "errors": 0}
    assert report.providers["fl511"]["kept"] == 1 and report.providers["fl511"]["in_range"] == 1
    assert report.providers["calendar"]["events"] == 0  # no holiday between Sep 14 and Sep 27
    by_source = {}
    for ev in session.execute(select(ExternalEvent)).scalars():
        by_source.setdefault(ev.source, []).append(ev)
    assert {k: len(v) for k, v in by_source.items()} == {"ticketmaster": 2, "seatgeek": 1, "fl511": 1}
    assert by_source["fl511"][0].affected_radius_km == 5.0
    # second run updates in place
    with mock_client(router) as http:
        events_sync(session, date(2026, 9, 14), date(2026, 9, 27), http, cfg, now=datetime(2026, 9, 18, 12, 0))
    assert len(session.execute(select(ExternalEvent)).scalars().all()) == 4


def test_events_sync_without_keys_only_writes_calendar(session):
    session.add(Store(code="HS1", name="One", latitude=28.0, longitude=-82.0))
    session.commit()
    with mock_client(lambda req: (500, {})) as http:
        report = events_sync(session, date(2026, 12, 20), date(2027, 1, 2), http, settings())
    assert report.providers["ticketmaster"].startswith("disabled")
    assert report.providers["fl511"].startswith("disabled")
    names = sorted(e.description for e in session.execute(select(ExternalEvent)).scalars())
    assert "Christmas Day" in names and "New Year's Eve" in names and "New Year's Day" in names
    ev = session.execute(select(ExternalEvent)).scalars().first()
    assert ev.affected_radius_km == 1000.0 and ev.latitude == 28.0


def test_events_sync_provider_error_is_reported_not_raised(session):
    session.add(Store(code="HS1", name="One", latitude=28.0, longitude=-82.0))
    session.commit()
    with mock_client(lambda req: (401, {"fault": "bad key"})) as http:
        report = events_sync(session, date(2026, 9, 14), date(2026, 9, 20), http, settings(ticketmaster_api_key="bad"), include_calendar=False)
    assert report.providers["ticketmaster"] == {"events": 0, "errors": 1}
    assert "HTTP 401" in report.warnings[0]


def test_geocode_fills_only_missing_coordinates(session):
    session.add_all([
        Store(code="A", name="A", address="4612 School Lane, Milton, FL 32571"),
        Store(code="B", name="B", latitude=1.0, longitude=2.0, address="somewhere"),
        Store(code="C", name="C"),
        Store(code="D", name="D", address="nowhere at all"),
    ])
    session.commit()
    seen = []

    def router(req):
        seen.append((req.headers.get("user-agent"), req.url.params["q"]))
        if "School Lane" in req.url.params["q"]:
            return 200, [{"lat": "30.6011", "lon": "-87.1602"}]
        return 200, []

    with mock_client(router) as http:
        rep = geocode_stores(session, http, "aurora-test/1.0 (ops@example.com)", throttle_s=0)
    assert rep.geocoded == ["A"] and rep.unresolved == ["D"] and rep.skipped_no_address == ["C"] and rep.already_located == 1
    a = session.execute(select(Store).where(Store.code == "A")).scalar_one()
    assert (a.latitude, a.longitude) == (30.6011, -87.1602)
    assert seen[0][0] == "aurora-test/1.0 (ops@example.com)"


def test_heartbeat_runs_and_gap_events(session):
    store = Store(code="HS1", name="One")
    session.add(store)
    session.commit()
    t0 = datetime(2026, 9, 18, 9, 0)
    for m in (0, 1, 2, 3):
        hb.record_ping(session, store, "power", t0 + timedelta(minutes=m), gap_minutes=5)
    # 47 minutes of silence, then pings resume
    for m in (50, 51):
        hb.record_ping(session, store, "power", t0 + timedelta(minutes=m), gap_minutes=5)
    # network never dropped
    for m in (0, 1, 2):
        hb.record_ping(session, store, "network", t0 + timedelta(minutes=m), gap_minutes=5)
    runs = session.execute(select(HeartbeatRun).order_by(HeartbeatRun.kind, HeartbeatRun.started_at)).scalars().all()
    assert [(r.kind, r.pings) for r in runs] == [("network", 3), ("power", 4), ("power", 2)]

    drafts = hb.heartbeat_events(session, min_gap_minutes=5, now=t0 + timedelta(minutes=52))
    assert len(drafts) == 1
    d = drafts[0]
    assert d.event_type == "utility" and d.store_code == "HS1" and d.severity == "moderate"  # 47 min
    assert (d.start_time, d.end_time) == (t0 + timedelta(minutes=3), t0 + timedelta(minutes=50))
    assert d.event_id == "hb:HS1:power:20260918T0903"
    # an ongoing silence on the network side shows up when asked for
    drafts = hb.heartbeat_events(session, 5, now=t0 + timedelta(hours=6), open_gap_after_minutes=10)
    kinds = sorted((x.event_type, x.metadata["closed"]) for x in drafts)
    assert kinds == [("connectivity", False), ("utility", False), ("utility", True)]
    r = upsert_events(session, drafts, "heartbeat")
    assert r.inserted == 3
    st = hb.status(session, now=t0 + timedelta(minutes=60))
    assert [(s.kind, s.minutes_silent) for s in st] == [("network", 58.0), ("power", 9.0)]
    assert hb.severity_for(300) == "severe" and hb.severity_for(5) == "minor"
    with pytest.raises(ValueError):
        hb.record_ping(session, store, "water", t0, 5)


def test_heartbeat_endpoint(engine, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app import config
    from app.db import get_session
    from app.main import app

    monkeypatch.setattr(config.settings, "heartbeat_token", "s3cret")
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        s.add(Store(code="HS1", name="One"))
        s.commit()

    def _override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        assert c.post("/api/heartbeat", params={"store": "HS1", "token": "nope"}).status_code == 401
        assert c.post("/api/heartbeat", params={"store": "ZZ", "token": "s3cret"}).status_code == 404
        r = c.post("/api/heartbeat", params={"store": "HS1", "token": "s3cret", "kind": "network", "at": "2026-09-18T09:00:00"})
        assert r.status_code == 200 and r.json()["pings"] == 1
        r = c.post("/api/heartbeat", params={"store": "HS1", "token": "s3cret", "kind": "network", "at": "2026-09-18T09:01:00"})
        assert r.json()["pings"] == 2
        st = c.get("/api/heartbeat/status").json()
        assert st[0]["store"] == "HS1" and st[0]["kind"] == "network"
        assert c.post("/api/heartbeat", params={"store": "HS1", "token": "s3cret", "kind": "water"}).status_code == 422
    app.dependency_overrides.clear()


GIS_ROWS = {"exceededTransferLimit": False, "features": [
    {"attributes": {"OBJECTID": 1, "incident_type": "Crash", "Severity": "intermediate", "IncidentID": "114870", "status": "confirmed",
                    "description": "Crash in Santa Rosa County on US-90 at Pace Blvd. All lanes blocked. Last updated at 08:08 AM.",
                    "TimeReported": "09/18/2026 7:29:11 AM", "LastUpdated": "09/18/2026 8:08:39 AM",
                    "primarylocation_county": "Santa Rosa", "primarylocation_highway": "US-90", "primarylocation_direction": "e"},
     "geometry": {"x": -87.17, "y": 30.605}},
    {"attributes": {"OBJECTID": 2, "incident_type": "Planned Construction", "Severity": "minor", "IncidentID": "114871",
                    "description": "Planned construction in Palm Beach County. Left lane blocked.", "TimeReported": "09/01/2026 7:31:18 AM",
                    "LastUpdated": "09/16/2026 8:08:48 AM", "primarylocation_highway": "Southern Blvd", "primarylocation_direction": "e"},
     "geometry": {"x": -80.15, "y": 26.68}},
    {"attributes": {"OBJECTID": 3, "incident_type": "Road Closed", "Severity": "minor", "IncidentID": "9", "description": "Not reprojected",
                    "TimeReported": "09/18/2026 7:29:11 AM", "LastUpdated": "09/18/2026 7:29:11 AM"},
     "geometry": {"x": -8921837.95, "y": 3087014.53}},
]}


def test_fl511_arcgis_keyless_feed():
    from app.integrations.events import fl511_arcgis

    seen = []

    def router(req):
        seen.append(dict(req.url.params))
        return 200, GIS_ROWS

    with mock_client(router) as http:
        drafts, stats = fl511_arcgis.fetch_events(http, [PACE, TAMPA], 5.0, "https://gis.test/query", now=datetime(2026, 9, 18, 14, 0))
    assert seen[0]["outSR"] == "4326" and seen[0]["resultRecordCount"] == "1000"
    assert stats == {"total": 3, "near_store": 1, "kept": 1, "pages": 1}
    d = drafts[0]
    assert d.event_id == "fl511-gis:114870" and d.event_type == "traffic" and d.severity == "major"  # crash + all lanes blocked
    # 7:29 Eastern -> 6:29 Central at Pace
    assert d.start_time == datetime(2026, 9, 18, 6, 29, 11) and d.end_time == datetime(2026, 9, 18, 7, 8, 39)
    assert d.metadata["nearest_store"] == "HS10136" and d.metadata["county"] == "Santa Rosa"
    assert fl511_arcgis.severity_for({"Severity": "minor", "incident_type": "Planned Construction", "description": "Left lane blocked"}) == "minor"
    assert fl511_arcgis.severity_for({"Severity": "minor", "incident_type": "Road Closed", "description": ""}) == "major"


def test_events_sync_uses_public_layer_without_a_key(session):
    session.add(Store(code=PACE.code, name=PACE.name, latitude=PACE.latitude, longitude=PACE.longitude, timezone=PACE.timezone))
    session.commit()

    def router(req):
        if req.url.host == "gis.test":
            return 200, GIS_ROWS
        return 404, {}

    cfg = settings(fl511_arcgis_url="https://gis.test/query")
    with mock_client(router) as http:
        report = events_sync(session, date(2026, 9, 14), date(2026, 9, 27), http, cfg, include_calendar=False, now=datetime(2026, 9, 18, 14, 0))
    assert report.providers["fl511_arcgis"]["kept"] == 1 and report.providers["fl511_arcgis"]["in_range"] == 1
    assert "fl511" not in report.providers  # the keyed feed is not reported as disabled when the public layer ran
    assert session.execute(select(ExternalEvent)).scalar_one().source == "fl511-gis"


R511_ACTIVE = {"data": [
    {"id": "fl-evt-1", "source_id": "SG-1", "source": "fl", "jurisdiction": "FL", "type": "incident", "severity": "major", "status": "active",
     "title": "Crash on US-90 WB at Pace Blvd", "affected_roads": ["US-90"], "direction": "westbound", "lanes_affected": "all lanes blocked",
     "start_time": "2026-09-18T11:40:00Z", "end_time": None, "estimated_end_time": "2026-09-18T13:40:00Z", "latitude": 30.605, "longitude": -87.17,
     "last_updated": "2026-09-18T12:00:00Z", "created_at": "2026-09-18T11:40:00Z"},
    {"id": "fl-evt-2", "type": "restriction", "severity": "minor", "status": "active", "title": "Weight limit", "start_time": "2026-09-18T00:00:00Z",
     "latitude": 30.606, "longitude": -87.16},
], "total": 2, "has_more": False}
R511_ARCHIVED = {"data": [
    {"id": "fl-evt-old", "type": "closure", "severity": "critical", "status": "archived", "title": "Road closed for parade", "affected_roads": ["SR-90"],
     "start_time": "2026-09-05T13:00:00Z", "end_time": None, "archived_at": "2026-09-05T18:30:00Z", "archive_reason": "observed",
     "latitude": 30.60, "longitude": -87.165},
], "total": 1, "has_more": False}


def test_road511_mapping_and_paging():
    from app.integrations.events import road511

    seen = []

    def router(req):
        seen.append((dict(req.url.params), req.headers.get("x-api-key")))
        if req.url.params["status"] == "archived":
            return 200, R511_ARCHIVED
        if int(req.url.params["offset"]) == 0:  # the provider advances offset by rows received, not by page size
            return 200, {**R511_ACTIVE, "has_more": True}
        return 200, {"data": [], "has_more": False}

    with mock_client(router) as http:
        drafts, stats = road511.fetch_events(http, "sk_test", [PACE], 5.0, date(2026, 9, 1), date(2026, 9, 30), "https://r511.test/events",
                                             statuses=("active", "archived"), now=datetime(2026, 9, 18, 14, 0), store_states={"HS10136": "FL"})
    assert seen[0][1] == "sk_test" and seen[0][0]["jurisdiction"] == "FL" and seen[0][0]["radius_km"] == "5.0"
    assert stats["requests"] == 3 and stats["kept"] == 2 and stats["gates"] == []
    by = {d.event_id: d for d in drafts}
    crash = by["road511:fl-evt-1"]
    assert crash.severity == "major" and crash.description == "US-90: Crash on US-90 WB at Pace Blvd"
    assert crash.start_time == datetime(2026, 9, 18, 6, 40) and crash.end_time == datetime(2026, 9, 18, 8, 40)  # UTC -> Central, estimated end
    assert crash.metadata["end_known"] is False and crash.source_reference == "SG-1"
    old = by["road511:fl-evt-old"]
    assert old.severity == "severe" and old.end_time == datetime(2026, 9, 5, 13, 30) and old.metadata["archive_reason"] == "observed"
    assert "road511:fl-evt-2" not in by  # restrictions are not traffic impacts


def test_road511_plan_gate_is_reported_not_raised(session):
    session.add(Store(code=PACE.code, name=PACE.name, latitude=PACE.latitude, longitude=PACE.longitude, timezone=PACE.timezone, state="FL"))
    session.commit()

    def router(req):
        if req.url.params["status"] == "archived":
            return 403, {"error": "archived events require Starter", "code": "plan_archived_event", "plan": "free"}
        return 200, R511_ACTIVE

    with mock_client(router) as http:
        report = events_sync(session, date(2026, 9, 14), date(2026, 9, 27), http, settings(road511_api_key="sk_test", fl511_arcgis_url="https://gis.test/query"),
                             include_calendar=False, now=datetime(2026, 9, 18, 14, 0))
    assert report.providers["road511"]["kept"] == 1 and report.providers["road511"]["gates"][0]["code"] == "plan_archived_event"
    assert any("plan_archived_event" in w for w in report.warnings)
    assert "fl511_arcgis" not in report.providers  # Road511 takes over as the traffic source
    assert session.execute(select(ExternalEvent)).scalar_one().source == "road511"
