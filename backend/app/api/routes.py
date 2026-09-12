from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.comparisons import weekly_comparison, weekly_trend
from app.analytics.external import event_findings, proactive_forecast, weather_intelligence
from app.analytics.financial import financial_summary
from app.analytics.inventory import inventory_report
from app.analytics.periods import Period, four_week_window, previous_week, week_containing
from app.analytics.products import category_profitability, product_profitability
from app.analytics.promotions import promotion_results
from app.db import get_session
from app.importers.csv_importer import IMPORTERS, ImportError_
from app.models import Sale, Store

router = APIRouter(prefix="/api")


def resolve_as_of(session: Session, as_of: date | None) -> date:
    """Default to the latest sale date so a stale import still shows a full picture."""
    if as_of:
        return as_of
    latest = session.execute(select(func.max(Sale.sold_at))).scalar_one()
    return latest.date() if latest else date.today()


def resolve_period(session: Session, start: date | None, end: date | None, as_of: date | None) -> Period:
    if start and end:
        if end < start:
            raise HTTPException(400, "end precedes start")
        return Period("custom", start, end)
    return week_containing(resolve_as_of(session, as_of))


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/stores")
def stores(session: Session = Depends(get_session)):
    return [{"code": s.code, "name": s.name} for s in session.execute(select(Store).order_by(Store.code)).scalars()]


@router.post("/import/{kind}")
async def import_csv(kind: str, file: UploadFile = File(...), session: Session = Depends(get_session)):
    if kind not in IMPORTERS:
        raise HTTPException(404, f"unknown import kind {kind!r}; expected one of {sorted(IMPORTERS)}")
    try:
        result = IMPORTERS[kind](session, await file.read())
    except ImportError_ as e:
        raise HTTPException(400, str(e))
    return result.to_dict()


@router.get("/metrics/weekly")
def metrics_weekly(as_of: date | None = None, store: str | None = None, session: Session = Depends(get_session)):
    return weekly_comparison(session, resolve_as_of(session, as_of), store)


@router.get("/metrics/weekly-trend")
def metrics_weekly_trend(
    as_of: date | None = None,
    weeks: int = Query(12, ge=2, le=104),
    store: str | None = None,
    session: Session = Depends(get_session),
):
    return weekly_trend(session, resolve_as_of(session, as_of), weeks, store)


@router.get("/metrics/summary")
def metrics_summary(
    start: date | None = None, end: date | None = None, as_of: date | None = None, store: str | None = None,
    session: Session = Depends(get_session),
):
    return financial_summary(session, resolve_period(session, start, end, as_of), store).to_dict()


@router.get("/metrics/products")
def metrics_products(
    start: date | None = None, end: date | None = None, as_of: date | None = None, store: str | None = None,
    session: Session = Depends(get_session),
):
    period = resolve_period(session, start, end, as_of)
    return {"period": period.to_dict(), "products": product_profitability(session, period, store)}


@router.get("/metrics/categories")
def metrics_categories(
    start: date | None = None, end: date | None = None, as_of: date | None = None, store: str | None = None,
    session: Session = Depends(get_session),
):
    period = resolve_period(session, start, end, as_of)
    return {"period": period.to_dict(), "categories": category_profitability(session, period, store)}


@router.get("/metrics/inventory")
def metrics_inventory(as_of: date | None = None, store: str | None = None, session: Session = Depends(get_session)):
    return inventory_report(session, resolve_as_of(session, as_of), store)


@router.get("/metrics/promotions")
def metrics_promotions(store: str | None = None, name: str | None = None, session: Session = Depends(get_session)):
    return promotion_results(session, store, name)


@router.get("/dashboard")
def dashboard(as_of: date | None = None, store: str | None = None, session: Session = Depends(get_session)):
    """Everything the home screen needs in one call."""
    as_of = resolve_as_of(session, as_of)
    current = week_containing(as_of)
    inv = inventory_report(session, as_of, store)
    inv_items = inv.pop("items")
    watch = [i for i in inv_items if i["status"] in ("dead", "slow")][:10]
    stockout = sorted(
        (i for i in inv_items if i["days_of_supply"] is not None and i["days_of_supply"] < 7 and i["status"] != "dead"),
        key=lambda i: i["days_of_supply"],
    )[:10]
    return {
        "as_of": as_of.isoformat(),
        "store": store,
        "weekly": weekly_comparison(session, as_of, store),
        "trend": weekly_trend(session, as_of, 12, store),
        "categories": category_profitability(session, current, store),
        "top_products": product_profitability(session, current, store)[:10],
        "four_week_categories": category_profitability(session, four_week_window(current), store),
        "inventory": {**inv, "watch": watch, "stockout_risk": stockout},
        "promotions": promotion_results(session, store),
        "external": _external_block(session, store, as_of, current),
    }


def _external_block(session: Session, store: str | None, as_of: date, current: Period) -> dict | None:
    """External findings for the home screen: this week's material events, learned
    weather effects, and the next 7 days. Per-store, so pick the first store when none given."""
    code = store or (session.execute(select(Store.code).order_by(Store.code)).scalars().first())
    if code is None:
        return None
    events = event_findings(session, code, as_of)
    this_week = [
        f for f in events["findings"]
        if f["evidence_level"] != "no_material_variance"
        and current.start.isoformat() <= f["start_time"][:10] <= current.end.isoformat()
    ]
    weather = weather_intelligence(session, code, as_of)
    return {
        "store": code,
        "this_week_findings": this_week,
        "top_findings": [f for f in events["findings"] if f["evidence_level"] != "no_material_variance"][:8],
        "resilience": events["resilience"],
        "weather_effects": [e for e in weather["effects"] if e["condition"] != "fair"],
        "forecast": proactive_forecast(session, code, as_of, 7)["days"],
    }
