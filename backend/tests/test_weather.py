"""Open-Meteo + NWS weather adapter against httpx.MockTransport."""
from datetime import date, datetime
from decimal import Decimal

import httpx
from sqlalchemy import select

from app.analytics.external import daily_weather, weather_tags
from app.integrations.weather import (
    Alert,
    HourRow,
    condition_for,
    fetch_nws_alerts,
    fetch_open_meteo,
    stamp_alerts,
    weather_sync,
)
from app.models import ExternalEvent, Store, WeatherObservation

TODAY = date(2026, 9, 18)
NOW = datetime(2026, 9, 18, 16, 0)  # UTC; 12:00 in Pace (Central)


def om_payload(day: str, hours: int = 24, temp=88.0, precip=0.0, code=1, wind=8.0):
    times = [f"{day}T{h:02d}:00" for h in range(hours)]
    n = len(times)
    return {"hourly": {"time": times, "temperature_2m": [temp] * n, "precipitation": [precip] * n,
                       "snowfall": [0.0] * n, "wind_speed_10m": [wind] * n, "weather_code": [code] * n}}


def nws_feature(aid, event, severity, onset, ends, headline=None):
    return {"id": aid, "properties": {"id": aid, "event": event, "severity": severity, "onset": onset, "ends": ends, "headline": headline}}


def mock(router):
    def handler(req):
        status, body = router(req)
        return httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_wmo_condition_mapping():
    assert [condition_for(c) for c in (0, 2, 45, 61, 82, 71, 86, 95, 99, None, 20)] == \
        ["clear", "cloudy", "fog", "rain", "rain", "snow", "snow", "storm", "storm", None, None]


def test_open_meteo_splits_archive_and_forecast():
    calls = []

    def router(req):
        calls.append((req.url.host, dict(req.url.params)))
        if "archive" in req.url.host:
            return 200, om_payload("2026-09-01", 2)
        return 200, {"hourly": {"time": ["2026-09-13T00:00", "2026-09-17T12:00", "2026-09-20T09:00", "2026-09-26T00:00"],
                                "temperature_2m": [80, 81, 82, 83], "precipitation": [0, 0.6, 0, 0], "snowfall": [0, 0, 0, 0],
                                "wind_speed_10m": [5, 6, 7, 8], "weather_code": [0, 63, 3, 95]}}

    with mock(router) as http:
        rows, stats = fetch_open_meteo(http, 30.6, -87.16, "America/Chicago", date(2026, 9, 1), date(2026, 9, 25), TODAY,
                                       "https://api.test/v1/forecast", "https://archive.test/v1/archive")
    assert stats == {"archive_calls": 1, "forecast_calls": 1}
    archive_params = calls[0][1]
    assert (archive_params["start_date"], archive_params["end_date"]) == ("2026-09-01", "2026-09-13")  # today - 5
    assert archive_params["temperature_unit"] == "fahrenheit" and archive_params["precipitation_unit"] == "inch"
    forecast_params = calls[1][1]
    assert (forecast_params["past_days"], forecast_params["forecast_days"]) == ("4", "8")  # 9/14..9/17 back, 9/18..9/25 ahead
    # the forecast payload is clipped to [today-4, end]: 9/13 (archive's) and 9/26 (past end) are dropped
    assert [r.observed_at for r in rows] == [datetime(2026, 9, 1, 0), datetime(2026, 9, 1, 1), datetime(2026, 9, 17, 12), datetime(2026, 9, 20, 9)]
    assert rows[2].condition == "rain" and rows[2].precipitation_in == Decimal("0.6")


def test_open_meteo_forecast_only_when_range_is_recent():
    with mock(lambda req: (200, om_payload("2026-09-18", 3))) as http:
        rows, stats = fetch_open_meteo(http, 30.6, -87.16, "America/Chicago", date(2026, 9, 16), date(2026, 9, 24), TODAY)
    assert stats == {"archive_calls": 0, "forecast_calls": 1} and len(rows) == 3


def test_nws_alerts_parse_and_stamp():
    seen = []

    def router(req):
        seen.append((req.url.path, dict(req.url.params), req.headers.get("user-agent")))
        if req.url.path.endswith("/active"):
            return 200, {"features": [nws_feature("urn:a:2", "Heat Advisory", "Moderate", "2026-09-18T15:00:00+00:00", "2026-09-19T00:00:00+00:00")]}
        return 200, {"features": [
            nws_feature("urn:a:1", "Tropical Storm Warning", "Severe", "2026-09-10T06:00:00+00:00", "2026-09-11T06:00:00+00:00", "TS Warning for Santa Rosa County"),
            nws_feature("urn:a:2", "Heat Advisory", "Moderate", "2026-09-18T15:00:00+00:00", "2026-09-19T00:00:00+00:00"),  # duplicate of active
            {"id": "urn:a:3", "properties": {"event": "No onset", "severity": "Minor"}},
        ]}

    with mock(router) as http:
        alerts = fetch_nws_alerts(http, 30.6, -87.16, "America/Chicago", date(2026, 9, 1), date(2026, 9, 25), TODAY, "aurora-test (x@y.z)")
    assert seen[0][2] == "aurora-test (x@y.z)" and seen[0][1]["point"] == "30.6000,-87.1600"
    assert [(a.event, a.severity) for a in alerts] == [("Tropical Storm Warning", "major"), ("Heat Advisory", "moderate")]
    ts = alerts[0]
    assert ts.onset == datetime(2026, 9, 10, 1, 0) and ts.ends == datetime(2026, 9, 11, 1, 0)  # UTC -> Central
    rows = [HourRow(datetime(2026, 9, 10, h), Decimal(80), Decimal(0), Decimal(0), Decimal(5), "rain") for h in (0, 1, 12)]
    assert stamp_alerts(rows, alerts) == 2
    assert [r.alert for r in rows] == [None, "Tropical Storm Warning", "Tropical Storm Warning"]


