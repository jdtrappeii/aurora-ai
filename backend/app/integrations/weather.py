"""Weather from two free, keyless sources into weather_observations (and
weather alerts into external_events).

  Open-Meteo   hourly temperature, precipitation, snowfall, wind and a WMO
               weather code. The forecast endpoint covers the last 92 days plus
               16 days ahead; the archive endpoint (ERA5) covers 1940 to about
               five days ago. `weather_sync` picks per range so a two-year
               backfill and a nightly refresh use the same command.
  NWS          api.weather.gov alerts for the store's point: active ones for
               the forecast window and historical ones for the backfill. Each
               alert stamps the hourly rows it covers (`alert`, which the engine
               turns into an "alert" day tag) and becomes a weather event
               explicit to the store, severity from NWS (Extreme -> severe,
               Severe -> major, Moderate -> moderate, Minor -> minor).

Units are requested in Fahrenheit, inches and mph so no conversion happens
here. Hours after the sync time are stored with is_forecast=1; a later sync
writes the observed value as a separate is_forecast=0 row, so the forecast
history is kept for skill checks and the engine reads observations only.

Open-Meteo is free for non-commercial use up to 10,000 calls a day and asks
for attribution (CC BY 4.0); NWS requires a User-Agent that identifies you.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.money import D
from app.importers.csv_importer import ImportResult
from app.integrations.events.common import EventDraft, ProviderError, get_json
from app.models import Store, WeatherObservation

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
NWS_ALERTS_URL = "https://api.weather.gov/alerts"
HOURLY = ["temperature_2m", "precipitation", "snowfall", "wind_speed_10m", "weather_code"]
ARCHIVE_LAG_DAYS = 5      # ERA5 finalises about five days behind
FORECAST_PAST_DAYS = 92   # how far back the forecast endpoint reaches
FORECAST_DAYS = 16
SOURCE_OM = "open-meteo"
SOURCE_NWS = "nws"

NWS_SEVERITY = {"extreme": "severe", "severe": "major", "moderate": "moderate", "minor": "minor", "unknown": "moderate"}


def condition_for(code: int | None) -> str | None:
    """WMO weather code -> Aurora condition (clear|cloudy|rain|storm|snow|fog)."""
    if code is None:
        return None
    if code in (0, 1):
        return "clear"
    if code in (2, 3):
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if code >= 95:
        return "storm"
    return None


@dataclass
class HourRow:
    observed_at: datetime  # store-local, naive
    temperature_f: Decimal | None
    precipitation_in: Decimal
    snowfall_in: Decimal
    wind_mph: Decimal | None
    condition: str | None
    alert: str | None = None


@dataclass
class Alert:
    id: str
    event: str            # "Tropical Storm Warning"
    severity: str         # Aurora severity
    headline: str | None
    onset: datetime       # store-local, naive
    ends: datetime
    nws_severity: str


# ---------- Open-Meteo ----------

def _dec(v) -> Decimal | None:
    return None if v is None else D(v)


def _parse_hourly(payload: dict) -> list[HourRow]:
    h = payload.get("hourly") or {}
    times = h.get("time") or []
    get = lambda k: h.get(k) or [None] * len(times)
    rows = []
    for i, t in enumerate(times):
        rows.append(HourRow(
            observed_at=datetime.fromisoformat(t),
            temperature_f=_dec(get("temperature_2m")[i]),
            precipitation_in=_dec(get("precipitation")[i]) or Decimal("0"),
            snowfall_in=_dec(get("snowfall")[i]) or Decimal("0"),
            wind_mph=_dec(get("wind_speed_10m")[i]),
            condition=condition_for(get("weather_code")[i]),
        ))
    return rows


def _params(lat: float, lon: float, tz: str) -> dict:
    return {
        "latitude": lat, "longitude": lon, "hourly": ",".join(HOURLY), "timezone": tz,
        "temperature_unit": "fahrenheit", "precipitation_unit": "inch", "wind_speed_unit": "mph",
    }


def fetch_open_meteo(http: httpx.Client, lat: float, lon: float, tz: str, start: date, end: date,
                     today: date, forecast_url: str = FORECAST_URL, archive_url: str = ARCHIVE_URL) -> tuple[list[HourRow], dict]:
    """Hourly rows for [start, end]. Archive for the old part, forecast endpoint
    (past_days + forecast_days) for the recent part; the two never overlap."""
    stats = {"archive_calls": 0, "forecast_calls": 0}
    rows: list[HourRow] = []
    archive_end = min(end, today - timedelta(days=ARCHIVE_LAG_DAYS))
    recent_start = max(start, today - timedelta(days=ARCHIVE_LAG_DAYS - 1))
    if start <= archive_end:
        payload = get_json(http, archive_url, {**_params(lat, lon, tz), "start_date": start.isoformat(), "end_date": archive_end.isoformat()})
        stats["archive_calls"] += 1
        rows.extend(_parse_hourly(payload))
    if recent_start <= end:
        past_days = min(FORECAST_PAST_DAYS, max(0, (today - recent_start).days))
        forecast_days = min(FORECAST_DAYS, max(1, (end - today).days + 1))
        payload = get_json(http, forecast_url, {**_params(lat, lon, tz), "past_days": past_days, "forecast_days": forecast_days})
        stats["forecast_calls"] += 1
        rows.extend(r for r in _parse_hourly(payload) if recent_start <= r.observed_at.date() <= end)
    return rows, stats


# ---------- NWS alerts ----------

def _nws_time(value: str | None, tz: str) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def fetch_nws_alerts(http: httpx.Client, lat: float, lon: float, tz: str, start: date, end: date, today: date,
                     user_agent: str, url: str = NWS_ALERTS_URL) -> list[Alert]:
    """Historical alerts for the window (NWS keeps them) plus currently active ones."""
    headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
    point = f"{lat:.4f},{lon:.4f}"
    features: list[dict] = []
    if start <= today:
        payload = get_json(http, url, {
            "point": point, "start": f"{start.isoformat()}T00:00:00Z",
            "end": f"{(min(end, today) + timedelta(days=1)).isoformat()}T00:00:00Z", "limit": 500,
        }, headers)
        features.extend(payload.get("features") or [])
    if end >= today:
        payload = get_json(http, f"{url}/active", {"point": point}, headers)
        features.extend(payload.get("features") or [])
    out: dict[str, Alert] = {}
    for f in features:
        p = f.get("properties") or {}
        aid = f.get("id") or p.get("id")
        if not aid or aid in out:
            continue
        onset = _nws_time(p.get("onset") or p.get("effective") or p.get("sent"), tz)
        ends = _nws_time(p.get("ends") or p.get("expires"), tz)
        if onset is None:
            continue
        if ends is None or ends < onset:
            ends = onset + timedelta(hours=6)
        sev = (p.get("severity") or "unknown").lower()
        out[aid] = Alert(
            id=aid, event=p.get("event") or "Weather alert", severity=NWS_SEVERITY.get(sev, "moderate"),
            headline=p.get("headline"), onset=onset, ends=ends, nws_severity=sev,
        )
    return list(out.values())


def stamp_alerts(rows: list[HourRow], alerts: list[Alert]) -> int:
    """Write the strongest alert's event name onto every hour it covers."""
    rank = {"minor": 1, "moderate": 2, "major": 3, "severe": 4}
    stamped = 0
    for r in rows:
        best = None
        for a in alerts:
            if a.onset <= r.observed_at < a.ends and (best is None or rank[a.severity] > rank[best.severity]):
                best = a
        if best:
            r.alert = best.event
            stamped += 1
    return stamped


