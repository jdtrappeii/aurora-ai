"""Spreadsheet sources: reader, OMMU market rows, competitor deals, promotions
workbook, sync orchestration. Network is httpx.MockTransport only."""
import io
import json
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import openpyxl
import pytest
from sqlalchemy import select

from app.analytics.discounts import promotion_feed
from app.analytics.market import competitor_pressure, market_context
from app.analytics.periods import Period
from app.analytics.promotions import promotion_results
from app.importers.market import deals_to_events, import_market_rows, market_competition_events, offer_severity
from app.importers.promotions_sheet import active_days, import_promotion_rows, infer_type_value, parse_weekdays
from app.integrations.events.common import ProviderError
from app.integrations.events.sync import upsert_events
from app.integrations.sheets import google_sheet_csv_url, norm_header, onedrive_direct_url, read_table, rows_from_grid
from app.integrations.sheets_sync import sheets_sync
from app.models import DiscountDaily, ExternalEvent, MarketWeekly, Promotion, Store

OMMU_CSV = b"""row_id,week_ending,report_date,source_url,mmtc_name_canonical,is_p13_fl,dispensing_locations,medical_marijuana_mg_thc,low_thc_cannabis_mg_cbd,marijuana_smoking_oz,share_thc_pct,share_flower_pct,share_locations_pct,statewide_qualified_patients,is_totals_row
2026-09-03__trulieve,2026-09-03,9/4/2026,https://x/0903.pdf,Trulieve,FALSE,165,1.20E+08,0,50000,26.0,33.0,22.0,941000,FALSE
2026-09-03__p13,2026-09-03,9/4/2026,https://x/0903.pdf,Planet 13 Florida Cannabis for the Planet,TRUE,34,7000000,0,3300,2.2,2.9,4.5,941000,FALSE
2026-09-03__sunburn,2026-09-03,9/4/2026,https://x/0903.pdf,Sunburn,FALSE,20,6500000,0,3100,2.1,2.4,2.7,941000,FALSE
2026-09-10__trulieve,2026-09-10,9/11/2026,https://x/0910.pdf,Trulieve,FALSE,165,1.10E+08,0,48000,25.0,32.0,22.0,941271,FALSE
2026-09-10__p13,2026-09-10,9/11/2026,https://x/0910.pdf,Planet 13 Florida Cannabis for the Planet,TRUE,34,7350000,0,3400,2.29,3.19,4.5,941271,FALSE
2026-09-10__sunburn,2026-09-10,9/11/2026,https://x/0910.pdf,Sunburn,FALSE,24,7000000,0,3300,2.19,2.5,3.2,941271,FALSE
"""
DEALS_CSV = b"""row_id,deal_id,week_ending,operator_canonical,operator_display,observation_source,observed_at_utc,source_url_or_msg_id,subject,offer_type,offer_value,hook,audience,confidence
r1,d1,2026-09-10,Sunburn,Sunburn,flcd_site,2026-09-08T10:40:00.000Z,https://deals/1,SUNBURN 40% OFF FLOWER,bundle,40% off,urgency,general,high
r2,d2,2026-09-10,Mint Cannabis,Mint,flcd_site,2026-09-09T10:40:00.000Z,https://deals/2,MINT,value,(unparsed offer),,general,low
r3,d3,2026-09-10,Planet 13 Florida Cannabis for the Planet,Planet 13,flcd_site,2026-09-09T10:40:00.000Z,https://deals/3,OURS,value,30% off,,general,high
r4,d4,2026-09-10,Curaleaf Florida LLC,Curaleaf,gmail,2026-09-09T10:40:00.000Z,msg-9,Curaleaf BOGO,bogo,BOGO pre-rolls,urgency,members,medium
r5,d5,2026-09-10,GrowHealthy,GrowHealthy,flcd_site,,https://deals/5,no date,value,15% off,,general,high
r6,d6,2026-09-03,Sunburn,Sunburn,flcd_site,2026-08-30T10:40:00.000Z,https://deals/6,SUNBURN 20% OFF,value,20% off,,general,high
"""


def mock(router):
    def handler(req):
        status, body, headers = router(req)
        return httpx.Response(status, content=body, headers=headers)
    return httpx.Client(transport=httpx.MockTransport(handler))


