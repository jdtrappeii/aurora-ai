"""Run the free event stack for a date range and upsert into external_events.

    report = events_sync(session, start, end, http=httpx.Client(), settings=settings)

Providers run only when their key is set. Ticketmaster is the primary ticket
source; SeatGeek events at the same venue on the same day are dropped. Calendar
events are generated once with the stores' centroid and a statewide radius, or
per store when no store has coordinates yet. Re-running is idempotent (event_id).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.external import DEFAULT_RADIUS_KM
from app.importers.csv_importer import ImportResult
from app.integrations.events import calendar as calendar_provider
from app.integrations.events import fl511, fl511_arcgis, road511, seatgeek, ticketmaster
from app.integrations.events.common import EventDraft, ProviderError, StorePoint, dedupe
from app.models import ExternalEvent, Store


@dataclass
class EventsReport:
    stores_with_coordinates: int = 0
    stores_without_coordinates: list[str] = field(default_factory=list)
    providers: dict = field(default_factory=dict)
    results: list[ImportResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "stores_with_coordinates": self.stores_with_coordinates,
            "stores_without_coordinates": self.stores_without_coordinates,
            "providers": self.providers,
            "results": [r.to_dict() for r in self.results],
            "warnings": self.warnings,
        }


def store_points(session: Session) -> tuple[list[StorePoint], list[str]]:
    located, missing = [], []
    for s in session.execute(select(Store).order_by(Store.code)).scalars():
        if s.latitude is not None and s.longitude is not None:
            located.append(StorePoint(s.code, s.name, s.latitude, s.longitude, s.timezone))
        else:
            missing.append(s.code)
    return located, missing


def upsert_events(session: Session, drafts: list[EventDraft], kind: str) -> ImportResult:
    import json

    res = ImportResult(kind)
    codes = {s.code: s.id for s in session.execute(select(Store)).scalars()}
    existing = {e.event_id: e for e in session.execute(
        select(ExternalEvent).where(ExternalEvent.event_id.in_([d.event_id for d in drafts]))
    ).scalars()} if drafts else {}
    for d in drafts:
        if d.store_code and d.store_code not in codes:
            res.errors.append(f"{d.event_id}: unknown store {d.store_code}")
            res.skipped += 1
            continue
        if d.store_code is None and (d.latitude is None or d.longitude is None):
            res.errors.append(f"{d.event_id}: needs a store or coordinates")
            res.skipped += 1
            continue
        values = dict(
            store_id=codes.get(d.store_code) if d.store_code else None,
            event_type=d.event_type, source=d.source, latitude=d.latitude, longitude=d.longitude,
            affected_radius_km=d.affected_radius_km, start_time=d.start_time, end_time=d.end_time,
            severity=d.severity, description=d.description, confidence=d.confidence,
            source_reference=d.source_reference, is_forecast=d.is_forecast,
            raw_source_metadata=json.dumps(d.metadata, default=str) if d.metadata else None,
        )
        ev = existing.get(d.event_id)
        if ev is None:
            ev = ExternalEvent(event_id=d.event_id, **values)
            session.add(ev)
            existing[d.event_id] = ev
            res.inserted += 1
        else:
            for k, v in values.items():
                setattr(ev, k, v)
            res.updated += 1
    session.commit()
    return res


def events_sync(session: Session, start: date, end: date, http: httpx.Client, settings,
                include_calendar: bool = True, now: datetime | None = None) -> EventsReport:
    if end < start:
        raise ValueError("end precedes start")
    report = EventsReport()
    stores, missing = store_points(session)
    report.stores_with_coordinates = len(stores)
    report.stores_without_coordinates = missing
    if missing:
        report.warnings.append(f"{len(missing)} store(s) have no coordinates and get no local events: run geocode-stores")

    # --- ticket sellers ---
    tm_drafts: list[EventDraft] = []
    if settings.ticketmaster_api_key and stores:
        errors = 0
        for s in stores:
            try:
                tm_drafts.extend(ticketmaster.fetch_store_events(http, settings.ticketmaster_api_key, s, start, end, settings.events_radius_km))
            except ProviderError as e:
                errors += 1
                report.warnings.append(f"ticketmaster {s.code}: {e}")
        tm_drafts = _unique(tm_drafts)
        report.providers["ticketmaster"] = {"events": len(tm_drafts), "errors": errors}
        report.results.append(upsert_events(session, tm_drafts, "ticketmaster"))
    elif stores:
        report.providers["ticketmaster"] = "disabled (TICKETMASTER_API_KEY not set)"

    if settings.seatgeek_client_id and stores:
        sg_drafts: list[EventDraft] = []
        errors = 0
        for s in stores:
            try:
                sg_drafts.extend(seatgeek.fetch_store_events(http, settings.seatgeek_client_id, settings.seatgeek_client_secret, s, start, end, settings.events_radius_km))
            except ProviderError as e:
                errors += 1
                report.warnings.append(f"seatgeek {s.code}: {e}")
        sg_drafts, dropped = dedupe(tm_drafts, _unique(sg_drafts))
        report.providers["seatgeek"] = {"events": len(sg_drafts), "duplicates_of_ticketmaster": dropped, "errors": errors}
        report.results.append(upsert_events(session, sg_drafts, "seatgeek"))
    elif stores:
        report.providers["seatgeek"] = "disabled (SEATGEEK_CLIENT_ID not set)"

    # --- traffic: Road511 when its key exists (lifecycle + history), else the keyed FL511 feed, else FDOT's public layer ---
    road511_key = getattr(settings, "road511_api_key", "")
    if road511_key and stores:
        try:
            statuses = ("active", "archived") if getattr(settings, "road511_history", True) else ("active",)
            states = {s.code: st for s in session.execute(select(Store)).scalars() for st in [s.state] if st}
            drafts, stats = road511.fetch_events(http, road511_key, stores, settings.traffic_radius_km, start, end,
                                                 getattr(settings, "road511_url", road511.DEFAULT_URL), statuses=statuses, now=now, store_states=states)
            report.providers["road511"] = {**stats, "in_range": len(drafts)}
            for g in stats["gates"]:
                report.warnings.append(f"road511 {g['status']}: {g['message']}")
            report.results.append(upsert_events(session, drafts, "road511"))
        except ProviderError as e:
            report.providers["road511"] = {"error": str(e)}
            report.warnings.append(f"road511: {e}")
    if not road511_key and not settings.fl511_api_key and getattr(settings, "fl511_arcgis_url", "") and stores:
        try:
            drafts, stats = fl511_arcgis.fetch_events(http, stores, settings.traffic_radius_km, settings.fl511_arcgis_url, now=now)
            drafts = [d for d in drafts if d.start_time.date() <= end and d.end_time.date() >= start]
            report.providers["fl511_arcgis"] = {**stats, "in_range": len(drafts)}
            report.results.append(upsert_events(session, drafts, "fl511-gis"))
        except ProviderError as e:
            report.providers["fl511_arcgis"] = {"error": str(e)}
            report.warnings.append(f"fl511_arcgis: {e}")
    if not road511_key and settings.fl511_api_key and stores:
        try:
            drafts, stats = fl511.fetch_events(http, settings.fl511_api_key, stores, settings.traffic_radius_km, settings.fl511_api_url, now=now)
            drafts = [d for d in drafts if d.start_time.date() <= end and d.end_time.date() >= start]
            report.providers["fl511"] = {**stats, "in_range": len(drafts)}
            report.results.append(upsert_events(session, drafts, "fl511"))
        except ProviderError as e:
            report.providers["fl511"] = {"error": str(e)}
            report.warnings.append(f"fl511: {e}")
    elif stores and not getattr(settings, "fl511_arcgis_url", ""):
        report.providers["fl511"] = "disabled (FL511_API_KEY not set and FL511_ARCGIS_URL blank)"

    # --- calendar ---
    if include_calendar:
        if stores:
            lat = sum(s.latitude for s in stores) / len(stores)
            lon = sum(s.longitude for s in stores) / len(stores)
            drafts = calendar_provider.calendar_events(start, end, settings.holiday_country, settings.holiday_subdivision or None, lat, lon)
            for d in drafts:
                d.affected_radius_km = DEFAULT_RADIUS_KM["calendar"]
        else:
            drafts = []
            for code in missing:
                for d in calendar_provider.calendar_events(start, end, settings.holiday_country, settings.holiday_subdivision or None, store_code=code):
                    d.event_id = f"{d.event_id}:{code}"
                    drafts.append(d)
        report.providers["calendar"] = {"events": len(drafts)}
        report.results.append(upsert_events(session, drafts, "calendar"))
    return report


def _unique(drafts: list[EventDraft]) -> list[EventDraft]:
    """The same event is returned for every store within radius; keep one."""
    seen: dict[str, EventDraft] = {}
    for d in drafts:
        seen.setdefault(d.event_id, d)
    return list(seen.values())
