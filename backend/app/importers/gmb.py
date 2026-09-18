"""Google Business Profile locations export -> stores.

The export ("Ungrouped_locations-....csv" from Business Profile Manager)
carries the store code, business name, street address, locality, state,
postal code, phone, weekly hours and opening date, but no coordinates. Rows
are matched to existing stores (from Headset) by postal code, then by the
locality appearing in the store name; unmatched rows are reported, never
guessed. It fills address, state, gmb_code, phone, opening date and hours,
and never overwrites coordinates. Run `geocode-stores` afterwards: the
addresses here are the ones Nominatim resolves best.
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.csv_importer import ImportResult, _read_rows
from app.integrations.sheets import cell_date, cell_str
from app.models import Store

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _zip5(value: str | None) -> str | None:
    m = re.search(r"\d{5}", value or "")
    return m.group() if m else None


def _norm(text: str | None) -> str:
    return " ".join((text or "").casefold().replace("-", " ").split())


def match_store(stores: list[Store], zip5: str | None, locality: str | None, state: str | None) -> Store | None:
    if zip5:
        hits = [s for s in stores if _zip5(s.address) == zip5]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1 and locality:
            narrowed = [s for s in hits if _norm(locality) in _norm(s.name)]
            if len(narrowed) == 1:
                return narrowed[0]
    if locality:
        hits = [s for s in stores if _norm(locality) in _norm(s.name) and (not state or not s.state or s.state == state)]
        if len(hits) == 1:
            return hits[0]
    return None


def import_gmb_locations(session: Session, source) -> ImportResult:
    res = ImportResult("gmb_locations")
    rows = _read_rows(source)
    stores = session.execute(select(Store)).scalars().all()
    claimed: set[int] = set()
    for i, r in enumerate(rows, start=2):
        if (r.get("status") or "").strip().casefold() not in ("", "published", "verified", "open"):
            res.skipped += 1
            continue
        name = cell_str(r.get("business name"))
        zip5 = _zip5(r.get("postal code"))
        locality = cell_str(r.get("locality"))
        state = (cell_str(r.get("administrative area")) or "").upper() or None
        store = match_store([s for s in stores if s.id not in claimed], zip5, locality, state)
        if store is None:
            res.errors.append(f"row {i}: no store matched {name!r} ({locality}, {state} {zip5})")
            res.skipped += 1
            continue
        claimed.add(store.id)
        line1 = cell_str(r.get("address line 1"))
        address = ", ".join(p for p in (line1, locality, f"{state or ''} {zip5 or ''}".strip()) if p) or None
        hours = {}
        for d in DAYS:
            v = cell_str(r.get(f"{d} hours"))
            if v:
                hours[d[:3]] = v
        store.address = address or store.address
        store.state = state or store.state
        store.gmb_code = cell_str(r.get("store code")) or store.gmb_code
        store.phone = cell_str(r.get("primary phone")) or store.phone
        store.opened_on = cell_date(r.get("opening date")) or store.opened_on
        store.hours = json.dumps(hours) if hours else store.hours
        res.updated += 1
    session.commit()
    return res