def settings(**over):
    base = dict(market_sheet_id="", market_sheet_tab="", market_self_operator="", deals_sheet_id="", deals_sheet_tab="",
                promotions_url="", promotions_sheet="", promotions_column_map="", market_centroid_lat=28.1, market_centroid_lon=-81.6)
    base.update(over)
    return SimpleNamespace(**base)


def promo_xlsx(rows, headers=("Promo", "Start Date", "End Date", "Discount", "Days", "Stores", "POS Discount Name", "Category", "Notes")):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Promos"
    ws.append(["Planet 13 FL promotions calendar"])  # a title row above the header
    ws.append([])
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- reader ----------

def test_header_normalisation_and_grid_detection():
    assert norm_header(" POS Discount Name ") == "pos_discount_name"
    assert norm_header("[merged] Week of") == "week_of"
    grid = [["Title"], [], ["A", "B", "C"], [1, "", 3], [None, None, None], ["x", "y", "z"]]
    rows = rows_from_grid(grid)
    assert rows == [{"a": 1, "b": "", "c": 3}, {"a": "x", "b": "y", "c": "z"}]


def test_read_csv_and_xlsx():
    rows = read_table(OMMU_CSV, "m.csv")
    assert rows[0]["mmtc_name_canonical"] == "Trulieve" and len(rows) == 6
    data = promo_xlsx([("Try it Tuesday", date(2026, 9, 1), None, "60% OFF ALL 1G Distillate Vapes", "Tuesday", "", "DD - Auto - Try it Tuesday: 60% OFF ALL 1G Distillate Vapes!", "Vape Cart 1g", "")])
    rows = read_table(data, "promos.xlsx", "Promos")
    assert rows[0]["promo"] == "Try it Tuesday" and rows[0]["start_date"] == datetime(2026, 9, 1)
    assert "pos_discount_name" in rows[0]


def test_source_urls():
    assert google_sheet_csv_url("ID1", "1234") == "https://docs.google.com/spreadsheets/d/ID1/gviz/tq?tqx=out:csv&gid=1234"
    assert google_sheet_csv_url("ID1", "raw weekly").endswith("&sheet=raw weekly")
    u = onedrive_direct_url("https://1drv.ms/x/s!abc")
    assert u.startswith("https://api.onedrive.com/v1.0/shares/u!") and u.endswith("/root/content") and "=" not in u


def test_google_sign_in_page_is_an_error():
    with mock(lambda req: (200, b"<!DOCTYPE html><html>sign in</html>", {"content-type": "text/html"})) as http:
        from app.integrations.sheets import google_sheet_csv
        with pytest.raises(ProviderError, match="anyone with the link"):
            google_sheet_csv(http, "ID", None)


# ---------- market ----------

def test_market_rows_context_and_competition_events(session):
    rows = read_table(OMMU_CSV, "m.csv")
    r = import_market_rows(session, rows)
    assert (r.inserted, r.skipped, r.errors) == (6, 0, [])
    assert import_market_rows(session, rows).updated == 6
    p13 = session.execute(select(MarketWeekly).where(MarketWeekly.is_self == 1, MarketWeekly.week_ending == date(2026, 9, 10))).scalar_one()
    assert p13.mg_thc == Decimal("7350000.00") and p13.share_flower_pct == Decimal("3.1900")

    ctx = market_context(session, date(2026, 9, 15))
    assert ctx["current"]["week_ending"] == "2026-09-10"
    # market: 110M + 7.35M + 7M = 124.35M vs 120M + 7M + 6.5M = 133.5M -> -6.85%
    assert ctx["current"]["market_mg_thc"] == Decimal("124350000.00")
    assert ctx["vs_previous_week"]["market_mg_thc_pct"] == Decimal("-0.0685")
    assert ctx["vs_previous_week"]["self_mg_thc_pct"] == Decimal("0.0500")  # 7.35 / 7.0
    assert ctx["vs_previous_week"]["share_thc_bps"] == Decimal("9.0000")  # 2.29 - 2.20 = 0.09 pts = 9 bps
    assert ctx["vs_previous_week"]["share_flower_bps"] == Decimal("29.0000")
    assert ctx["vs_previous_week"]["market_dispensaries_delta"] == 4 and ctx["vs_previous_week"]["patients_delta"] == 271
    assert ctx["read"].startswith("Outpaced the market by 11.9 points")
    assert market_context(session, date(2026, 8, 1)) is None

    drafts = market_competition_events(session, (28.1, -81.6))
    assert len(drafts) == 1
    d = drafts[0]
    assert d.event_id == "ommu:2026-09-10:sunburn:+4" and d.severity == "major" and d.event_type == "competition"
    assert d.start_time == datetime(2026, 9, 4, 0, 0) and d.affected_radius_km == 1000.0
    assert "Sunburn added 4 dispensing locations (20 → 24)" in d.description


