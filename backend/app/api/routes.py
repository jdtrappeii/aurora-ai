from datetime import date, datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.comparisons import weekly_comparison, weekly_trend
from app.analytics.discounts import discount_report
from app.analytics.market import competitor_pressure, market_context
from app.analytics.report import weekly_report
from app.analytics.scope import is_state, stores_in_scope
from app.reports.render import render_html
from app.analytics.external import event_findings, proactive_forecast, weather_intelligence
from app.analytics.financial import financial_summary
from app.analytics.inventory import inventory_report
from app.analytics.periods import Period, four_week_window, previous_week, week_containing
from app.analytics.products import category_profitability, product_profitability
from app.analytics.promotions import promotion_results
from app.db import get_session
from app.importers.csv_importer import IMPORTERS, ImportError_
from app.importers.headset import Envelope, import_envelope, reconcile
from app.integrations import heartbeat as hb
from app.config import settings
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
    """Stores plus the state scopes above them and the default scope for the
    dashboard (DEFAULT_SCOPE, e.g. "state:FL")."""
    rows = session.execute(select(Store).order_by(Store.state, Store.code)).scalars().all()
    states = sorted({s.state for s in rows if s.state})
    return {
        "default": settings.default_scope or None,
        "scopes": [{"code": f"state:{st}", "name": f"All {st} stores", "state": st} for st in states],
        "stores": [{"code": s.code, "name": s.name, "state": s.state} for s in rows],
    }


@router.post("/import/headset")
async def import_headset(file: UploadFile = File(...), session: Session = Depends(get_session)):
    """Upload one recorded Headset envelope (see app/importers/headset.py)."""
    try:
        return import_envelope(session, Envelope.load(await file.read())).to_dict()
    except ImportError_ as e:
        raise HTTPException(400, str(e))


@router.post("/import/{kind}")
async def import_csv(kind: str, file: UploadFile = File(...), session: Session = Depends(get_session)):
    if kind not in IMPORTERS:
        raise HTTPException(404, f"unknown import kind {kind!r}; expected one of {sorted(IMPORTERS)}")
    try:
        result = IMPORTERS[kind](session, await file.read())
    except ImportError_ as e:
        raise HTTPException(400, str(e))
    return result.to_dict()


@router.get("/headset/reconcile")
def headset_reconcile(store: str | None = None, session: Session = Depends(get_session)):
    rows = reconcile(session, store)
    return {
        "days": len(rows),
        "ok": sum(1 for r in rows if r["coverage"] == "ok"),
        "mismatches": [r for r in rows if r["coverage"] == "mismatch"],
        "missing": [{"store": r["store"], "date": r["date"], "feed_revenue": r["feed_revenue"]} for r in rows if r["coverage"] == "missing"],
    }


@router.post("/heartbeat")
def heartbeat(
    store: str, kind: str = Query("power", pattern="^(power|network)$"), token: str | None = None,
    at: datetime | None = None, session: Session = Depends(get_session),
):
    """A device at the store calls this every minute. Token comes from HEARTBEAT_TOKEN
    (query parameter or X-Heartbeat-Token header); the endpoint is off when it is blank."""
    if not settings.heartbeat_token:
        raise HTTPException(503, "heartbeats disabled: set HEARTBEAT_TOKEN")
    if token != settings.heartbeat_token:
        raise HTTPException(401, "bad token")
    st = session.execute(select(Store).where(Store.code == store)).scalar_one_or_none()
    if st is None:
        raise HTTPException(404, f"unknown store {store!r}")
    run = hb.record_ping(session, st, kind, (at or datetime.utcnow()).replace(tzinfo=None), settings.heartbeat_gap_minutes)
    return {"store": store, "kind": kind, "run_started_at": run.started_at.isoformat(), "last_seen_at": run.last_seen_at.isoformat(), "pings": run.pings}


@router.get("/heartbeat/status")
def heartbeat_status(session: Session = Depends(get_session)):
    return [s.__dict__ for s in hb.status(session)]


class AnalystTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class AnalystQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    scope: str | None = None
    as_of: date | None = None
    history: list[AnalystTurn] = Field(default_factory=list, max_length=20)


@router.post("/analyst")
def analyst(q: AnalystQuestion, session: Session = Depends(get_session)):
    """Ask the analyst. Read-only: every number comes from Aurora's own tools."""
    import anthropic

    from app.analyst.agent import ask

    if not (settings.anthropic_api_key or __import__("os").environ.get("ANTHROPIC_API_KEY") or __import__("os").environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise HTTPException(503, "analyst disabled: set ANTHROPIC_API_KEY on the server")
    try:
        return ask(session, q.question, q.scope, q.as_of, [t.model_dump() for t in q.history]).to_dict()
    except anthropic.AuthenticationError:
        raise HTTPException(503, "analyst: invalid ANTHROPIC_API_KEY")
    except anthropic.RateLimitError:
        raise HTTPException(429, "analyst: rate limited, try again shortly")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"analyst: upstream error {e.status_code}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "analyst: cannot reach the Claude API")


@router.get("/report/weekly")
def report_weekly(
    as_of: date | None = None, store: str | None = None, format: str = Query("json", pattern="^(json|html)$"),
    session: Session = Depends(get_session),
):
    rep = weekly_report(session, resolve_as_of(session, as_of), store)
    if format == "html":
        return HTMLResponse(render_html(rep))
    return rep


@router.get("/metrics/market")
def metrics_market(as_of: date | None = None, session: Session = Depends(get_session)):
    return market_context(session, resolve_as_of(session, as_of))


@router.get("/metrics/pressure")
def metrics_pressure(
    start: date | None = None, end: date | None = None, as_of: date | None = None, session: Session = Depends(get_session),
):
    return competitor_pressure(session, resolve_period(session, start, end, as_of))


@router.get("/metrics/discounts")
def metrics_discounts(
    start: date | None = None, end: date | None = None, as_of: date | None = None, store: str | None = None,
    limit: int = Query(25, ge=1, le=500),
    session: Session = Depends(get_session),
):
    return discount_report(session, resolve_period(session, start, end, as_of), store, limit)


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
        "discounts": discount_report(session, current, store, 15),
        "market": market_context(session, as_of),
        "pressure": competitor_pressure(session, current),
        "external": _external_block(session, store, as_of, current),
    }


def _external_block(session: Session, store: str | None, as_of: date, current: Period) -> dict | None:
    """External findings for the home screen: this week's material events, learned
    weather effects, and the next 7 days. Per-store, so pick the first store when none given."""
    if store and not is_state(store):
        code = store
    else:
        first = stores_in_scope(session, store)
        code = first[0].code if first else None
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