def alert_events(store: Store, alerts: list[Alert], today: date) -> list[EventDraft]:
    return [EventDraft(
        event_id=f"nws:{store.code}:{a.id.rsplit('/', 1)[-1]}",
        event_type="weather", source=SOURCE_NWS, start_time=a.onset, end_time=a.ends, severity=a.severity,
        description=a.headline or a.event, store_code=store.code, source_reference=a.id,
        is_forecast=1 if a.onset.date() > today else 0,
        metadata={"event": a.event, "nws_severity": a.nws_severity},
    ) for a in alerts]


# ---------- persistence ----------

def upsert_observations(session: Session, store: Store, rows: list[HourRow], now_local: datetime, source: str) -> ImportResult:
    res = ImportResult(f"weather:{store.code}")
    if not rows:
        return res
    lo, hi = min(r.observed_at for r in rows), max(r.observed_at for r in rows)
    existing = {
        (w.observed_at, w.is_forecast): w
        for w in session.execute(select(WeatherObservation).where(
            WeatherObservation.store_id == store.id, WeatherObservation.observed_at >= lo, WeatherObservation.observed_at <= hi,
        )).scalars()
    }
    for r in rows:
        is_forecast = 1 if r.observed_at > now_local else 0
        values = dict(temperature_f=r.temperature_f, precipitation_in=r.precipitation_in, snowfall_in=r.snowfall_in,
                      wind_mph=r.wind_mph, condition=r.condition, alert=r.alert, source=source)
        obs = existing.get((r.observed_at, is_forecast))
        if obs is None:
            obs = WeatherObservation(store_id=store.id, observed_at=r.observed_at, is_forecast=is_forecast, **values)
            session.add(obs)
            existing[(r.observed_at, is_forecast)] = obs
            res.inserted += 1
        else:
            for k, v in values.items():
                setattr(obs, k, v)
            res.updated += 1
    session.commit()
    return res