# ---------- deals ----------

def test_offer_severity():
    assert offer_severity("40% off", "bundle") == ("major", Decimal("40"))
    assert offer_severity("20% off", None) == ("moderate", Decimal("20"))
    assert offer_severity("15% off", None) == ("minor", Decimal("15"))
    assert offer_severity("BOGO pre-rolls", "bogo")[0] == "major"
    assert offer_severity("$25 eighths", None) == ("moderate", None)
    assert offer_severity("free stickers", "freebie") == ("minor", None)


def test_deals_to_events_and_pressure(session):
    session.add(Store(code="HS1", name="One", latitude=28.0, longitude=-82.0))
    session.commit()
    rows = read_table(DEALS_CSV, "d.csv")
    rep = deals_to_events(rows, (28.1, -81.6), "Planet 13 Florida Cannabis for the Planet")
    assert rep.to_dict() == {"events": 3, "skipped_self": 1, "skipped_unparsed": 1, "skipped_no_date": 1}
    by = {d.event_id: d for d in rep.drafts}
    assert by["deal:d1"].severity == "major" and by["deal:d1"].confidence == Decimal("0.9")
    assert by["deal:d4"].severity == "major" and by["deal:d4"].confidence == Decimal("0.7")
    assert by["deal:d1"].start_time == datetime(2026, 9, 8) and by["deal:d1"].end_time == datetime(2026, 9, 15, 23, 59)
    assert upsert_events(session, rep.drafts, "deal-intel").inserted == 3

    week = Period("w", date(2026, 9, 7), date(2026, 9, 13))
    p = competitor_pressure(session, week)
    assert p["total_deals"] == 2 and p["previous_total_deals"] == 1 and p["vs_previous_pct"] == Decimal("1.0000")
    ops = {o["operator"]: o for o in p["operators"]}
    assert ops["Sunburn"]["deals"] == 1 and ops["Sunburn"]["previous_deals"] == 1 and ops["Sunburn"]["major"] == 1
    assert ops["Curaleaf Florida LLC"]["previous_deals"] == 0 and ops["Curaleaf Florida LLC"]["vs_previous_pct"] is None


# ---------- promotions ----------

def test_weekday_and_type_parsing():
    assert parse_weekdays("Tuesday") == "2" and parse_weekdays("tue, thu") == "2,4"
    assert parse_weekdays("Mon-Wed") == "1,2,3" and parse_weekdays("Fri - Mon") == "1,5,6,7"
    assert parse_weekdays("Daily") is None and parse_weekdays("") is None and parse_weekdays("weekends") == "6,7"
    assert infer_type_value(None, "60% OFF ALL 1G Distillate Vapes") == ("percent", Decimal("60"))
    assert infer_type_value(None, "$10 Off 3.5g & 7g P13 Flower") == ("amount", Decimal("10"))
    assert infer_type_value("bogo", "Buy one get one pre-rolls") == ("bogo", Decimal("1"))
    assert infer_type_value("percent", 35) == ("percent", Decimal("35"))
    assert infer_type_value(None, 12.5) == ("percent", Decimal("12.5"))


