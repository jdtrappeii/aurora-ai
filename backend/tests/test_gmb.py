"""Google Business Profile export -> stores: postal-code and locality matching, fields filled, coordinates untouched."""
from datetime import date

from sqlalchemy import select

from app.importers.gmb import import_gmb_locations, match_store
from app.models import Store

CSV = b"""Google updates,Status,Store code,Business name,Address line 1,Locality,Administrative area,Country / Region,Postal code,Primary phone,Monday hours,Sunday hours,Opening date
,Published,P13FL9,Demo Brand Neighborhood Dispensary Edgewater,1308 S Ridgewood Ave,Edgewater,FL,US,32132,(386) 287-5539,09:00-19:00,10:00-18:00,2025-05-21
,Published,P13FL8,Demo Brand Neighborhood Dispensary Daytona,1027 N Nova Rd,Holly Hill,FL,US,32117,(386) 555-0100,09:00-19:00,10:00-18:00,2024-11-01
,Published,P13FL99,Demo Brand Somewhere New,1 New St,Nowhere,FL,US,00000,,09:00-19:00,,2026-01-01
,Not published,P13FL5,Draft listing,2 Draft St,Draft,FL,US,33333,,,,
"""


def test_import_matches_by_zip_and_locality(session):
    session.add_all([
        Store(code="HS10003", name="FL - Demo - Edgewater", address="FL 32132", state="FL", latitude=29.0, longitude=-81.0),
        Store(code="HS10004", name="FL - Demo - Daytona", address="FL 32117", state="FL"),
        Store(code="HS2328", name="NV - Demo Brand - Vegas (REC)", address="NV 89109", state="NV"),
    ])
    session.commit()
    r = import_gmb_locations(session, CSV)
    assert (r.updated, r.skipped) == (2, 2)
    assert any("Somewhere New" in e for e in r.errors) and len(r.errors) == 1  # the draft is skipped silently
    edge = session.execute(select(Store).where(Store.code == "HS10003")).scalar_one()
    assert edge.address == "1308 S Ridgewood Ave, Edgewater, FL 32132"
    assert edge.gmb_code == "P13FL9" and edge.phone == "(386) 287-5539" and edge.opened_on == date(2025, 5, 21)
    assert edge.hours == '{"mon": "09:00-19:00", "sun": "10:00-18:00"}'
    assert (edge.latitude, edge.longitude) == (29.0, -81.0)  # never overwritten
    daytona = session.execute(select(Store).where(Store.code == "HS10004")).scalar_one()
    assert daytona.address.startswith("1027 N Nova Rd, Holly Hill") and daytona.opened_on == date(2024, 11, 1)
    assert import_gmb_locations(session, CSV).updated == 2  # idempotent


def test_match_store_prefers_zip_then_locality():
    a = Store(code="A", name="FL - Demo - Tampa Kennedy", address="x, Tampa, FL 33606", state="FL")
    b = Store(code="B", name="FL - Demo - Tampa Bruce B Downs", address="y, Tampa, FL 33647", state="FL")
    assert match_store([a, b], "33647", "Tampa", "FL") is b
    assert match_store([a, b], None, "Tampa Kennedy", "FL") is a
    assert match_store([a, b], None, "Tampa", "FL") is None  # ambiguous: never guess