@dataclass
class WeatherReport:
    stores: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    results: list[ImportResult] = field(default_factory=list)
    alerts: dict = field(default_factory=dict)
    calls: dict = field(default_factory=lambda: {"archive_calls": 0, "forecast_calls": 0, "nws_calls": 0})
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"stores": self.stores, "skipped_no_coordinates": self.skipped, "alerts": self.alerts, "calls": self.calls,
                "results": [r.to_dict() for r in self.results], "warnings": self.warnings}


def weather_sync(session: Session, http: httpx.Client, start: date, end: date, user_agent: str,
                 store_codes: list[str] | None = None, include_alerts: bool = True, now: datetime | None = None,
                 forecast_url: str = FORECAST_URL, archive_url: str = ARCHIVE_URL, nws_url: str = NWS_ALERTS_URL) -> WeatherReport:
    from app.integrations.events.sync import upsert_events

    if end < start:
        raise ValueError("end precedes start")
    now = now or datetime.utcnow()
    today = now.date()
    report = WeatherReport()
    for store in session.execute(select(Store).order_by(Store.code)).scalars():
        if store_codes and store.code not in store_codes:
            continue
        if store.latitude is None or store.longitude is None:
            report.skipped.append(store.code)
            continue
        tz = store.timezone or "UTC"
        now_local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz)).replace(tzinfo=None)
        try:
            rows, stats = fetch_open_meteo(http, store.latitude, store.longitude, tz, start, end, today, forecast_url, archive_url)
        except ProviderError as e:
            report.warnings.append(f"{store.code}: open-meteo: {e}")
            continue
        report.calls["archive_calls"] += stats["archive_calls"]
        report.calls["forecast_calls"] += stats["forecast_calls"]
        if include_alerts:
            try:
                alerts = fetch_nws_alerts(http, store.latitude, store.longitude, tz, start, end, today, user_agent, nws_url)
                report.calls["nws_calls"] += (1 if start <= today else 0) + (1 if end >= today else 0)
                stamped = stamp_alerts(rows, alerts)
                if alerts:
                    report.results.append(upsert_events(session, alert_events(store, alerts, today), f"nws:{store.code}"))
                report.alerts[store.code] = {"alerts": len(alerts), "hours_stamped": stamped, "events": [a.event for a in alerts]}
            except ProviderError as e:
                report.warnings.append(f"{store.code}: nws: {e}")
        report.results.append(upsert_observations(session, store, rows, now_local, SOURCE_OM))
        report.stores.append(store.code)
    return report