def test_promotions_import_and_feed(session):
    session.add_all([Store(code="HS10136", name="FL - Planet 13 - Pace"), Store(code="HS10132", name="FL - Planet 13 - Tampa Kennedy")])
    session.commit()
    data = promo_xlsx([
        ("Try it Tuesday", date(2026, 9, 1), None, "60% OFF ALL 1G Distillate Vapes", "Tuesday", "", "DD - Auto - Try it Tuesday: 60% OFF ALL 1G Distillate Vapes!", "Vape Cart 1g", ""),
        ("Pace Grand Reopening", "9/12/2026", "9/13/2026", "$10 off", "", "Pace", "DD - Auto - Pace $10", "", "two days"),
        ("Bad row", None, None, "10%", "", "", "", "", ""),
        ("Nowhere deal", date(2026, 9, 1), date(2026, 9, 2), "5%", "", "Atlantis", "", "", ""),
        ("Pace Grand Reopening", "9/12/2026", "9/13/2026", "$10 off", "", "Tampa Kennedy", "DD - Auto - Pace $10", "", ""),
    ])
    rows = read_table(data, "promos.xlsx")
    r = import_promotion_rows(session, rows)
    assert (r.inserted, r.updated, r.skipped) == (3, 1, 1)
    assert any("no start date" in e for e in r.errors) and any("Atlantis" in e for e in r.errors)
    tt = session.execute(select(Promotion).where(Promotion.name == "Try it Tuesday")).scalar_one()
    assert (tt.discount_type, tt.discount_value, tt.weekdays, tt.store_codes) == ("percent", Decimal("60"), "2", None)
    assert tt.end_date == date(2027, 9, 1) and "open-ended" in tt.notes and tt.source == "sheet"
    assert tt.discount_names == "DD - Auto - Try it Tuesday: 60% OFF ALL 1G Distillate Vapes!"
    pg = session.execute(select(Promotion).where(Promotion.name == "Pace Grand Reopening")).scalar_one()
    assert sorted(pg.store_codes.split("|")) == ["HS10132", "HS10136"]  # two rows merged
    assert active_days(tt, date(2026, 9, 1), date(2026, 9, 30)) == [date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29)]

    # feed join: two Tuesdays of Headset discount rows at Pace, plus a Wednesday row that must not count
    pace = session.execute(select(Store).where(Store.code == "HS10136")).scalar_one()
    for d, disc, rev, t in ((date(2026, 9, 8), "3000", "2000", 60), (date(2026, 9, 15), "3888", "2482.04", 70), (date(2026, 9, 16), "999", "1", 1)):
        session.add(DiscountDaily(store_id=pace.id, sale_date=d, discount_name="DD - Auto - Try it Tuesday: 60% OFF ALL 1G Distillate Vapes!",
                                  discount_total=Decimal(disc), revenue=Decimal(rev), units=100, transaction_count=t))
    session.commit()
    feed = promotion_feed(session, tt, "HS10136")
    assert feed["window"]["discount_total"] == Decimal("6888.00") and feed["window"]["days_with_data"] == 2
    assert feed["window"]["discount_per_day"] == Decimal("3444.00") and feed["window"]["tickets_per_day"] == Decimal("65.0000")
    assert feed["window"]["discount_depth"] == Decimal("0.6058")  # 6888 / (4482.04 + 6888)
    assert feed["baseline"] is None  # nothing before Sep 1
    assert promotion_feed(session, tt, "HS10132")["window"]["days_with_data"] == 0
    assert promotion_feed(session, pg, "HS10136")["applies_to_store"] is True
    results = {p["promotion"]: p for p in promotion_results(session, "HS10136")}
    assert results["Try it Tuesday"]["weekdays"] == "2" and results["Try it Tuesday"]["feed"]["window"]["discount_total"] == Decimal("6888.00")
    assert results["Try it Tuesday"]["verdict"] == "no_baseline"  # no line-level data: the POS path stays honest


def test_column_map_override(session):
    data = promo_xlsx([("X", date(2026, 1, 1), date(2026, 1, 2), "10%", "", "", "", "", "")], headers=("Deal Title", "Live From", "Live To", "Pct", "", "", "", "", ""))
    rows = read_table(data, "p.xlsx")
    r = import_promotion_rows(session, rows, json.dumps({"name": "Deal Title", "start": "Live From", "end": "Live To", "value": "Pct"}))
    assert r.inserted == 1
    p = session.execute(select(Promotion)).scalar_one()
    assert p.name == "X" and p.end_date == date(2026, 1, 2) and p.discount_type == "percent"


# ---------- sync ----------

