"""Headset connector: envelope import, exact totals, ticket counting, discount
report, sync orchestration and reconciliation. Numbers are worked by hand."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.analytics.discounts import discount_report
from app.analytics.financial import financial_summary
from app.analytics.inventory import inventory_report
from app.analytics.periods import Period, week_containing
from app.analytics.products import category_profitability, product_profitability
from app.importers.csv_importer import ImportError_
from app.importers.headset import (
    Envelope,
    import_envelope,
    import_headset_directory,
    reconcile,
    store_code_for,
    timezone_for,
)
from app.integrations.headset.client import StaticSource
from app.integrations.headset.pull import _looker_range, headset_sync
from app.models import DailyStoreSummary, InventorySnapshot, Product, Sale, SaleItem, Store

STORES = {"stores": [
    {"storeId": 10136, "accountId": 1, "name": "FL - Demo - Pace", "address": {"city": "Milton", "state": "FL", "postalCode": "32571"}},
    {"storeId": 10132, "accountId": 1, "name": "FL - Demo - Tampa ", "address": {"city": "Tampa", "state": "FL", "postalCode": "33606"}},
]}
PACE = "FL - Demo - Pace"
DAY = date(2026, 9, 15)  # a Tuesday

# Two SKUs. Totals chosen so per-unit prices do NOT divide evenly: the exact
# totals must survive, not qty * rounded unit price.
#   A: 3 units, gross 100.00, revenue 33.33 (disc 66.67), cost 10.00, 2 tickets
#   B: 7 units, gross 70.00,  revenue 50.00 (disc 20.00), cost 21.00, 5 tickets
PRODUCTS = {"rows": [
    {"product_name": "Alpha 3.5g", "sku": "A1", "total_revenue": 33.33, "total_gross_sales": 100.0, "total_units": 3,
     "total_discounts": 66.67, "total_cost": 10.0, "total_profit": 23.33, "transaction_count": 2},
    {"product_name": "Beta 1g Vape", "sku": "B1", "total_revenue": 50.0, "total_gross_sales": 70.0, "total_units": 7,
     "total_discounts": 20.0, "total_cost": 21.0, "total_profit": 29.0, "transaction_count": 5},
], "hasMore": False}
# The day's true totals: 6 receipts (one held both SKUs), revenue 83.33, cost 31.00
STORE_DAYS = {"rows": [
    {"sold_date": "2026-09-15", "store_name": "FL - Demo - Pace", "total_revenue": 83.33, "total_gross_sales": 170.0,
     "total_units": 10, "total_discounts": 86.67, "total_cost": 31.0, "total_profit": 52.33, "transaction_count": 6},
], "hasMore": False}
DISCOUNTS = {"rows": [
    {"discount_name": None, "total_revenue": 20.0, "total_units": 2, "total_discounts": 0, "transaction_count": 2},
    {"discount_name": "Tuesday 60% Vapes", "total_revenue": 30.0, "total_units": 5, "total_discounts": 45.0, "transaction_count": 3},
    {"discount_name": "Loyalty $1", "total_revenue": 33.33, "total_units": 3, "total_discounts": 3.0, "transaction_count": 3},
], "hasMore": False}
INVENTORY = {"rows": [
    {"product_name": "Alpha 3.5g", "sku": "A1", "brand": "Demo", "category": "Flower Pouch 3.5g", "unit": "3.5g", "vendor": "Demo Co",
     "on_hand_units": 40, "price": 35, "on_hand_retail_value": 1400, "on_hand_cost_value": 133.4},
    {"product_name": "Beta 1g Vape", "sku": "B1", "brand": "Demo", "category": "Vape Cart 1g", "unit": "1g", "vendor": "Demo Co",
     "on_hand_units": 0, "price": 40, "on_hand_retail_value": 0, "on_hand_cost_value": None, "avg_daily_units": None},
], "hasMore": False}


def env(kind, result, **meta):
    return {"kind": kind, "result": result, **meta}


def load_all(session):
    import_envelope(session, env("stores", STORES))
    import_envelope(session, env("inventory", INVENTORY, store_name=PACE, snapshot_date="2026-09-15"))
    import_envelope(session, env("products", PRODUCTS, store_name=PACE, sold_date="2026-09-15"))
    import_envelope(session, env("discounts", DISCOUNTS, store_name=PACE, sold_date="2026-09-15"))
    import_envelope(session, env("store_days", STORE_DAYS))


def test_store_codes_and_timezones(session):
    r = import_envelope(session, env("stores", STORES))
    assert (r.inserted, r.errors) == (2, [])
    pace = session.execute(select(Store).where(Store.code == store_code_for(10136))).scalar_one()
    assert pace.name == "FL - Demo - Pace"
    assert pace.timezone == "America/Chicago"  # panhandle zip
    tampa = session.execute(select(Store).where(Store.code == "HS10132")).scalar_one()
    assert tampa.name == "FL - Demo - Tampa"  # trailing space trimmed
    assert tampa.timezone == "America/New_York"
    assert timezone_for("NV", "89109") == "America/Los_Angeles"
    assert timezone_for("XX", None) is None
    # re-import updates in place
    r = import_envelope(session, env("stores", STORES))
    assert (r.inserted, r.updated) == (0, 2)


def test_store_name_matching_ignores_whitespace(session):
    import_envelope(session, env("stores", STORES))
    r = import_envelope(session, env("products", PRODUCTS, store_name="FL - Demo -  Tampa", sold_date="2026-09-15"))
    assert r.inserted == 2
    with pytest.raises(ImportError_):
        import_envelope(session, env("products", PRODUCTS, store_name="FL - Demo - Nowhere", sold_date="2026-09-15"))


def test_product_lines_keep_exact_totals(session):
    load_all(session)
    items = session.execute(select(SaleItem).join(Sale).order_by(Sale.transaction_id)).scalars().all()
    a = next(i for i in items if i.sale.transaction_id.endswith(":A1"))
    assert a.sale.source == "headset"
    assert a.sale.transaction_id == "HS:HS10136:2026-09-15:A1"
    assert (a.quantity, a.ticket_count) == (3, 2)
    assert (a.gross_total, a.revenue_total, a.cogs_total) == (Decimal("100.00"), Decimal("33.33"), Decimal("10.00"))
    assert a.discount_amount == Decimal("66.67")
    assert a.sale_price == Decimal("11.11")  # 33.33 / 3 informational only
    assert a.unit_cost == Decimal("3.3333")

    s = financial_summary(session, Period("day", DAY, DAY), "HS10136")
    assert s.gross_sales == Decimal("170.00")
    assert s.revenue == Decimal("83.33")  # not 3*11.11 + 7*7.14 = 83.31
    assert s.discount_total == Decimal("86.67")
    assert s.cogs == Decimal("31.00")
    assert s.gross_profit == Decimal("52.33")
    assert s.transactions == 6  # store-day total, not 2 + 5
    assert s.units == 10
    assert s.avg_transaction_value == Decimal("13.89")  # 83.33 / 6


def test_ticket_count_without_store_day_falls_back_to_line_tickets(session):
    import_envelope(session, env("stores", STORES))
    import_envelope(session, env("products", PRODUCTS, store_name=PACE, sold_date="2026-09-15"))
    s = financial_summary(session, Period("day", DAY, DAY), "HS10136")
    assert s.transactions == 7  # 2 + 5: the best we know without the feed's day total


def test_product_and_category_breakdowns(session):
    load_all(session)
    rows = product_profitability(session, Period("day", DAY, DAY), "HS10136")
    by_sku = {r["sku"]: r for r in rows}
    assert by_sku["A1"]["category"] == "Flower Pouch 3.5g"  # learned from inventory
    assert by_sku["A1"]["brand"] == "Demo"
    assert by_sku["A1"]["transactions"] == 2  # exact at product grain
    assert by_sku["A1"]["gross_profit"] == Decimal("23.33")
    assert by_sku["B1"]["gross_profit"] == Decimal("29.00")
    cats = {r["category"]: r for r in category_profitability(session, Period("day", DAY, DAY), "HS10136")}
    assert cats["Vape Cart 1g"]["revenue"] == Decimal("50.00")
    assert cats["Vape Cart 1g"]["revenue_share"] == Decimal("0.6000")  # 50 / 83.33


def test_sales_before_inventory_get_uncategorized_then_upgraded(session):
    import_envelope(session, env("stores", STORES))
    import_envelope(session, env("products", PRODUCTS, store_name=PACE, sold_date="2026-09-15"))
    p = session.execute(select(Product).where(Product.sku == "A1")).scalar_one()
    assert p.category.name == "Uncategorized"
    import_envelope(session, env("inventory", INVENTORY, store_name=PACE, snapshot_date="2026-09-15"))
    session.refresh(p)
    assert p.category.name == "Flower Pouch 3.5g"
    assert p.unit_cost == Decimal("3.3350")  # 133.40 / 40
    assert p.retail_price == Decimal("35.00")


def test_inventory_snapshot_and_report(session):
    load_all(session)
    snaps = {s.product.sku: s for s in session.execute(select(InventorySnapshot)).scalars()}
    assert snaps["A1"].quantity_on_hand == 40
    assert snaps["A1"].unit_cost == Decimal("3.3350")
    assert snaps["B1"].quantity_on_hand == 0
    rep = inventory_report(session, DAY, "HS10136")
    assert rep["inventory_value"] == Decimal("133.40")
    items = {i["sku"]: i for i in rep["items"]}
    assert items["A1"]["units_sold_30d"] == 3
    assert items["A1"]["age_bucket"] == "unknown"  # Headset has no received_date
    assert items["B1"]["status"] == "out_of_stock"


def test_discount_report(session):
    load_all(session)
    rep = discount_report(session, Period("day", DAY, DAY), "HS10136")
    assert rep["total_discounts"] == Decimal("48.00")  # 45 + 3, undiscounted row excluded
    assert rep["undiscounted"]["revenue"] == Decimal("20.00")
    top = rep["codes"][0]
    assert top["discount_name"] == "Tuesday 60% Vapes"
    assert top["discount_depth"] == Decimal("0.6000")  # 45 / (30 + 45)
    assert top["share"] == Decimal("0.9375")  # 45 / 48
    assert top["vs_previous_pct"] is None
    assert rep["vs_previous_pct"] is None


def test_reimport_is_idempotent(session):
    load_all(session)
    load_all(session)
    assert session.execute(select(Sale)).scalars().all().__len__() == 2
    assert len(session.execute(select(DailyStoreSummary)).scalars().all()) == 1
    assert len(session.execute(select(InventorySnapshot)).scalars().all()) == 2


def test_reconcile_flags_partial_product_pull(session):
    load_all(session)
    assert reconcile(session)[0]["revenue_diff"] == Decimal("0.00")
    partial = {"rows": PRODUCTS["rows"][:1], "hasMore": True}
    # remove B1 by re-importing only A1 does not delete B1; simulate a restated day instead
    restated = {"rows": [{**STORE_DAYS["rows"][0], "total_revenue": 90.0}], "hasMore": False}
    import_envelope(session, env("store_days", restated))
    r = reconcile(session, "HS10136")[0]
    assert r["revenue_diff"] == Decimal("-6.67")
    assert r["feed_tickets"] == 6 and r["product_line_tickets"] == 7
    assert r["coverage"] == "mismatch"
    assert partial["hasMore"]


def test_reconcile_reports_store_days_with_no_product_detail(session):
    import_envelope(session, env("stores", STORES))
    import_envelope(session, env("store_days", STORE_DAYS))
    rows = reconcile(session)
    assert len(rows) == 1
    assert rows[0]["coverage"] == "missing"
    assert rows[0]["skus"] == 0
    assert rows[0]["revenue_diff"] == Decimal("-83.33")


def test_envelope_validation():
    with pytest.raises(ImportError_):
        Envelope.from_dict({"kind": "nope", "result": {}})
    with pytest.raises(ImportError_):
        Envelope.from_dict({"kind": "products", "result": "not a dict"})
    with pytest.raises(ImportError_):
        Envelope.load(b"{not json")


def test_directory_replay_orders_dimensions_first(session, tmp_path):
    import json
    (tmp_path / "z_products.json").write_text(json.dumps(env("products", PRODUCTS, store_name=PACE, sold_date="2026-09-15")))
    (tmp_path / "a_store_days.json").write_text(json.dumps(env("store_days", STORE_DAYS)))
    (tmp_path / "y_stores.json").write_text(json.dumps(env("stores", STORES)))
    (tmp_path / "broken.json").write_text("{")
    results = import_headset_directory(session, tmp_path)
    kinds = [r.kind.split(" ")[0] for r in results]
    assert kinds == ["skip:broken.json", "headset_stores", "headset_products", "headset_store_days"]
    assert results[0].errors


def test_looker_range_is_end_exclusive():
    assert _looker_range(date(2026, 9, 1), date(2026, 9, 14)) == "2026-09-01 to 2026-09-15"


def test_headset_sync_end_to_end(session, tmp_path):
    def by_dimension(args):
        if args["dimension"] == "product":
            return PRODUCTS if args["soldDate"] == "2026-09-15" else {"rows": [], "hasMore": False}
        return DISCOUNTS if args["soldDate"] == "2026-09-15" else {"rows": [], "hasMore": False}

    def inventory(args):
        assert args["storeNames"] == [PACE]
        return INVENTORY

    source = StaticSource({
        "retailer_get_stores": STORES,
        "retailer_get_inventory": inventory,
        "retailer_sales_by_dimension": by_dimension,
        "retailer_sales_trend": lambda a: STORE_DAYS,
    })
    report = headset_sync(source, session, date(2026, 9, 14), date(2026, 9, 15), store_filter="pace",
                          record_dir=tmp_path, snapshot_date=DAY)
    assert report.stores == [PACE]
    # stores + inventory + 1 trend chunk + 2 days x (products + discounts) = 7
    assert report.calls == 7
    assert report.warnings == []
    assert len(report.reconciliation) == 1 and report.reconciliation[0]["revenue_diff"] == 0
    trend_call = next(a for t, a in source.calls if t == "retailer_sales_trend")
    assert trend_call["soldDate"] == "2026-09-14 to 2026-09-16"
    recorded = sorted(p.name for p in tmp_path.glob("*.json"))
    assert recorded == [
        "discounts__fl-demo-pace__2026-09-14.json", "discounts__fl-demo-pace__2026-09-15.json",
        "inventory__fl-demo-pace__2026-09-15.json",
        "products__fl-demo-pace__2026-09-14.json", "products__fl-demo-pace__2026-09-15.json",
        "store_days__2026-09-14__2026-09-15.json", "stores.json",
    ]
    week = financial_summary(session, week_containing(DAY), "HS10136")
    assert week.revenue == Decimal("83.33") and week.transactions == 6


def test_sync_pages_inventory_by_category_when_truncated(session):
    calls = []

    def inventory(args):
        calls.append(args)
        if args.get("groupBy") == "category":
            return {"rows": [{"category": "Flower Pouch 3.5g"}, {"category": "Vape Cart 1g"}], "hasMore": False}
        if "categories" in args:
            return {"rows": [r for r in INVENTORY["rows"] if r["category"] in args["categories"]], "hasMore": False}
        return {"rows": INVENTORY["rows"][:1], "hasMore": True}

    source = StaticSource({"retailer_get_stores": STORES, "retailer_get_inventory": inventory,
                           "retailer_sales_trend": {"rows": [], "hasMore": False}})
    report = headset_sync(source, session, DAY, DAY, store_filter="pace", include_products=False,
                          include_discounts=False, snapshot_date=DAY)
    assert [c.get("categories") for c in calls] == [None, None, ["Flower Pouch 3.5g"], ["Vape Cart 1g"]]
    assert calls[0]["inStockOnly"] is True
    assert len(session.execute(select(InventorySnapshot)).scalars().all()) == 2
    assert report.warnings == []


# ---------- HTTP surface ----------

@pytest.fixture
def client(engine):
    import json  # noqa: F401
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.db import get_session
    from app.main import app

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _upload(client, payload):
    import json
    return client.post("/api/import/headset", files={"file": ("x.json", json.dumps(payload).encode(), "application/json")})


def test_api_import_headset_and_reports(client):
    assert _upload(client, env("stores", STORES)).json()["inserted"] == 2
    assert _upload(client, env("inventory", INVENTORY, store_name=PACE, snapshot_date="2026-09-15")).json()["inserted"] == 2
    assert _upload(client, env("products", PRODUCTS, store_name=PACE, sold_date="2026-09-15")).json()["inserted"] == 2
    assert _upload(client, env("discounts", DISCOUNTS, store_name=PACE, sold_date="2026-09-15")).json()["inserted"] == 3
    assert _upload(client, env("store_days", STORE_DAYS)).json()["inserted"] == 1

    bad = _upload(client, {"kind": "products", "result": {"rows": []}, "store_name": "nobody", "sold_date": "2026-09-15"})
    assert bad.status_code == 400 and "unknown store" in bad.json()["detail"]

    rec = client.get("/api/headset/reconcile").json()
    assert rec == {"days": 1, "ok": 1, "mismatches": [], "missing": []}

    disc = client.get("/api/metrics/discounts", params={"as_of": "2026-09-15", "store": "HS10136"}).json()
    assert disc["total_discounts"] == 48.0
    assert disc["codes"][0]["discount_name"] == "Tuesday 60% Vapes"

    dash = client.get("/api/dashboard", params={"as_of": "2026-09-15", "store": "HS10136"}).json()
    assert dash["weekly"]["current_week"]["revenue"] == 83.33
    assert dash["weekly"]["current_week"]["transactions"] == 6
    assert dash["discounts"]["code_count"] == 2
    assert {c["category"] for c in dash["categories"]} == {"Flower Pouch 3.5g", "Vape Cart 1g"}
    listing = client.get("/api/stores").json()
    assert {s["code"] for s in listing["stores"]} == {"HS10136", "HS10132"}
    assert [s["code"] for s in listing["scopes"]] == ["state:FL"]
