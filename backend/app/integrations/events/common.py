from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import httpx

from app.analytics.external import haversine_km


@dataclass
class EventDraft:
    """A provider's view of one event, in the shape of the external_events row."""

    event_id: str
    event_type: str  # weather|traffic|connectivity|utility|local_event|calendar|competition|economic|demand
    source: str
    start_time: datetime  # naive, local to the store area
    end_time: datetime
    severity: str = "moderate"  # minor|moderate|major|severe
    description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    affected_radius_km: float | None = None
    store_code: str | None = None
    confidence: Decimal = Decimal("1")
    source_reference: str | None = None
    is_forecast: int = 0
    metadata: dict = field(default_factory=dict)
    venue: str | None = None  # for cross-provider de-duplication

    @property
    def local_date(self):
        return self.start_time.date()


@dataclass
class StorePoint:
    code: str
    name: str
    latitude: float
    longitude: float
    timezone: str | None = None


def nearest_store(stores: list[StorePoint], lat: float, lon: float) -> tuple[StorePoint | None, float]:
    best, best_km = None, math.inf
    for s in stores:
        km = haversine_km(s.latitude, s.longitude, lat, lon)
        if km < best_km:
            best, best_km = s, km
    return best, best_km


def geohash(lat: float, lon: float, precision: int = 9) -> str:
    """Standard geohash; Ticketmaster's geoPoint parameter takes one."""
    base32 = "0123456789bcdefghjkmnpqrstuvwxyz"
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    bits, ch, even, out = 0, 0, True, []
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch = ch * 2 + 1
                lon_lo = mid
            else:
                ch *= 2
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch = ch * 2 + 1
                lat_lo = mid
            else:
                ch *= 2
                lat_hi = mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(base32[ch])
            bits, ch = 0, 0
    return "".join(out)


def norm_venue(name: str | None) -> str:
    return " ".join((name or "").casefold().replace("-", " ").split())


def dedupe(primary: list[EventDraft], secondary: list[EventDraft]) -> tuple[list[EventDraft], int]:
    """Drop secondary events that share a venue and local date with a primary one
    (the same concert listed by two ticket sellers). Returns kept + dropped count."""
    seen = {(norm_venue(e.venue), e.local_date) for e in primary if e.venue}
    kept, dropped = [], 0
    for e in secondary:
        if e.venue and (norm_venue(e.venue), e.local_date) in seen:
            dropped += 1
        else:
            kept.append(e)
    return kept, dropped


class ProviderError(RuntimeError):
    pass


def get_json(client: httpx.Client, url: str, params: dict, headers: dict | None = None) -> dict | list:
    resp = client.get(url, params=params, headers=headers or {})
    if resp.status_code >= 400:
        raise ProviderError(f"{url}: HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        raise ProviderError(f"{url}: non-JSON response")