def test_sheets_sync_end_to_end(session):
    session.add(Store(code="HS10136", name="FL - Planet 13 - Pace", latitude=30.6, longitude=-87.16))
    session.commit()
    xlsx = promo_xlsx([("Try it Tuesday", date(2026, 9, 1), None, "60% off vapes", "Tue", "", "DD - Auto - Try it Tuesday", "", "")])

    def router(req):
        url = str(req.url)
        if "docs.google.com" in url and "MARKET" in url:
            assert "gid=77" in url
            return 200, OMMU_CSV, {"content-type": "text/csv"}
        if "docs.google.com" in url and "DEALS" in url:
            assert "sheet=deals" in url
            return 200, DEALS_CSV, {"content-type": "text/csv"}
        if "api.onedrive.com" in url:
            return 200, xlsx, {"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        return 404, b"", {}

    cfg = settings(market_sheet_id="MARKET", market_sheet_tab="77", deals_sheet_id="DEALS", deals_sheet_tab="deals_library",
                   market_self_operator="Planet 13 Florida Cannabis for the Planet",
                   promotions_url="https://planet13-my.sharepoint.com/:x:/g/personal/x/abc?e=1", promotions_sheet="Promos")
    with mock(router) as http:
        rep = sheets_sync(session, http, cfg)
    assert rep.ran == ["market", "deals", "promotions"] and rep.warnings == [] and rep.skipped == {}
    assert rep.details["market"] == {"tab": "77", "rows": 6, "competition_events": 1}
    assert rep.details["deals"]["events"] == 3
    assert rep.details["promotions"]["rows"] == 1 and "pos_discount_name" in rep.details["promotions"]["headers"]
    kinds = {}
    for ev in session.execute(select(ExternalEvent)).scalars():
        kinds[ev.source] = kinds.get(ev.source, 0) + 1
    assert kinds == {"ommu": 1, "deal-intel": 3}
    assert session.execute(select(Promotion)).scalar_one().weekdays == "2"


def _workbook(tabs: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, grid in tabs.items():
        ws = wb.create_sheet(name)
        for row in grid:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_sync_finds_tabs_by_headers_when_no_tab_is_configured(session):
    import csv as _csv
    market_grid = [r for r in _csv.reader(io.StringIO(OMMU_CSV.decode()))]
    deals_grid = [r for r in _csv.reader(io.StringIO(DEALS_CSV.decode()))]
    market_xlsx = _workbook({"Dashboard": [["Last refresh"], ["x", "y"]], "raw_weekly": market_grid, "Trends": [["week", "a", "b"]]})
    deals_xlsx = _workbook({"Briefing": [["hello"]], "deals_library": deals_grid})
    promo_xlsx_bytes = _workbook({"Cover": [["Planet 13 promos"]], "FL Deals": [["Promo", "Start Date", "End Date", "Discount", "Days"], ["Try it Tuesday", date(2026, 9, 1), None, "60% off", "Tue"]]})
    seen = []

    def router(req):
        url = str(req.url)
        seen.append(url)
        if "MARKET/export?format=xlsx" in url:
            return 200, market_xlsx, {"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        if "DEALS/export?format=xlsx" in url:
            return 200, deals_xlsx, {"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        if "sharepoint.com" in url and "download=1" in url:
            return 200, promo_xlsx_bytes, {"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        return 404, b"", {}

    cfg = settings(market_sheet_id="MARKET", deals_sheet_id="DEALS", market_self_operator="Planet 13 Florida Cannabis for the Planet",
                   promotions_url="https://netorg-my.sharepoint.com/:x:/g/personal/someone/IQBabc?e=nsvmdG")
    with mock(router) as http:
        rep = sheets_sync(session, http, cfg)
    assert rep.warnings == [] and rep.ran == ["market", "deals", "promotions"]
    assert rep.details["market"]["tab"] == "raw_weekly" and rep.details["market"]["rows"] == 6
    assert rep.details["deals"]["tab"] == "deals_library" and rep.details["deals"]["events"] == 3
    assert rep.details["promotions"]["tab"] == "FL Deals" and rep.details["promotions"]["rows"] == 1
    assert any("sharepoint.com" in u and "download=1" in u and "e=nsvmdG" in u for u in seen)
    assert session.execute(select(Promotion)).scalar_one().weekdays == "2"


def test_sync_reports_missing_tab_columns(session):
    wb = _workbook({"Only": [["a", "b", "c"], [1, 2, 3]]})
    with mock(lambda req: (200, wb, {"content-type": "application/octet-stream"})) as http:
        rep = sheets_sync(session, http, settings(market_sheet_id="M"), which={"market"})
    assert rep.ran == [] and "no tab has the columns" in rep.warnings[0] and "['Only']" in rep.warnings[0]


def test_sheets_sync_skips_unconfigured_and_reports_errors(session):
    with mock(lambda req: (403, b"forbidden", {})) as http:
        rep = sheets_sync(session, http, settings(deals_sheet_id="X"))
    assert set(rep.skipped) == {"market", "promotions"} and rep.ran == [] and "deals: " in rep.warnings[0] and "403" in rep.warnings[0]
