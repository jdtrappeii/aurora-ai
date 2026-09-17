"""Fill store coordinates from their address with Nominatim (OpenStreetMap).

Free, no key, but their usage policy is strict: at most one request a second
and a User-Agent that identifies you (GEOCODER_USER_AGENT). Only stores with an
address and no coordinates are touched, so a hand-set coordinate from GMB or a
stores.csv re-import is never overwritten.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Store

NOMINATIM = "https://nominatim.openstreetmap.org/search"


@dataclass
class GeocodeReport:
    geocoded: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    skipped_no_address: list[str] = field(default_factory=list)
    already_located: int = 0

    def to_dict(self) -> dict:
        return self.__dict__


def geocode_address(http: httpx.Client, address: str, user_agent: str) -> tuple[float, float] | None:
    resp = http.get(NOMINATIM, params={"q": address, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
                    headers={"User-Agent": user_agent})
    if resp.status_code != 200:
        return None
    rows = resp.json()
    if not rows:
        return None
    try:
        return float(rows[0]["lat"]), float(rows[0]["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def geocode_stores(session: Session, http: httpx.Client, user_agent: str, throttle_s: float = 1.1,
                   only_codes: list[str] | None = None) -> GeocodeReport:
    report = GeocodeReport()
    first = True
    for store in session.execute(select(Store).order_by(Store.code)).scalars():
        if only_codes and store.code not in only_codes:
            continue
        if store.latitude is not None and store.longitude is not None:
            report.already_located += 1
            continue
        if not store.address:
            report.skipped_no_address.append(store.code)
            continue
        if not first and throttle_s:
            time.sleep(throttle_s)
        first = False
        hit = geocode_address(http, store.address, user_agent)
        if hit is None:
            report.unresolved.append(store.code)
            continue
        store.latitude, store.longitude = hit
        report.geocoded.append(store.code)
    session.commit()
    return report
