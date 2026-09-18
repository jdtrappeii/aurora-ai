"""The weekly owner report: one structure that every renderer (HTML, text,
JSON) works from. Nothing is computed here that the dashboard does not
already compute; this module chooses, orders and trims.

Sections, in the order an owner reads them:
  headline        the week vs last week vs the four-week average
  market          the state report's read: with or against the market
  stores          every store ranked by gross profit change vs the prior week
  movers          categories and products that moved the most (gross profit)
  promotions      deal autopsies with verdicts, plus what the feed says
  discounts       the codes that cost the most this week
  inventory       cash tied up, dead / slow stock, stockout risk
  outside         external findings this week (weather, outages, events, competition)
  pressure        competitor deal pressure
  next_week       what is coming: calendar, forecast events, weather
  coverage        which sources are populated and which days lack detail
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.comparisons import weekly_comparison
from app.analytics.discounts import discount_report
from app.analytics.external import event_findings, proactive_forecast
from app.analytics.financial import financial_summary
from app.analytics.inventory import inventory_report
from app.analytics.market import competitor_pressure, market_context
from app.analytics.money import D, money, pct_change, rate
from app.analytics.periods import Period, previous_week, week_containing
from app.analytics.products import category_profitability, product_profitability
from app.analytics.promotions import promotion_results
from app.analytics.scope import is_state, scope_label, stores_in_scope
from app.importers.headset import reconcile
from app.models import ExternalEvent, Store, WeatherObservation


def _store_codes(session: Session) -> list[tuple[str, str]]:
    return [(s.code, s.name) for s in session.execute(select(Store).order_by(Store.code)).scalars()]


def store_ranking(session: Session, current: Period, prev: Period, limit: int = 40, scope: str | None = None) -> list[dict]:
    """Every store in scope with sales in either period, ranked by gross-profit change."""
    rows = []
    for code, name in [(s.code, s.name) for s in stores_in_scope(session, scope if is_state(scope) else None)]:
        cur = financial_summary(session, current, code)
        before = financial_summary(session, prev, code)
        if cur.revenue == 0 and before.revenue == 0:
            continue
        rows.append({
            "store": code, "name": name,
            "revenue": cur.revenue, "gross_profit": cur.gross_profit, "gross_margin": cur.gross_margin,
            "transactions": cur.transactions, "discount_rate": cur.discount_rate,
            "previous_revenue": before.revenue, "previous_gross_profit": before.gross_profit,
            "revenue_pct": pct_change(cur.revenue, before.revenue),
            "gross_profit_pct": pct_change(cur.gross_profit, before.gross_profit),
            "gross_profit_delta": money(cur.gross_profit - before.gross_profit),
        })
    rows.sort(key=lambda r: (r["gross_profit_delta"], r["store"]))
    return rows[:limit]


def _movers(rows_now: list[dict], rows_before: list[dict], key: str, label: str, n: int = 5) -> dict:
    before = {r[key]: r for r in rows_before}
    moves = []
    for r in rows_now:
        b = before.get(r[key])
        delta = r["gross_profit"] - (b["gross_profit"] if b else Decimal("0"))
        moves.append({label: r[key], "name": r.get("product") or r.get("category"), "gross_profit": r["gross_profit"],
                      "previous_gross_profit": b["gross_profit"] if b else Decimal("0.00"), "delta": money(delta),
                      "pct": pct_change(r["gross_profit"], b["gross_profit"]) if b else None, "revenue": r["revenue"]})
    for r in rows_before:
        if r[key] not in {m[label] for m in moves}:
            moves.append({label: r[key], "name": r.get("product") or r.get("category"), "gross_profit": Decimal("0.00"),
                          "previous_gross_profit": r["gross_profit"], "delta": money(-r["gross_profit"]), "pct": Decimal("-1.0000"), "revenue": Decimal("0.00")})
    moves.sort(key=lambda m: m["delta"])
    return {"down": moves[:n], "up": list(reversed(moves[-n:]))}


def weekly_report(session: Session, as_of: date, store_code: str | None = None) -> dict:
    current = week_containing(as_of)
    if as_of < current.end:
        current = Period("current_week", current.start, as_of)
    prev = previous_week(week_containing(as_of))
    if as_of < week_containing(as_of).end:  # partial: same weekdays of last week
        prev = Period("previous_week", prev.start, prev.start + timedelta(days=(as_of - current.start).days))
    weekly = weekly_comparison(session, as_of, store_code)

    inv = inventory_report(session, as_of, store_code)
    items = inv.pop("items")
    watch = [i for i in items if i["status"] in ("dead", "slow")][:8]
    stockout = sorted((i for i in items if i["days_of_supply"] is not None and i["days_of_supply"] < 7 and i["status"] != "dead"),
                      key=lambda i: i["days_of_supply"])[:8]

    promos = [p for p in promotion_results(session, store_code)
              if p["start_date"] <= current.end.isoformat() and p["end_date"] >= (current.start - timedelta(days=28)).isoformat()]

    findings_block = None
    forecast_block = None
    codes = [store_code] if store_code and not is_state(store_code) else [s.code for s in stores_in_scope(session, store_code)]
    findings, forecast_days = [], []
    for code in codes[:60]:
        ef = event_findings(session, code, as_of)
        for f in ef["findings"]:
            if f["evidence_level"] != "no_material_variance" and current.start.isoformat() <= f["start_time"][:10] <= current.end.isoformat():
                findings.append({"store": code, **f})
        if store_code and not is_state(store_code):
            findings_block = ef["resilience"]
            forecast_days = proactive_forecast(session, code, as_of, 7)["days"]
    findings.sort(key=lambda f: (-abs(D(f["variance"])), f["store"]))

    next_start, next_end = current.end + timedelta(days=1), current.end + timedelta(days=7)
    upcoming = session.execute(
        select(ExternalEvent).where(ExternalEvent.start_time >= next_start, ExternalEvent.start_time < next_end + timedelta(days=1))
        .order_by(ExternalEvent.start_time)
    ).scalars().all()
    upcoming_rows = [{"date": e.start_time.date().isoformat(), "type": e.event_type, "severity": e.severity, "description": e.description, "source": e.source}
                     for e in upcoming if e.event_type != "traffic"][:15]

    rec = reconcile(session, store_code)
    missing = [r for r in rec if r["coverage"] == "missing" and current.start.isoformat() <= r["date"] <= current.end.isoformat()]
    weather_rows = session.execute(select(WeatherObservation.id).limit(1)).first()

    cats_now = category_profitability(session, current, store_code)
    cats_before = category_profitability(session, prev, store_code)
    prods_now = product_profitability(session, current, store_code)
    prods_before = product_profitability(session, prev, store_code)

    cw, pw = weekly["current_week"], weekly["previous_week"]
    market = market_context(session, as_of)
    headline_read = _headline(cw, pw, weekly["vs_previous_week"], market)

    return {
        "as_of": as_of.isoformat(),
        "store": store_code,
        "store_name": scope_label(session, store_code),
        "period": current.to_dict(),
        "previous_period": prev.to_dict(),
        "is_partial": weekly["is_partial"],
        "comparison_basis": weekly["comparison_basis"],
        "headline": {"read": headline_read, "current": cw, "previous": pw, "four_week_average": weekly["four_week_average"],
                     "vs_previous_week": weekly["vs_previous_week"], "vs_four_week_average": weekly["vs_four_week_average"]},
        "market": market,
        "stores": store_ranking(session, current, prev, scope=store_code) if (not store_code or is_state(store_code)) else [],
        "movers": {"categories": _movers(cats_now, cats_before, "category", "category"),
                   "products": _movers(prods_now, prods_before, "sku", "sku")},
        "promotions": promos[:10],
        "discounts": discount_report(session, current, store_code, 8),
        "inventory": {**inv, "watch": watch, "stockout_risk": stockout},
        "outside": {"findings": findings[:12], "resilience": findings_block},
        "pressure": competitor_pressure(session, current, limit=8),
        "next_week": {"start": next_start.isoformat(), "end": next_end.isoformat(), "events": upcoming_rows, "forecast": forecast_days},
        "coverage": {
            "sales_days_missing_detail": [f"{r['store']} {r['date']}" for r in missing],
            "weather": bool(weather_rows),
            "market": market is not None,
            "promotions": len(promos),
            "stores_reporting": len(store_ranking(session, current, prev, scope=store_code)) if (not store_code or is_state(store_code)) else 1,
        },
    }


def _headline(cw: dict, pw: dict, deltas: dict, market: dict | None) -> str:
    rev = deltas["revenue"]["pct"]
    gp = deltas["gross_profit"]["pct"]
    if rev is None:
        return "First week with data: no prior week to compare against."
    parts = [f"Revenue ${money(cw['revenue']):,.0f} ({_s(rev)} vs last week), gross profit ${money(cw['gross_profit']):,.0f} ({_s(gp)})."]
    gm_delta = D(cw["gross_margin"]) - D(pw["gross_margin"])
    if abs(gm_delta) >= Decimal("0.005"):
        parts.append(f"Margin {'up' if gm_delta > 0 else 'down'} {abs(gm_delta) * 100:.1f} points to {D(cw['gross_margin']) * 100:.1f}%.")
    dr_delta = D(cw["discount_rate"]) - D(pw["discount_rate"])
    if abs(dr_delta) >= Decimal("0.01"):
        parts.append(f"Discounting {'rose' if dr_delta > 0 else 'fell'} {abs(dr_delta) * 100:.1f} points to {D(cw['discount_rate']) * 100:.1f}% of gross.")
    if market and market.get("read"):
        parts.append(market["read"])
    return " ".join(parts)


def _s(v) -> str:
    return "n/a" if v is None else f"{'+' if D(v) >= 0 else ''}{D(v) * 100:.1f}%"
