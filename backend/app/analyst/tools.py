"""Read-only tools over Aurora's analytics, bound to one database session.

Each tool returns a JSON string (Decimals as strings) trimmed to what an
answer needs. Scope follows the rest of Aurora: a store code, "state:FL", or
nothing for everything. Dates are ISO (YYYY-MM-DD)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from anthropic import beta_tool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.comparisons import weekly_comparison, weekly_trend
from app.analytics.discounts import discount_report
from app.analytics.external import event_findings, proactive_forecast, weather_intelligence
from app.analytics.inventory import inventory_report
from app.analytics.market import competitor_pressure, market_context
from app.analytics.periods import Period, week_containing
from app.analytics.products import category_profitability, product_profitability
from app.analytics.promotions import promotion_results
from app.analytics.report import store_ranking
from app.analytics.scope import is_state, stores_in_scope
from app.importers.headset import reconcile
from app.models import ExternalEvent, Sale, Store


def _dump(obj) -> str:
    return json.dumps(obj, default=lambda o: str(o) if isinstance(o, Decimal) else (o.isoformat() if hasattr(o, "isoformat") else str(o)))


def _period(start: str | None, end: str | None, as_of: date) -> Period:
    if start and end:
        return Period("custom", date.fromisoformat(start), date.fromisoformat(end))
    return week_containing(as_of)


def build_tools(session: Session, default_scope: str | None, as_of_default: date) -> list:
    """Tools closed over one session. Returned in a stable order (prompt cache)."""

    def scope_of(scope: str | None) -> str | None:
        return scope if scope else default_scope

    def as_of_of(as_of: str | None) -> date:
        return date.fromisoformat(as_of) if as_of else as_of_default

    @beta_tool
    def list_stores() -> str:
        """List every store with its code, name and state, plus the state scopes
        ("state:FL") that other tools accept as `scope`. Call this first when a
        question names a store by name or city."""
        rows = session.execute(select(Store).order_by(Store.state, Store.code)).scalars().all()
        return _dump({
            "default_scope": default_scope,
            "as_of_default": as_of_default,
            "scopes": sorted({f"state:{s.state}" for s in rows if s.state}),
            "stores": [{"code": s.code, "name": s.name, "state": s.state, "has_coordinates": s.latitude is not None} for s in rows],
        })

    @beta_tool
    def weekly_summary(as_of: str | None = None, scope: str | None = None) -> str:
        """The week containing `as_of` (default: latest data) versus the previous
        week and the four-week average: revenue, gross profit, margin, discount
        rate, tickets, average ticket, with deltas. A partial week is compared
        week-to-date against the same weekdays. `scope` = store code, "state:FL"
        or omitted for the default scope."""
        return _dump(weekly_comparison(session, as_of_of(as_of), scope_of(scope)))

    @beta_tool
    def weekly_trend_series(as_of: str | None = None, weeks: int = 12, scope: str | None = None) -> str:
        """One financial summary per week for the last `weeks` weeks (2..52)."""
        return _dump(weekly_trend(session, as_of_of(as_of), max(2, min(52, weeks)), scope_of(scope)))

    @beta_tool
    def store_ranking_for_period(start: str | None = None, end: str | None = None, as_of: str | None = None, scope: str | None = None, limit: int = 40) -> str:
        """Every store in scope ranked by gross-profit change versus the equal
        period before (worst first). Use for "which stores dropped" questions.
        Give start/end or as_of (then the week containing it)."""
        p = _period(start, end, as_of_of(as_of))
        prev = Period("previous", p.start - timedelta(days=p.days), p.start - timedelta(days=1))
        return _dump(store_ranking(session, p, prev, limit=max(1, min(60, limit)), scope=scope_of(scope)))

    @beta_tool
    def categories(start: str | None = None, end: str | None = None, as_of: str | None = None, scope: str | None = None) -> str:
        """Category profitability for a period (revenue share, gross profit, margin, discount rate)."""
        return _dump(category_profitability(session, _period(start, end, as_of_of(as_of)), scope_of(scope)))

    @beta_tool
    def products(start: str | None = None, end: str | None = None, as_of: str | None = None, scope: str | None = None, limit: int = 25, bottom: bool = False) -> str:
        """Top products by gross profit for a period (or the bottom ones with bottom=true)."""
        rows = product_profitability(session, _period(start, end, as_of_of(as_of)), scope_of(scope))
        rows = rows[-limit:] if bottom else rows[:limit]
        return _dump(rows)

    @beta_tool
    def inventory(as_of: str | None = None, scope: str | None = None, status: str | None = None, limit: int = 25) -> str:
        """Inventory position: value at cost, aging, cash tied over 90 days,
        status counts, and the top items by value. `status` filters items to
        dead|slow|hot|normal|out_of_stock. Stockout risk = days_of_supply < 7."""
        rep = inventory_report(session, as_of_of(as_of), scope_of(scope))
        items = rep.pop("items")
        if status:
            items = [i for i in items if i["status"] == status]
        rep["items"] = items[:max(1, min(100, limit))]
        rep["stockout_risk"] = sorted((i for i in items if i["days_of_supply"] is not None and i["days_of_supply"] < 7 and i["status"] != "dead"),
                                      key=lambda i: i["days_of_supply"])[:limit]
        return _dump(rep)

    @beta_tool
    def promotions(scope: str | None = None, name: str | None = None) -> str:
        """Deal autopsies: each promotion versus the 28 days before it, with a
        verdict (profitable, revenue_up_profit_down, unprofitable, no_baseline)
        and, when the promotion has POS discount names, what the aggregate feed
        says it gave away per scheduled day."""
        return _dump(promotion_results(session, scope_of(scope), name))

    @beta_tool
    def discount_codes(start: str | None = None, end: str | None = None, as_of: str | None = None, scope: str | None = None, limit: int = 25) -> str:
        """Discount / promo codes for a period: dollars given away per code, its
        share of all discounting, depth on the items it touched, versus the prior
        equal period. From the aggregate feed."""
        return _dump(discount_report(session, _period(start, end, as_of_of(as_of)), scope_of(scope), max(1, min(200, limit))))

    @beta_tool
    def market(as_of: str | None = None) -> str:
        """State market context from the regulator's weekly report: market volume
        (mg THC) versus ours, our THC and flower share in bps, dispensary counts,
        patients; week over week and versus the four-week average; a one-line read."""
        return _dump(market_context(session, as_of_of(as_of)))

    @beta_tool
    def competitor_deals(start: str | None = None, end: str | None = None, as_of: str | None = None) -> str:
        """Competitor promo pressure: deals observed per operator in the period vs the prior period."""
        return _dump(competitor_pressure(session, _period(start, end, as_of_of(as_of))))

    @beta_tool
    def external_findings(store: str, as_of: str | None = None, only_material: bool = True) -> str:
        """For ONE store: external events (weather, outages, traffic, local
        events, competition, calendar) matched to it, each with expected vs
        actual revenue and tickets, the variance, and an evidence level
        (no_material_variance | correlation | historical_relationship |
        likely_contributor). Also the resilience value of outages."""
        ef = event_findings(session, store, as_of_of(as_of))
        if only_material:
            ef["findings"] = [f for f in ef["findings"] if f["evidence_level"] != "no_material_variance"]
        ef["findings"] = ef["findings"][:40]
        return _dump(ef)

    @beta_tool
    def weather_effects(store: str, as_of: str | None = None) -> str:
        """For ONE store: learned effect of each weather condition (rain, heavy
        rain, storm, heat, cold, wind, alert) on revenue, with observation counts
        and evidence level, plus the last days' weather tags."""
        w = weather_intelligence(session, store, as_of_of(as_of))
        w["days"] = w.get("days", [])[-14:]
        return _dump(w)

    @beta_tool
    def forecast(store: str, as_of: str | None = None, days: int = 7) -> str:
        """For ONE store: the next `days` days' revenue projection from the
        baseline, priced only with effects the store has shown, with the events
        and weather driving each day."""
        return _dump(proactive_forecast(session, store, as_of_of(as_of), max(1, min(14, days))))

    @beta_tool
    def upcoming_events(start: str | None = None, days: int = 14, scope: str | None = None) -> str:
        """Events on the calendar from `start` (default tomorrow) for `days` days:
        holidays, concerts, sports, competitor openings, forecast weather alerts."""
        s = date.fromisoformat(start) if start else as_of_default + timedelta(days=1)
        e = s + timedelta(days=max(1, min(60, days)))
        stmt = select(ExternalEvent).where(ExternalEvent.start_time >= s.isoformat(), ExternalEvent.start_time < e.isoformat()).order_by(ExternalEvent.start_time)
        codes = {st.id for st in stores_in_scope(session, scope_of(scope))} if scope_of(scope) else None
        out = []
        for ev in session.execute(stmt).scalars():
            if codes is not None and ev.store_id is not None and ev.store_id not in codes:
                continue
            out.append({"date": ev.start_time.date(), "type": ev.event_type, "severity": ev.severity, "source": ev.source,
                        "description": ev.description, "is_forecast": bool(ev.is_forecast)})
        return _dump(out[:80])

    @beta_tool
    def data_coverage(scope: str | None = None) -> str:
        """What data exists: first and last sale dates, store-days the feed
        reported but that lack product detail (revenue understated there), and
        which sources are populated. Check this before explaining a drop."""
        rows = reconcile(session, scope_of(scope) if scope_of(scope) and not is_state(scope_of(scope)) else None)
        first, last = session.execute(select(Sale.sold_at).order_by(Sale.sold_at)).scalars().first(), session.execute(select(Sale.sold_at).order_by(Sale.sold_at.desc())).scalars().first()
        return _dump({
            "first_sale": first.date() if first else None, "last_sale": last.date() if last else None,
            "feed_store_days": len(rows),
            "missing_product_detail": [f"{r['store']} {r['date']}" for r in rows if r["coverage"] == "missing"][:60],
            "mismatched_days": [f"{r['store']} {r['date']}" for r in rows if r["coverage"] == "mismatch"][:20],
            "market_report": market_context(session, as_of_default) is not None,
        })

    return [list_stores, data_coverage, weekly_summary, weekly_trend_series, store_ranking_for_period, categories, products,
            inventory, promotions, discount_codes, market, competitor_deals, external_findings, weather_effects, forecast, upcoming_events]
