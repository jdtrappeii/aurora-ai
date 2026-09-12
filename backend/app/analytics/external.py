"""External Intelligence Engine (deterministic layer).

What this module does, in the order the spec asks for it:

1. Match external events to stores by explicit store, or by haversine distance
   against the event's affected radius.
2. Build an hourly EXPECTED baseline for any interval: the mean of the same
   weekday+hour over the previous BASELINE_WEEKS weeks, excluding hours that
   sat inside another observed major/severe event or on an adverse-weather day
   (so a past outage or storm does not drag the baseline down), and ignoring
   weeks where the store has no data at all.
3. Compare expected vs actual revenue / transactions / gross profit over the
   event's affected interval.
4. Learn historical effects: group observed events by type (and type+severity)
   and report the mean variance once there are MIN_OBSERVATIONS of them.
5. Classify evidence strength. Correlation is never promoted to causation by
   this code; the levels are explicit and the rules are printed with each finding.
6. Weather: summarise hourly observations to daily conditions, learn each
   store's own response per condition, and use forecasts to project forward.

Nothing here estimates money with a model. Every number is a sum, a mean,
or a ratio of stored rows, and each finding carries the rows it used.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.lines import completed, load_lines
from app.analytics.money import ZERO, D, money, rate, safe_div
from app.analytics.periods import Period
from app.models import ExternalEvent, Promotion, Store, WeatherObservation

BASELINE_WEEKS = 4
MIN_OBSERVATIONS = 3  # events of a kind before a "historical relationship" is claimed
CONSISTENCY_SHARE = Decimal("0.75")  # share of past events that must move the same direction
MATERIALITY = Decimal("0.10")  # |variance| below 10% of expected is reported as noise
DEFAULT_RADIUS_KM = {
    "weather": 40.0,
    "traffic": 5.0,
    "connectivity": 2.0,
    "utility": 3.0,
    "local_event": 15.0,
    "calendar": 1_000.0,
    "competition": 10.0,
    "economic": 50.0,
    "demand": 50.0,
}
SEVERITY_RANK = {"minor": 1, "moderate": 2, "major": 3, "severe": 4}

EVIDENCE_LEVELS = {
    "no_material_variance": "Performance during the event stayed within ±10% of expected.",
    "correlation": "The external condition and a material performance change occurred together. Nothing more is claimed.",
    "historical_relationship": "At least 3 comparable past events moved performance the same direction at least 75% of the time.",
    "likely_contributor": "Timing, location, severity and history all line up, and no promotion started or ended inside the window.",
}


# ---------------------------------------------------------------------------
# Geometry and time buckets
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def hour_buckets(start: datetime, end: datetime) -> list[datetime]:
    """Every hour-floor from start to end inclusive of the hour containing end."""
    cur = start.replace(minute=0, second=0, microsecond=0)
    last = end.replace(minute=0, second=0, microsecond=0)
    if end == last and end > start:  # an event ending exactly on the hour does not touch the next hour
        last = last - timedelta(hours=1)
    out = []
    while cur <= last:
        out.append(cur)
        cur += timedelta(hours=1)
    return out


@dataclass
class HourActual:
    revenue: Decimal = ZERO
    gross_profit: Decimal = ZERO
    transactions: int = 0


def hourly_actuals(session: Session, store_code: str | None, start: datetime, end: datetime) -> dict[datetime, HourActual]:
    period = Period("actuals", start.date(), end.date())
    buckets: dict[datetime, HourActual] = defaultdict(HourActual)
    seen_tickets: dict[datetime, set[int]] = defaultdict(set)
    for ln in completed(load_lines(session, period, store_code)):
        h = ln.sold_at.replace(minute=0, second=0, microsecond=0)
        b = buckets[h]
        b.revenue += ln.revenue
        b.gross_profit += ln.gross_profit
        if ln.sale_id not in seen_tickets[h]:
            seen_tickets[h].add(ln.sale_id)
            b.transactions += 1
    return buckets


# ---------------------------------------------------------------------------
# Event matching
# ---------------------------------------------------------------------------

@dataclass
class MatchedEvent:
    event: ExternalEvent
    store: Store
    distance_km: float | None
    location_weight: Decimal  # 1.0 explicit store; otherwise decays linearly to 0.2 at the radius edge
    radius_km: float | None


def match_events(session: Session, store: Store, observed_only: bool = True) -> list[MatchedEvent]:
    stmt = select(ExternalEvent).order_by(ExternalEvent.start_time)
    if observed_only:
        stmt = stmt.where(ExternalEvent.is_forecast == 0)
    matched = []
    for ev in session.execute(stmt).scalars():
        if ev.store_id is not None:
            if ev.store_id == store.id:
                matched.append(MatchedEvent(ev, store, None, Decimal("1"), ev.affected_radius_km))
            continue
        if store.latitude is None or store.longitude is None or ev.latitude is None or ev.longitude is None:
            continue
        radius = ev.affected_radius_km or DEFAULT_RADIUS_KM.get(ev.event_type, 10.0)
        dist = haversine_km(store.latitude, store.longitude, ev.latitude, ev.longitude)
        if dist <= radius:
            weight = max(Decimal("0.2"), rate(1 - D(dist) / D(radius)))
            matched.append(MatchedEvent(ev, store, round(dist, 2), weight, radius))
    return matched


# ---------------------------------------------------------------------------
# Expected vs actual
# ---------------------------------------------------------------------------

def excluded_hours(matched: list[MatchedEvent], skip_event_id: int | None = None) -> set[datetime]:
    """Hours covered by observed major/severe events, so they do not pollute baselines."""
    hours: set[datetime] = set()
    for m in matched:
        if m.event.id == skip_event_id or SEVERITY_RANK.get(m.event.severity, 0) < SEVERITY_RANK["major"]:
            continue
        hours.update(hour_buckets(m.event.start_time, m.event.end_time))
    return hours


def adverse_weather_hours(session: Session, store: Store) -> set[datetime]:
    """Every hour of a day whose observed weather carried any non-fair tag. A baseline
    is supposed to describe a normal day, so rain, storm, heat and alert days are
    kept out of it (otherwise the third rainy Wednesday is judged against two
    earlier rainy Wednesdays and the learned effect shrinks toward zero)."""
    hours: set[datetime] = set()
    for d, day in daily_weather(session, store).items():
        if day["tags"] != ["fair"]:
            hours.update(hour_buckets(datetime.combine(d, time.min), datetime.combine(d, time(23, 59))))
    return hours


def expected_vs_actual(
    session: Session,
    store_code: str,
    start: datetime,
    end: datetime,
    exclude: set[datetime] | None = None,
    weeks: int = BASELINE_WEEKS,
) -> dict:
    exclude = exclude or set()
    hours = hour_buckets(start, end)
    if not hours:
        return {"expected_revenue": ZERO, "actual_revenue": ZERO, "expected_transactions": ZERO, "actual_transactions": 0,
                "expected_gross_profit": ZERO, "actual_gross_profit": ZERO, "hours": 0, "baseline_samples": 0}
    window_start = hours[0] - timedelta(weeks=weeks)
    actuals = hourly_actuals(session, store_code, window_start, hours[-1] + timedelta(hours=1))
    # A past week counts as a sample only if the store traded at all that day. Weeks
    # before the data starts (or a day the store was closed) are absent, not zero.
    trading_days = {h.date() for h in actuals}

    exp_rev = exp_gp = ZERO
    exp_txn = ZERO
    act_rev = act_gp = ZERO
    act_txn = 0
    samples = 0
    for h in hours:
        past = [h - timedelta(weeks=w) for w in range(1, weeks + 1)]
        past = [p for p in past if p not in exclude and p.date() in trading_days]
        if past:
            samples += len(past)
            exp_rev += sum((actuals[p].revenue for p in past), ZERO) / len(past)
            exp_gp += sum((actuals[p].gross_profit for p in past), ZERO) / len(past)
            exp_txn += D(sum(actuals[p].transactions for p in past)) / len(past)
        a = actuals.get(h, HourActual())
        act_rev += a.revenue
        act_gp += a.gross_profit
        act_txn += a.transactions

    return {
        "expected_revenue": money(exp_rev),
        "actual_revenue": money(act_rev),
        "expected_transactions": rate(exp_txn),
        "actual_transactions": act_txn,
        "expected_gross_profit": money(exp_gp),
        "actual_gross_profit": money(act_gp),
        "hours": len(hours),
        "baseline_samples": samples,
    }


def _variance(block: dict) -> dict:
    var = money(D(block["actual_revenue"]) - D(block["expected_revenue"]))
    pct = rate(safe_div(var, block["expected_revenue"])) if D(block["expected_revenue"]) else None
    txn_var = rate(D(block["actual_transactions"]) - D(block["expected_transactions"]))
    return {"variance": var, "variance_pct": pct, "transaction_variance": txn_var}


# ---------------------------------------------------------------------------
# Historical effects and evidence
# ---------------------------------------------------------------------------

def _history(findings: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[Decimal]] = defaultdict(list)
    for f in findings:
        if f["variance_pct"] is None:
            continue
        groups[f["event_type"]].append(D(f["variance_pct"]))
        groups[f"{f['event_type']}:{f['severity']}"].append(D(f["variance_pct"]))
    out = {}
    for key, values in groups.items():
        n = len(values)
        mean = rate(sum(values, ZERO) / n)
        sign = 1 if mean >= 0 else -1
        same = sum(1 for v in values if (v >= 0) == (sign > 0))
        out[key] = {
            "observations": n,
            "mean_variance_pct": mean,
            "consistent_share": rate(D(same) / n),
            "is_relationship": n >= MIN_OBSERVATIONS and D(same) / n >= CONSISTENCY_SHARE,
        }
    return out


def _promotions_touching(session: Session, start: date, end: date) -> list[str]:
    stmt = select(Promotion.name).where(
        ((Promotion.start_date >= start) & (Promotion.start_date <= end))
        | ((Promotion.end_date >= start) & (Promotion.end_date <= end))
    )
    return [n for (n,) in session.execute(stmt).all()]


def _evidence(f: dict, hist: dict | None, competing: list[str]) -> tuple[str, str]:
    pct = f["variance_pct"]
    if pct is None or abs(D(pct)) < MATERIALITY:
        return "no_material_variance", "low"
    level = "correlation"
    if hist and hist["is_relationship"] and (D(hist["mean_variance_pct"]) >= 0) == (D(pct) >= 0):
        level = "historical_relationship"
        if (
            D(f["location_weight"]) >= Decimal("0.6")
            and SEVERITY_RANK.get(f["severity"], 0) >= SEVERITY_RANK["major"]
            and not competing
        ):
            level = "likely_contributor"
    confidence = {"correlation": "low", "historical_relationship": "medium", "likely_contributor": "high"}[level]
    if D(f["event_confidence"]) < Decimal("0.7") and confidence == "high":
        confidence = "medium"  # the event itself is uncertain
    return level, confidence


def _impact_range(f: dict, hist: dict | None) -> dict | None:
    """Observed variance bounded by what history would have predicted."""
    observed = D(f["variance"])
    if hist is None or hist["observations"] < MIN_OBSERVATIONS:
        return {"low": money(observed), "high": money(observed), "basis": "observed variance only; no history"}
    predicted = D(f["expected_revenue"]) * D(hist["mean_variance_pct"])
    lo, hi = sorted([observed, predicted], key=lambda v: abs(v))
    return {"low": money(lo), "high": money(hi), "basis": f"observed variance vs {hist['observations']} comparable events"}


def event_findings(session: Session, store_code: str, as_of: date) -> dict:
    store = session.execute(select(Store).where(Store.code == store_code)).scalar_one()
    matched = [m for m in match_events(session, store) if m.event.start_time.date() <= as_of]
    wx_hours = adverse_weather_hours(session, store)
    findings = []
    for m in matched:
        ev = m.event
        block = expected_vs_actual(session, store_code, ev.start_time, ev.end_time, excluded_hours(matched, ev.id) | wx_hours)
        f = {
            "event_id": ev.event_id,
            "event_type": ev.event_type,
            "severity": ev.severity,
            "source": ev.source,
            "description": ev.description,
            "store": store.code,
            "start_time": ev.start_time.isoformat(timespec="minutes"),
            "end_time": ev.end_time.isoformat(timespec="minutes"),
            "distance_km": m.distance_km,
            "radius_km": m.radius_km,
            "location_weight": m.location_weight,
            "event_confidence": rate(ev.confidence),
            **block,
        }
        f.update(_variance(block))
        findings.append(f)

    history = _history(findings)
    for f in findings:
        hist = history.get(f"{f['event_type']}:{f['severity']}") or history.get(f["event_type"])
        if hist and hist["observations"] < MIN_OBSERVATIONS:
            hist_for_level = None
        else:
            hist_for_level = hist
        competing = _promotions_touching(session, date.fromisoformat(f["start_time"][:10]), date.fromisoformat(f["end_time"][:10]))
        level, confidence = _evidence(f, hist_for_level, competing)
        f["historical_effect"] = hist
        f["historical_effect_pct"] = hist["mean_variance_pct"] if hist and hist["observations"] >= MIN_OBSERVATIONS else None
        f["competing_internal_factors"] = competing
        f["evidence_level"] = level
        f["evidence_rule"] = EVIDENCE_LEVELS[level]
        f["confidence"] = confidence
        f["estimated_impact"] = _impact_range(f, hist) if level != "no_material_variance" else None

    findings.sort(key=lambda f: (abs(D(f["variance"])), f["start_time"]), reverse=True)

    # Resilience value: what outages have cost, annualised over the history we hold.
    outage = [f for f in findings if f["event_type"] in ("connectivity", "utility") and D(f["variance"]) < 0]
    first = min((datetime.fromisoformat(f["start_time"]) for f in findings), default=None)
    history_days = (as_of - first.date()).days + 1 if first else 0
    outage_loss = money(-sum((D(f["variance"]) for f in outage), ZERO))
    annualised = money(safe_div(outage_loss * 365, history_days)) if history_days else ZERO

    return {
        "as_of": as_of.isoformat(),
        "store": store.code,
        "matched_events": len(findings),
        "findings": findings,
        "historical_effects": history,
        "resilience": {
            "outage_events": len(outage),
            "outage_loss": outage_loss,
            "history_days": history_days,
            "annualised_outage_loss": annualised,
        },
        "rules": {
            "baseline": f"mean of the same weekday+hour over the previous {BASELINE_WEEKS} weeks, excluding hours inside other major/severe events and days with adverse observed weather",
            "materiality": f"{MATERIALITY:%}",
            "min_observations": MIN_OBSERVATIONS,
            "consistency_share": f"{CONSISTENCY_SHARE:%}",
            "levels": EVIDENCE_LEVELS,
        },
    }


# ---------------------------------------------------------------------------
# Weather intelligence
# ---------------------------------------------------------------------------

HEAVY_RAIN_IN = Decimal("0.5")
RAIN_IN = Decimal("0.1")
SNOW_IN = Decimal("1.0")
HEAT_F = Decimal("95")
COLD_F = Decimal("32")
WIND_MPH = Decimal("30")


def daily_weather(session: Session, store: Store, is_forecast: int = 0) -> dict[date, dict]:
    stmt = (
        select(WeatherObservation)
        .where(WeatherObservation.store_id == store.id, WeatherObservation.is_forecast == is_forecast)
        .order_by(WeatherObservation.observed_at)
    )
    days: dict[date, dict] = {}
    for w in session.execute(stmt).scalars():
        d = w.observed_at.date()
        day = days.setdefault(d, {"date": d, "precipitation_in": ZERO, "snowfall_in": ZERO, "max_wind_mph": None,
                                  "min_temp_f": None, "max_temp_f": None, "alerts": set(), "conditions": defaultdict(int), "hours": 0})
        day["hours"] += 1
        day["precipitation_in"] += D(w.precipitation_in)
        day["snowfall_in"] += D(w.snowfall_in)
        if w.wind_mph is not None:
            day["max_wind_mph"] = max(D(w.wind_mph), day["max_wind_mph"] or D(w.wind_mph))
        if w.temperature_f is not None:
            t = D(w.temperature_f)
            day["min_temp_f"] = t if day["min_temp_f"] is None else min(day["min_temp_f"], t)
            day["max_temp_f"] = t if day["max_temp_f"] is None else max(day["max_temp_f"], t)
        if w.alert:
            day["alerts"].add(w.alert)
        if w.condition:
            day["conditions"][w.condition] += 1
    for d, day in days.items():
        day["dominant_condition"] = max(day["conditions"], key=day["conditions"].get) if day["conditions"] else None
        day["conditions"] = dict(day["conditions"])
        day["alerts"] = sorted(day["alerts"])
        day["tags"] = weather_tags(day)
    # pre-storm tag needs the next day
    ordered = sorted(days)
    for i, d in enumerate(ordered[:-1]):
        nxt = days[ordered[i + 1]]
        if ordered[i + 1] - d == timedelta(days=1) and ("alert" in nxt["tags"] or "storm" in nxt["tags"]):
            days[d]["tags"].append("pre_storm")
    return days


def weather_tags(day: dict) -> list[str]:
    tags = []
    if day["snowfall_in"] >= SNOW_IN:
        tags.append("snow")
    if day["precipitation_in"] >= HEAVY_RAIN_IN:
        tags.append("heavy_rain")
    elif day["precipitation_in"] >= RAIN_IN:
        tags.append("rain")
    if day["dominant_condition"] == "storm" or day["conditions"].get("storm", 0) >= 3:
        tags.append("storm")
    if day["max_temp_f"] is not None and day["max_temp_f"] >= HEAT_F:
        tags.append("extreme_heat")
    if day["min_temp_f"] is not None and day["min_temp_f"] <= COLD_F:
        tags.append("extreme_cold")
    if day["max_wind_mph"] is not None and day["max_wind_mph"] >= WIND_MPH:
        tags.append("high_wind")
    if day["alerts"]:
        tags.append("alert")
    if not tags:
        tags.append("fair")
    return tags


def _daily_block(session: Session, store_code: str, d: date, exclude: set[datetime]) -> dict:
    start = datetime.combine(d, time.min)
    end = datetime.combine(d, time(23, 59, 59))
    block = expected_vs_actual(session, store_code, start, end, exclude)
    block.update(_variance(block))
    return block


def weather_intelligence(session: Session, store_code: str, as_of: date) -> dict:
    store = session.execute(select(Store).where(Store.code == store_code)).scalar_one()
    days = daily_weather(session, store)
    matched = match_events(session, store)
    exclude = excluded_hours(matched) | adverse_weather_hours(session, store)

    # Observed days with sales inside the history we hold; skip the first baseline window.
    first_day = min(days) if days else None
    daily_rows = []
    per_tag: dict[str, list[Decimal]] = defaultdict(list)
    for d in sorted(days):
        if d > as_of or first_day is None or d < first_day + timedelta(weeks=BASELINE_WEEKS):
            continue
        block = _daily_block(session, store_code, d, exclude)
        if D(block["expected_revenue"]) == ZERO:
            continue
        row = {"date": d.isoformat(), "tags": days[d]["tags"], "alerts": days[d]["alerts"],
               "precipitation_in": rate(days[d]["precipitation_in"]), "snowfall_in": rate(days[d]["snowfall_in"]),
               "max_temp_f": days[d]["max_temp_f"], "min_temp_f": days[d]["min_temp_f"], "max_wind_mph": days[d]["max_wind_mph"],
               **block}
        daily_rows.append(row)
        for tag in days[d]["tags"]:
            per_tag[tag].append(D(block["variance_pct"]))

    effects = []
    for tag, values in per_tag.items():
        n = len(values)
        mean = rate(sum(values, ZERO) / n)
        sign_share = rate(D(sum(1 for v in values if (v >= 0) == (mean >= 0))) / n)
        effects.append({
            "condition": tag,
            "observations": n,
            "mean_variance_pct": mean,
            "consistent_share": sign_share,
            "evidence_level": "historical_relationship" if n >= MIN_OBSERVATIONS and sign_share >= CONSISTENCY_SHARE and abs(mean) >= MATERIALITY
            else ("correlation" if abs(mean) >= MATERIALITY else "no_material_variance"),
        })
    effects.sort(key=lambda e: (-e["observations"], e["condition"]))
    daily_rows.sort(key=lambda r: r["date"], reverse=True)
    return {"as_of": as_of.isoformat(), "store": store.code, "days_analysed": len(daily_rows), "effects": effects, "days": daily_rows}


# ---------------------------------------------------------------------------
# Proactive forecasting
# ---------------------------------------------------------------------------

def proactive_forecast(session: Session, store_code: str, as_of: date, horizon_days: int = 7) -> dict:
    store = session.execute(select(Store).where(Store.code == store_code)).scalar_one()
    learned = {e["condition"]: e for e in weather_intelligence(session, store_code, as_of)["effects"]}
    events_hist = event_findings(session, store_code, as_of)["historical_effects"]
    forecast_days = daily_weather(session, store, is_forecast=1)
    upcoming_events = [
        m for m in match_events(session, store, observed_only=False)
        if m.event.is_forecast == 1 or m.event.start_time.date() > as_of
    ]

    exclude = excluded_hours(match_events(session, store)) | adverse_weather_hours(session, store)
    out = []
    for i in range(1, horizon_days + 1):
        d = as_of + timedelta(days=i)
        start, end = datetime.combine(d, time.min), datetime.combine(d, time(23, 59, 59))
        base = expected_vs_actual(session, store_code, start, end, exclude)
        expected = D(base["expected_revenue"])
        factors, adj_low, adj_high = [], ZERO, ZERO
        wx = forecast_days.get(d)
        if wx:
            for tag in wx["tags"]:
                eff = learned.get(tag)
                if eff and eff["evidence_level"] in ("historical_relationship", "correlation"):
                    e = D(eff["mean_variance_pct"])
                    factors.append({"kind": "weather", "condition": tag, "effect_pct": e, "evidence_level": eff["evidence_level"],
                                    "observations": eff["observations"]})
                    adj_low += min(e, ZERO) if eff["evidence_level"] == "correlation" else e
                    adj_high += max(e, ZERO) if eff["evidence_level"] == "correlation" else e
                elif tag != "fair":
                    factors.append({"kind": "weather", "condition": tag, "effect_pct": None, "evidence_level": "insufficient_history",
                                    "observations": eff["observations"] if eff else 0})
        for m in upcoming_events:
            if m.event.start_time.date() <= d <= m.event.end_time.date():
                hist = events_hist.get(f"{m.event.event_type}:{m.event.severity}") or events_hist.get(m.event.event_type)
                e = D(hist["mean_variance_pct"]) if hist and hist["observations"] >= MIN_OBSERVATIONS else None
                factors.append({"kind": m.event.event_type, "event_id": m.event.event_id, "description": m.event.description,
                                "severity": m.event.severity, "effect_pct": e,
                                "evidence_level": "historical_relationship" if e is not None and hist["is_relationship"] else ("correlation" if e is not None else "insufficient_history"),
                                "observations": hist["observations"] if hist else 0})
                if e is not None:
                    adj_low += min(e, ZERO)
                    adj_high += max(e, ZERO)
        out.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%A"),
            "expected_revenue_baseline": money(expected),
            "projected_low": money(expected * (1 + adj_low)),
            "projected_high": money(expected * (1 + adj_high)),
            "weather": {"tags": wx["tags"], "alerts": wx["alerts"], "max_temp_f": wx["max_temp_f"], "precipitation_in": rate(wx["precipitation_in"])} if wx else None,
            "factors": factors,
            "note": "Forecasts and scheduled events, not observed facts. Ranges reflect learned store-specific effects only where history exists.",
        })
    return {"as_of": as_of.isoformat(), "store": store.code, "horizon_days": horizon_days, "days": out}
