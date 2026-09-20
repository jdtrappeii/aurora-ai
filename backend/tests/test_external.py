"""External Intelligence Engine.

The synthetic store sells exactly one A @ 25 (cost 10) every hour 10:00-17:59
for nine weeks, so every hour has revenue 25 and the baseline is trivially
25/hour. Planted deviations then produce exact, hand-checkable variances.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.analytics.external import (
    daily_weather,
    event_findings,
    expected_vs_actual,
    haversine_km,
    hour_buckets,
    proactive_forecast,
    weather_intelligence,
    weather_tags,
)
from tests.conftest import MONDAY, dt

AS_OF = date(2026, 9, 11)  # Friday
START = AS_OF - timedelta(weeks=9)


def steady_store(seed, skip: dict[date, set[int]] | None = None, mult: dict[date, Decimal] | None = None):
    """One sale per hour, 10..17, every day. `skip` removes hours; `mult` scales the ticket count."""
    seed.product("A", "Flower", "10", "25")
    d = START
    while d <= AS_OF:
        for h in range(10, 18):
            if skip and h in skip.get(d, set()):
                continue
            n = int(mult.get(d, Decimal(1)) * 1) if mult else 1
            for _ in range(max(n, 0)):
                seed.sale(dt(d, h, 30), [("A", 1, "25")])
        d += timedelta(days=1)


def test_haversine_known_distance():
    # Orlando -> Tampa is about 124 km
    assert 120 < haversine_km(28.5383, -81.3792, 27.9506, -82.4572) < 130
    assert haversine_km(28.5, -81.3, 28.5, -81.3) == 0


def test_hour_buckets_inclusive_of_partial_end_hour():
    hs = hour_buckets(datetime(2026, 9, 3, 14, 0), datetime(2026, 9, 3, 16, 59))
    assert [h.hour for h in hs] == [14, 15, 16]
    hs = hour_buckets(datetime(2026, 9, 3, 14, 15), datetime(2026, 9, 3, 17, 0))  # ends exactly 17:00
    assert [h.hour for h in hs] == [14, 15, 16]
    hs = hour_buckets(datetime(2026, 9, 3, 14, 0), datetime(2026, 9, 3, 14, 0))
    assert [h.hour for h in hs] == [14]


def test_expected_vs_actual_on_steady_store(session, seed):
    outage_day = date(2026, 9, 3)  # Thursday, 3 hours dark 14:00-16:59
    steady_store(seed, skip={outage_day: {14, 15, 16}})
    seed.commit()
    block = expected_vs_actual(session, "MAIN", datetime(2026, 9, 3, 14, 0), datetime(2026, 9, 3, 16, 59))
    assert block["hours"] == 3
    assert block["baseline_samples"] == 12  # 3 hours x 4 prior weeks
    assert block["expected_revenue"] == Decimal("75.00")
    assert block["actual_revenue"] == Decimal("0.00")
    assert block["expected_transactions"] == Decimal("3.0000")
    assert block["actual_transactions"] == 0
    assert block["expected_gross_profit"] == Decimal("45.00")


def test_event_findings_variance_history_and_evidence(session, seed):
    """Three past outages (each 2 hours, all dark) build a historical relationship;
    the fourth, most recent one is then a likely contributor. A far-away traffic
    incident must not match at all, and a nearby one with no sales change is noise."""
    outages = [date(2026, 8, 6), date(2026, 8, 13), date(2026, 8, 20), date(2026, 9, 3)]  # Thursdays
    steady_store(seed, skip={d: {14, 15} for d in outages})
    for i, d in enumerate(outages):
        seed.event(f"OUT{i}", "utility", dt(d, 14, 0), dt(d, 15, 59), severity="major", description="Power outage")
    seed.event("FAR", "traffic", dt(date(2026, 9, 8), 8, 0), dt(date(2026, 9, 8), 18, 0), store=False,
               lat=27.95, lon=-82.45, radius=5, description="Tampa crash")
    seed.event("NEAR", "traffic", dt(date(2026, 9, 8), 8, 0), dt(date(2026, 9, 8), 18, 0), store=False,
               lat=28.54, lon=-81.38, radius=5, severity="moderate", description="Lane closure, no effect")
    seed.commit()

    rep = event_findings(session, "MAIN", AS_OF)
    ids = {f["event_id"] for f in rep["findings"]}
    assert "FAR" not in ids and "NEAR" in ids and len(ids) == 5

    by_id = {f["event_id"]: f for f in rep["findings"]}
    latest = by_id["OUT3"]
    assert latest["expected_revenue"] == Decimal("50.00")
    assert latest["actual_revenue"] == Decimal("0.00")
    assert latest["variance"] == Decimal("-50.00")
    assert latest["variance_pct"] == Decimal("-1.0000")
    assert latest["baseline_samples"] == 2  # 2 hours x 4 weeks, but 3 of those Thursdays were outages and are excluded
    assert latest["evidence_level"] == "likely_contributor"
    assert latest["confidence"] == "high"
    assert latest["historical_effect_pct"] == Decimal("-1.0000")
    assert latest["estimated_impact"] == {"low": Decimal("-50.00"), "high": Decimal("-50.00"),
                                          "basis": "observed variance vs 4 comparable events"}

    # The earliest outage had no prior comparable events at scoring time, but history is
    # computed over the whole set, so it still shares the relationship.
    assert rep["historical_effects"]["utility"]["observations"] == 4
    assert rep["historical_effects"]["utility"]["is_relationship"] is True

    near = by_id["NEAR"]
    assert near["distance_km"] is not None and near["distance_km"] < 1
    assert near["location_weight"] > Decimal("0.8")
    assert near["evidence_level"] == "no_material_variance"
    assert near["estimated_impact"] is None

    res = rep["resilience"]
    assert res["outage_events"] == 4
    assert res["outage_loss"] == Decimal("200.00")
    assert res["history_days"] == (AS_OF - outages[0]).days + 1
    assert res["annualised_outage_loss"] == Decimal(str(round(Decimal("200.00") * 365 / res["history_days"], 2)))


def test_promotion_in_window_blocks_likely_contributor(session, seed):
    outages = [date(2026, 8, 6), date(2026, 8, 13), date(2026, 8, 20), date(2026, 9, 3)]
    steady_store(seed, skip={d: {14, 15} for d in outages})
    for i, d in enumerate(outages):
        seed.event(f"OUT{i}", "utility", dt(d, 14, 0), dt(d, 15, 59), severity="major")
    seed.promotion("Thursday Deal", date(2026, 9, 3), date(2026, 9, 3), category="Flower")
    seed.commit()
    latest = {f["event_id"]: f for f in event_findings(session, "MAIN", AS_OF)["findings"]}["OUT3"]
    assert latest["competing_internal_factors"] == ["Thursday Deal"]
    assert latest["evidence_level"] == "historical_relationship"
    assert latest["confidence"] == "medium"


def test_weather_tags():
    base = {"precipitation_in": Decimal("0"), "snowfall_in": Decimal("0"), "max_wind_mph": Decimal("5"),
            "min_temp_f": Decimal("70"), "max_temp_f": Decimal("88"), "alerts": [], "conditions": {"clear": 8}, "dominant_condition": "clear"}
    assert weather_tags(base) == ["fair"]
    assert weather_tags({**base, "precipitation_in": Decimal("0.2")}) == ["rain"]
    assert weather_tags({**base, "precipitation_in": Decimal("0.9"), "max_wind_mph": Decimal("45"), "alerts": ["Tropical Storm Warning"]}) == ["heavy_rain", "high_wind", "alert"]
    assert weather_tags({**base, "max_temp_f": Decimal("97")}) == ["extreme_heat"]
    assert weather_tags({**base, "snowfall_in": Decimal("3"), "min_temp_f": Decimal("20")}) == ["snow", "extreme_cold"]


def test_weather_learns_store_specific_response_and_forecasts(session, seed):
    """Heavy-rain days sell half as much (4 of 8 hours dark). Three of them -> a
    historical relationship of -50%; a forecast heavy-rain day next week then
    projects baseline x 0.5."""
    rain_days = [date(2026, 8, 12), date(2026, 8, 26), date(2026, 9, 9)]  # Wednesdays
    steady_store(seed, skip={d: {14, 15, 16, 17} for d in rain_days})
    d = START
    while d <= AS_OF:
        for h in range(0, 24):
            seed.weather(dt(d, h), precip="0.1" if d in rain_days else "0", condition="rain" if d in rain_days else "clear")
        d += timedelta(days=1)
    for h in range(0, 24):  # forecast for next Wednesday: heavy rain
        seed.weather(dt(date(2026, 9, 16), h), precip="0.1", condition="rain", is_forecast=1)
        seed.weather(dt(date(2026, 9, 14), h), precip="0", condition="clear", is_forecast=1)
    seed.commit()

    wx = weather_intelligence(session, "MAIN", AS_OF)
    effects = {e["condition"]: e for e in wx["effects"]}
    assert effects["heavy_rain"]["observations"] == 3
    assert effects["heavy_rain"]["mean_variance_pct"] == Decimal("-0.5000")
    assert effects["heavy_rain"]["consistent_share"] == Decimal("1.0000")
    assert effects["heavy_rain"]["evidence_level"] == "historical_relationship"
    assert effects["fair"]["evidence_level"] == "no_material_variance"

    fc = proactive_forecast(session, "MAIN", AS_OF, 7)
    days = {r["date"]: r for r in fc["days"]}
    wed = days["2026-09-16"]
    assert wed["expected_revenue_baseline"] == Decimal("200.00")
    assert wed["projected_low"] == Decimal("100.00") and wed["projected_high"] == Decimal("100.00")
    assert wed["factors"][0]["condition"] == "heavy_rain"
    mon = days["2026-09-14"]
    assert mon["projected_low"] == mon["projected_high"] == Decimal("200.00")
    assert mon["factors"] == []
