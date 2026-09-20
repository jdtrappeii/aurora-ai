from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.external import event_findings, proactive_forecast, weather_intelligence
from app.api.routes import resolve_as_of
from app.db import get_session
from app.models import Store

router = APIRouter(prefix="/api/external")


def resolve_store(session: Session, store: str | None) -> str:
    """External analytics are per-store because matching depends on location."""
    if store:
        if session.execute(select(Store).where(Store.code == store)).scalar_one_or_none() is None:
            raise HTTPException(404, f"unknown store {store!r}")
        return store
    first = session.execute(select(Store).order_by(Store.code)).scalars().first()
    if first is None:
        raise HTTPException(404, "no stores imported yet")
    return first.code


@router.get("/events")
def external_events(as_of: date | None = None, store: str | None = None, session: Session = Depends(get_session)):
    return event_findings(session, resolve_store(session, store), resolve_as_of(session, as_of))


@router.get("/weather")
def external_weather(as_of: date | None = None, store: str | None = None, session: Session = Depends(get_session)):
    return weather_intelligence(session, resolve_store(session, store), resolve_as_of(session, as_of))


@router.get("/forecast")
def external_forecast(
    as_of: date | None = None,
    store: str | None = None,
    days: int = Query(7, ge=1, le=14),
    session: Session = Depends(get_session),
):
    return proactive_forecast(session, resolve_store(session, store), resolve_as_of(session, as_of), days)
