from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.importers.csv_importer import (
    ImportError_,
    import_expenses,
    import_external_events,
    import_inventory,
    import_products,
    import_promotions,
    import_sale_items,
    import_sales,
    import_stores,
    import_weather,
)
from app.models import ExternalEvent, Product, Sale, SaleItem, Store, WeatherObservation

PRODUCTS = b"""sku,name,category,brand,vendor,unit_cost,retail_price
FLO-001,Blue Dream 3.5g,Flower,Sunburst,Sunburst,14.00,35.00
EDI-001,Gummies 100mg,Edibles,Honeybee,Honeybee,6.00,20.00
"""
SALES = b"""transaction_id,store,sold_at,employee,customer,status
T1,MAIN,2026-09-07T10:15:00,E101,C0001,completed
T2,MAIN,2026-09-07 11:00:00,E102,,refunded
"""
ITEMS = b"""transaction_id,line_no,sku,quantity,regular_price,sale_price,unit_cost,promotion
T1,1,FLO-001,2,35.00,31.50,14.00,
T1,2,EDI-001,1,20.00,20.00,,
T2,1,EDI-001,1,20.00,20.00,6.00,
"""


def test_products_then_sales_then_items_round_trip(session):
    r = import_products(session, PRODUCTS)
    assert (r.inserted, r.updated, r.errors) == (2, 0, [])
    r = import_sales(session, SALES)
    assert (r.inserted, r.errors) == (2, [])
    r = import_sale_items(session, ITEMS)
    assert (r.inserted, r.errors) == (3, [])

    items = session.execute(select(SaleItem).order_by(SaleItem.sale_id, SaleItem.line_no)).scalars().all()
    assert items[0].discount_amount == Decimal("7.00")  # (35 - 31.50) * 2
    assert items[1].unit_cost == Decimal("6.0000")  # blank cost falls back to the product
    assert session.execute(select(Sale).where(Sale.transaction_id == "T2")).scalar_one().status == "refunded"


def test_reimport_updates_in_place_without_duplicates(session):
    import_products(session, PRODUCTS)
    changed = PRODUCTS.replace(b"14.00,35.00", b"15.00,36.00")
    r = import_products(session, changed)
    assert (r.inserted, r.updated) == (0, 2)
    assert session.execute(select(func.count()).select_from(Product)).scalar_one() == 2
    assert session.execute(select(Product).where(Product.sku == "FLO-001")).scalar_one().retail_price == Decimal("36.00")

    import_sales(session, SALES)
    import_sale_items(session, ITEMS)
    r = import_sale_items(session, ITEMS)
    assert (r.inserted, r.updated) == (0, 3)
    assert session.execute(select(func.count()).select_from(SaleItem)).scalar_one() == 3


def test_row_level_errors_are_collected_not_fatal(session):
    import_products(session, PRODUCTS)
    import_sales(session, SALES)
    bad = ITEMS + b"T9,1,FLO-001,1,35.00,35.00,,\nT1,3,NOPE,1,1,1,,\nT1,4,FLO-001,0,35,35,,\nT1,5,FLO-001,1,35.00,40.00,,\n"
    r = import_sale_items(session, bad)
    assert r.inserted == 3
    assert len(r.errors) == 4
    assert "unknown transaction_id" in r.errors[0]
    assert "unknown sku" in r.errors[1]
    assert "quantity must be positive" in r.errors[2]
    assert "exceeds regular_price" in r.errors[3]


def test_missing_required_column_is_fatal(session):
    with pytest.raises(ImportError_):
        import_products(session, b"sku,name\nX,Y\n")


def test_promotions_inventory_expenses(session):
    import_products(session, PRODUCTS)
    r = import_promotions(session, b"name,start_date,end_date,discount_type,discount_value,eligible_skus,eligible_category\nFlower Friday,2026-09-04,2026-09-06,percent,30,,Flower\nBad,2026-09-04,2026-09-01,percent,10,,\n")
    assert r.inserted == 1 and "precedes" in r.errors[0]
    r = import_inventory(session, b"store,sku,snapshot_date,quantity_on_hand,unit_cost,received_date,last_sale_date\nMAIN,FLO-001,2026-09-11,40,14.00,2026-08-01,2026-09-10\nMAIN,FLO-001,2026-09-11,41,14.00,2026-08-01,\n")
    assert (r.inserted, r.updated) == (1, 1)  # same store/sku/date twice -> update
    r = import_expenses(session, b"expense_id,store,expense_date,category,vendor,description,amount\nX1,MAIN,09/07/2026,Rent,Harbor,Weekly,\"2,650.00\"\n")
    assert r.inserted == 1 and r.errors == []


def test_external_intelligence_imports(session):
    r = import_stores(session, b"code,name,latitude,longitude,timezone\nMAIN,Main St,28.5383,-81.3792,America/New_York\n")
    assert r.inserted == 1
    store = session.execute(select(Store).where(Store.code == "MAIN")).scalar_one()
    assert store.latitude == 28.5383

    events = (b"event_id,store,event_type,source,latitude,longitude,affected_radius_km,start_time,end_time,severity,description,confidence,source_reference,is_forecast\n"
              b"EV1,MAIN,utility,utility-co,,,,2026-09-03T14:00:00,2026-09-03T17:00:00,major,Power outage,1,,0\n"
              b"EV2,,traffic,dot,28.54,-81.38,3,2026-09-05T08:00:00,2026-09-05T18:00:00,moderate,Road closure,0.9,,0\n"
              b"EV3,,traffic,dot,,,3,2026-09-05T08:00:00,2026-09-05T18:00:00,moderate,No location,1,,0\n"
              b"EV4,MAIN,alien,x,,,,2026-09-05T08:00:00,2026-09-05T18:00:00,moderate,Bad type,1,,0\n")
    r = import_external_events(session, events)
    assert r.inserted == 2
    assert len(r.errors) == 2
    assert "needs either a store or latitude" in r.errors[0]
    assert "event_type must be one of" in r.errors[1]
    ev2 = session.execute(select(ExternalEvent).where(ExternalEvent.event_id == "EV2")).scalar_one()
    assert ev2.store_id is None and ev2.confidence == Decimal("0.9000")

    r = import_weather(session, b"store,observed_at,is_forecast,temperature_f,precipitation_in,snowfall_in,wind_mph,condition,alert,source\nMAIN,2026-09-03T14:00:00,0,91.4,0.25,,12,rain,,nws\nMAIN,2026-09-12T14:00:00,1,88,0,,8,cloudy,,nws\nMAIN,2026-09-03T14:00:00,0,92,0.30,,12,rain,,nws\n")
    assert (r.inserted, r.updated) == (2, 1)
    obs = session.execute(select(WeatherObservation).where(WeatherObservation.is_forecast == 0)).scalar_one()
    assert obs.precipitation_in == Decimal("0.300") and obs.snowfall_in == Decimal("0")
