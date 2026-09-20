"""State scope: "state:FL" behaves like a store code everywhere, sums the
state's stores and excludes the rest."""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.analytics.financial import financial_summary
from app.analytics.periods import week_containing
from app.analytics.report import weekly_report
from app.analytics.scope import is_state, scope_label, state_of, store_predicate, stores_in_scope
from app.importers.headset import import_envelope
from app.models import Product, Sale, SaleItem, Store
from tests.conftest import MONDAY, dt


def _sale(session, store, product, qty, price, when):
    s = Sale(transaction_id=f"T-{store.code}-{when.isoformat()}", store_id=store.id, sold_at=when, status="completed")
    session.add(s)
    session.flush()
    session.add(SaleItem(sale_id=s.id, line_no=1, product_id=product.id, quantity=qty, regular_price=Decimal(price), sale_price=Decimal(price),
                         discount_amount=Decimal("0"), unit_cost=product.unit_cost))
    session.flush()


def test_scope_helpers():
    assert is_state("state:FL") and not is_state("HS1") and not is_state(None)
    assert state_of("state:fl") == "FL" and state_of("HS1") is None
    assert str(store_predicate("state:FL")) == "stores.state = :state_1"
    assert str(store_predicate("HS1")) == "stores.code = :code_1"


def test_state_scope_sums_only_that_state(session, seed):
    seed.store.state = "NV"
    pace = Store(code="HS10001", name="Pace", state="FL")
    tampa = Store(code="HS10002", name="Tampa", state="FL")
    session.add_all([pace, tampa])
    session.commit()
    a = seed.product("A", "Flower", "10", "25")
    _sale(session, seed.store, a, 1, "25", dt(MONDAY, 10))          # NV: 25
    _sale(session, pace, a, 2, "25", dt(MONDAY, 11))                # FL: 50
    _sale(session, tampa, a, 4, "25", dt(MONDAY + timedelta(days=1), 11))  # FL: 100
    session.commit()
    week = week_containing(MONDAY)
    assert financial_summary(session, week).revenue == Decimal("175.00")
    assert financial_summary(session, week, "state:FL").revenue == Decimal("150.00")
    assert financial_summary(session, week, "state:FL").transactions == 2
    assert financial_summary(session, week, "state:NV").revenue == Decimal("25.00")
    assert financial_summary(session, week, "HS10001").revenue == Decimal("50.00")
    assert [s.code for s in stores_in_scope(session, "state:FL")] == ["HS10001", "HS10002"]
    assert scope_label(session, "state:FL") == "All FL stores" and scope_label(session, "HS10001") == "Pace"

    rep = weekly_report(session, MONDAY + timedelta(days=6), "state:FL")
    assert rep["store_name"] == "All FL stores"
    assert [r["store"] for r in rep["stores"]] == ["HS10001", "HS10002"]  # ranked by GP change: Pace +40, Tampa +80... ascending
    assert rep["headline"]["current"]["revenue"] == Decimal("150.00")
    assert rep["coverage"]["stores_reporting"] == 2


def test_headset_store_import_sets_state(session):
    payload = {"kind": "stores", "result": {"stores": [
        {"storeId": 1, "name": "FL - X", "address": {"state": "fl", "postalCode": "33606", "city": "Tampa", "address1": "1 Main"}},
        {"storeId": 2, "name": "NV - Y", "address": {"state": "NV", "postalCode": "89109"}},
    ]}}
    import_envelope(session, payload)
    states = {s.code: s.state for s in session.execute(select(Store)).scalars()}
    assert states == {"HS1": "FL", "HS2": "NV"}


def test_stores_route_lists_scopes_and_default(session, engine, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app import config
    from app.db import get_session
    from app.main import app

    monkeypatch.setattr(config.settings, "default_scope", "state:FL")
    session.add_all([Store(code="HS1", name="A", state="FL"), Store(code="HS2", name="B", state="NV"), Store(code="HS3", name="C", state="FL")])
    session.commit()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app.dependency_overrides[get_session] = lambda: (yield factory())
    with TestClient(app) as c:
        j = c.get("/api/stores").json()
        assert j["default"] == "state:FL"
        assert j["scopes"] == [{"code": "state:FL", "name": "All FL stores", "state": "FL"}, {"code": "state:NV", "name": "All NV stores", "state": "NV"}]
        assert [s["code"] for s in j["stores"]] == ["HS1", "HS3", "HS2"]
        d = c.get("/api/dashboard", params={"store": "state:FL", "as_of": "2026-09-07"}).json()
        assert d["store"] == "state:FL" and d["external"]["store"] == "HS1"
    app.dependency_overrides.clear()