def test_weather_sync_end_to_end(session):
    session.add(Store(code="HS10136", name="Pace", latitude=30.6, longitude=-87.16, timezone="America/Chicago"))
    session.add(Store(code="HS0", name="Unlocated"))
    session.commit()

    def router(req):
        host = req.url.host
        if host == "archive.test":
            return 200, om_payload("2026-09-10", 24, temp=84, precip=0.3, code=63)
        if host == "api.test":
            # 9/17 12:00 observed, 9/18 12:00 == now (observed), 9/18 13:00 forecast, 9/19 storm forecast
            return 200, {"hourly": {"time": ["2026-09-17T12:00", "2026-09-18T12:00", "2026-09-18T13:00", "2026-09-19T15:00"],
                                    "temperature_2m": [90, 96, 97, 85], "precipitation": [0, 0, 0, 1.2], "snowfall": [0, 0, 0, 0],
                                    "wind_speed_10m": [5, 6, 7, 35], "weather_code": [1, 1, 2, 95]}}
        if host == "nws.test" and req.url.path.endswith("/active"):
            return 200, {"features": [nws_feature("urn:a:9", "Tropical Storm Watch", "Severe", "2026-09-19T12:00:00+00:00", "2026-09-20T12:00:00+00:00")]}
        if host == "nws.test":
            return 200, {"features": [nws_feature("urn:a:1", "Tropical Storm Warning", "Extreme", "2026-09-10T06:00:00+00:00", "2026-09-11T06:00:00+00:00")]}
        return 500, {}

    with mock(router) as http:
        rep = weather_sync(session, http, date(2026, 9, 10), date(2026, 9, 25), "aurora-test (x@y.z)", now=NOW,
                           forecast_url="https://api.test/v1/forecast", archive_url="https://archive.test/v1/archive", nws_url="https://nws.test/alerts")
    assert rep.stores == ["HS10136"] and rep.skipped == ["HS0"] and rep.warnings == []
    assert rep.calls == {"archive_calls": 1, "forecast_calls": 1, "nws_calls": 2}
    assert rep.alerts["HS10136"]["alerts"] == 2 and rep.alerts["HS10136"]["hours_stamped"] == 24  # 9/10 01:00..23:00 Central + the 9/19 15:00 watch hour

    obs = session.execute(select(WeatherObservation).order_by(WeatherObservation.observed_at, WeatherObservation.is_forecast)).scalars().all()
    assert len(obs) == 28
    by = {(o.observed_at, o.is_forecast): o for o in obs}
    assert by[(datetime(2026, 9, 18, 12), 0)].temperature_f == Decimal("96")   # == now local -> observed
    assert by[(datetime(2026, 9, 18, 13), 1)].condition == "cloudy"            # after now -> forecast
    assert by[(datetime(2026, 9, 19, 15), 1)].condition == "storm"
    assert by[(datetime(2026, 9, 10, 5), 0)].alert == "Tropical Storm Warning" and by[(datetime(2026, 9, 10, 0), 0)].alert is None

    events = {e.event_id: e for e in session.execute(select(ExternalEvent)).scalars()}
    assert set(events) == {"nws:HS10136:1", "nws:HS10136:9"}
    assert events["nws:HS10136:1"].severity == "severe" and events["nws:HS10136:1"].is_forecast == 0
    assert events["nws:HS10136:9"].is_forecast == 1 and events["nws:HS10136:9"].store_id is not None

    # the engine's daily summary picks it up: rain + alert on 9/10, heat on 9/18 (max 97 forecast excluded -> 96 observed)
    store = session.execute(select(Store).where(Store.code == "HS10136")).scalar_one()
    days = daily_weather(session, store)
    d10 = days[date(2026, 9, 10)]
    assert d10["precipitation_in"] == Decimal("7.2") and "alert" in weather_tags(d10) and "heavy_rain" in weather_tags(d10)
    assert weather_tags(days[date(2026, 9, 18)]) == ["heat"]
    fc = daily_weather(session, store, is_forecast=1)
    assert "storm" in weather_tags(fc[date(2026, 9, 19)]) or "heavy_rain" in weather_tags(fc[date(2026, 9, 19)])

    # idempotent
    with mock(router) as http:
        rep2 = weather_sync(session, http, date(2026, 9, 10), date(2026, 9, 25), "aurora-test (x@y.z)", now=NOW,
                            forecast_url="https://api.test/v1/forecast", archive_url="https://archive.test/v1/archive", nws_url="https://nws.test/alerts")
    assert sum(r.inserted for r in rep2.results) == 0 and len(session.execute(select(WeatherObservation)).scalars().all()) == 28


def test_weather_sync_reports_provider_failure(session):
    session.add(Store(code="HS1", name="One", latitude=28.0, longitude=-82.0, timezone="America/New_York"))
    session.commit()
    with mock(lambda req: (503, {"reason": "down"})) as http:
        rep = weather_sync(session, http, date(2026, 9, 17), date(2026, 9, 19), "ua", now=NOW)
    assert rep.stores == [] and "open-meteo" in rep.warnings[0] and "503" in rep.warnings[0]
