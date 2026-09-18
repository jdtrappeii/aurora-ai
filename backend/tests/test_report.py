"""Weekly owner report: structure from a hand ledger, store ranking math, HTML
and text rendering, the API route, the add-column migration, and the email
sender's off switch."""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from app.analytics.periods import Period
from app.analytics.report import store_ranking, weekly_report
from app.db import Base, init_db
from app.models import Store
from app.reports.mail import send_report
from app.reports.render import render_html, render_text
from tests.conftest import MONDAY, dt

SUNDAY = MONDAY + timedelta(days=6)
LAST_MONDAY = MONDAY - timedelta(days=7)


def ledger(seed):
    """This week: A 4x@25 (cost 10) + B 2x@10 (cost 4) = rev 120, GP 72.
    Last week: A 2x@25 + B 6x@10 = rev 110, GP 66.  Category Flower up, Edibles down."""
    seed.product("A", "Flower", "10", "25")
    seed.product("B", "Edibles", "4", "10")
    seed.sale(dt(LAST_MONDAY, 10), [("A", 2, "25"), ("B", 6, "10")])
    seed.sale(dt(MONDAY, 10), [("A", 4, "25")])
    seed.sale(dt(MONDAY + timedelta(days=2), 12), [("B", 2, "10")])
    seed.inventory("A", MONDAY, 100, MONDAY - timedelta(days=120))  # aged stock, 4 sold in 30d -> slow
    seed.promotion("Flower Friday", MONDAY, SUNDAY, category="Flower")
    seed.commit()


def test_weekly_report_structure_and_math(session, seed):
    ledger(seed)
    r = weekly_report(session, SUNDAY)
    assert r["period"] == {"label": "current_week", "start": MONDAY.isoformat(), "end": SUNDAY.isoformat(), "days": 7}
    h = r["headline"]
    assert h["current"]["revenue"] == Decimal("120.00") and h["previous"]["revenue"] == Decimal("110.00")
    assert h["vs_previous_week"]["gross_profit"]["pct"] == Decimal("0.0909")  # 72 vs 66
    assert h["read"].startswith("Revenue $120 (+9.1% vs last week), gross profit $72 (+9.1%).")
    cats = r["movers"]["categories"]
    assert cats["up"][0]["category"] == "Flower" and cats["up"][0]["delta"] == Decimal("30.00")   # 60 - 30
    assert cats["down"][0]["category"] == "Edibles" and cats["down"][0]["delta"] == Decimal("-24.00")  # 12 - 36
    assert r["stores"][0]["store"] == "MAIN" and r["stores"][0]["gross_profit_delta"] == Decimal("6.00")
    assert r["promotions"][0]["promotion"] == "Flower Friday"
    assert r["inventory"]["cash_tied_over_90_days"] == Decimal("1000.00")
    assert r["inventory"]["watch"][0]["sku"] == "A" and r["inventory"]["watch"][0]["status"] == "slow"
    assert r["coverage"] == {"sales_days_missing_detail": [], "weather": False, "market": False, "promotions": 1, "stores_reporting": 1}
    assert r["next_week"]["start"] == (SUNDAY + timedelta(days=1)).isoformat()


def test_partial_week_compares_same_weekdays(session, seed):
    ledger(seed)
    r = weekly_report(session, MONDAY + timedelta(days=1))  # Tuesday
    assert r["is_partial"] and r["period"]["days"] == 2 and r["previous_period"]["days"] == 2
    assert r["headline"]["current"]["revenue"] == Decimal("100.00")  # Wednesday's B sale excluded


def test_store_ranking_orders_by_gross_profit_change(session, seed):
    ledger(seed)
    other = Store(code="B2", name="Second")
    session.add(other)
    session.commit()
    rows = store_ranking(session, Period("c", MONDAY, SUNDAY), Period("p", LAST_MONDAY, LAST_MONDAY + timedelta(days=6)))
    assert [r["store"] for r in rows] == ["MAIN"]  # a store with no sales in either period is left out
    assert rows[0]["revenue_pct"] == Decimal("0.0909")


def test_render_html_and_text(session, seed):
    ledger(seed)
    r = weekly_report(session, SUNDAY)
    html = render_html(r)
    assert "<!doctype html>" in html and "Aurora weekly · all stores" in html
    for needle in ("$120", "Flower Friday", "Categories up", "Dead and slow stock", "Data coverage", "Next week"):
        assert needle in html, needle
    assert "<script" not in html
    text = render_text(r)
    assert text.startswith("AURORA WEEKLY") and "↑ Flower: $30" in text and "COVERAGE: complete" in text


def test_report_route(session, seed, engine):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.db import get_session
    from app.main import app

    ledger(seed)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app.dependency_overrides[get_session] = lambda: (yield factory())
    with TestClient(app) as c:
        j = c.get("/api/report/weekly", params={"as_of": SUNDAY.isoformat()}).json()
        assert j["headline"]["current"]["revenue"] == 120.0
        h = c.get("/api/report/weekly", params={"as_of": SUNDAY.isoformat(), "format": "html"})
        assert h.status_code == 200 and "text/html" in h.headers["content-type"] and "Flower Friday" in h.text
    app.dependency_overrides.clear()


def test_init_db_adds_missing_columns():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    with eng.begin() as conn:  # an old promotions table from before recurrence / scope existed
        conn.execute(text("CREATE TABLE promotions (id INTEGER PRIMARY KEY, name VARCHAR(128) UNIQUE, start_date DATE, end_date DATE, "
                          "discount_type VARCHAR(16), discount_value NUMERIC(12,4), eligible_skus TEXT, eligible_category VARCHAR(64))"))
        conn.execute(text("INSERT INTO promotions (name, start_date, end_date, discount_type, discount_value) VALUES ('old', '2026-01-01', '2026-01-02', 'percent', 10)"))
    applied = init_db(eng)
    cols = {c["name"] for c in inspect(eng).get_columns("promotions")}
    assert {"weekdays", "store_codes", "discount_names", "audience", "notes", "source"} <= cols
    assert any("ADD COLUMN source VARCHAR(32) DEFAULT 'manual'" in a for a in applied)
    with eng.connect() as conn:
        assert conn.execute(text("SELECT source FROM promotions")).scalar_one() == "manual"
    assert init_db(eng) == []  # idempotent
    eng.dispose()


def test_send_report_is_off_without_smtp():
    s = SimpleNamespace(smtp_host="", report_to="a@b.c", smtp_user="", smtp_from="")
    assert send_report(s, "x", "<b/>", "x")["sent"] is False
    s = SimpleNamespace(smtp_host="mail", report_to="", smtp_user="", smtp_from="")
    assert send_report(s, "x", "<b/>", "x")["reason"].startswith("SMTP_HOST or REPORT_TO")
