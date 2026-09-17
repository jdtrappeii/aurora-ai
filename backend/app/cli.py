"""Aurora command line.

    python -m app.cli init-db
    python -m app.cli import-dir ../sample_data
    python -m app.cli weekly [--as-of 2026-09-11] [--store MAIN]
    python -m app.cli external [--as-of 2026-09-11] [--store MAIN]
    python -m app.cli discounts [--as-of 2026-09-11] [--store HS10136]

Headset connector:
    python -m app.cli headset-sync --start 2026-09-01 --end 2026-09-14 [--stores "FL -"] [--record data/headset]
    python -m app.cli headset-import-dir data/headset          # replay recorded pulls
    python -m app.cli headset-reconcile [--store HS10136]      # product lines vs store-day totals

External events (free stack):
    python -m app.cli geocode-stores                            # Nominatim, stores with an address and no coordinates
    python -m app.cli events-sync --start 2026-09-01 --end 2026-10-15   # Ticketmaster / SeatGeek / FL511 / calendar
    python -m app.cli heartbeat-events [--min-gap 5]           # silences between heartbeat runs -> outage events
    python -m app.cli heartbeat-status
    python -m app.cli weather-sync --start 2026-06-01 --end 2026-10-01 [--store HS10136] [--no-alerts]

Spreadsheets (OMMU market dashboard, competitor deals, promotions workbook):
    python -m app.cli sheets-sync [--only market|deals|promotions]
    python -m app.cli sheets-headers <share link or path> [--tab NAME]   # to configure PROMOTIONS_COLUMN_MAP
    python -m app.cli market [--as-of 2026-09-11]
"""
import argparse
import json
from datetime import date
from decimal import Decimal

from app.analytics.comparisons import weekly_comparison
from app.analytics.discounts import discount_report
from app.analytics.external import event_findings, proactive_forecast, weather_intelligence
from app.analytics.periods import week_containing
from app.config import settings
from app.db import SessionLocal, init_db
from app.importers.csv_importer import import_directory
from app.importers.headset import import_headset_directory, reconcile
from app.integrations import heartbeat as hb
from app.integrations.events.sync import events_sync, upsert_events
from app.integrations.geocode import geocode_stores
from app.integrations.weather import weather_sync
from app.integrations.sheets_sync import sheets_sync, show_headers
from app.analytics.market import competitor_pressure, market_context


def _json(obj) -> str:
    return json.dumps(obj, indent=2, default=lambda o: str(o) if isinstance(o, Decimal) else o)


def _print_results(results) -> None:
    for r in results:
        print(f"{r.kind:48s} inserted={r.inserted:6d} updated={r.updated:6d} errors={len(r.errors)}")
        for e in r.errors[:10]:
            print("   !", e)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aurora")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    p_imp = sub.add_parser("import-dir")
    p_imp.add_argument("directory")
    for name in ("weekly", "external", "discounts", "market"):
        p = sub.add_parser(name)
        p.add_argument("--as-of", default=None)
        p.add_argument("--store", default=None)
    p_hs = sub.add_parser("headset-sync", help="pull a date range from the Headset MCP server")
    p_hs.add_argument("--start", required=True)
    p_hs.add_argument("--end", required=True)
    p_hs.add_argument("--stores", default=None, help="substring filter on Headset store names, e.g. 'FL -'")
    p_hs.add_argument("--record", default=None, help="directory to write the raw pulls (default: HEADSET_DATA_DIR)")
    p_hs.add_argument("--no-record", action="store_true")
    p_hs.add_argument("--no-inventory", action="store_true")
    p_hs.add_argument("--full-catalog", action="store_true", help="walk every category so zero-stock SKUs are classified")
    p_hs.add_argument("--no-products", action="store_true")
    p_hs.add_argument("--no-discounts", action="store_true")
    p_hd = sub.add_parser("headset-import-dir", help="replay recorded Headset pulls")
    p_hd.add_argument("directory")
    p_hr = sub.add_parser("headset-reconcile")
    p_hr.add_argument("--store", default=None)
    p_geo = sub.add_parser("geocode-stores", help="fill missing store coordinates from their address (Nominatim)")
    p_geo.add_argument("--store", action="append", default=None, help="limit to these store codes")
    p_ev = sub.add_parser("events-sync", help="pull local events, traffic and the calendar into external_events")
    p_ev.add_argument("--start", required=True)
    p_ev.add_argument("--end", required=True)
    p_ev.add_argument("--no-calendar", action="store_true")
    p_hb = sub.add_parser("heartbeat-events", help="turn heartbeat silences into utility / connectivity events")
    p_hb.add_argument("--min-gap", type=int, default=None, help="minutes (default HEARTBEAT_GAP_MINUTES)")
    p_hb.add_argument("--open-after", type=int, default=None, help="also flag stores silent right now for this many minutes")
    sub.add_parser("heartbeat-status")
    p_wx = sub.add_parser("weather-sync", help="Open-Meteo hourly history + forecast and NWS alerts per store")
    p_wx.add_argument("--start", required=True)
    p_wx.add_argument("--end", required=True)
    p_wx.add_argument("--store", action="append", default=None)
    p_wx.add_argument("--no-alerts", action="store_true")
    p_ss = sub.add_parser("sheets-sync", help="read the market, deals and promotions spreadsheets")
    p_ss.add_argument("--only", action="append", choices=["market", "deals", "promotions"], default=None)
    p_sh = sub.add_parser("sheets-headers", help="show a spreadsheet's headers and first rows")
    p_sh.add_argument("location")
    p_sh.add_argument("--tab", default=None)
    a = ap.parse_args(argv)

    init_db()
    with SessionLocal() as session:
        if a.cmd == "init-db":
            print("database ready")
        elif a.cmd == "import-dir":
            _print_results(import_directory(session, a.directory))
        elif a.cmd == "headset-import-dir":
            _print_results(import_headset_directory(session, a.directory))
        elif a.cmd == "headset-reconcile":
            rows = reconcile(session, a.store)
            missing = [r for r in rows if r["coverage"] == "missing"]
            bad = [r for r in rows if r["coverage"] == "mismatch"]
            print(f"{len(rows)} store-days from the feed: {len(rows) - len(missing) - len(bad)} tie to the cent, "
                  f"{len(bad)} mismatch, {len(missing)} have no product detail yet")
            for r in bad:
                print(f"  MISMATCH {r['store']} {r['date']}: lines {r['lines_revenue']} vs feed {r['feed_revenue']} "
                      f"(diff {r['revenue_diff']}); gross profit diff {r['gross_profit_diff']}")
            for r in missing:
                print(f"  MISSING  {r['store']} {r['date']}: feed revenue {r['feed_revenue']}, no product pull")
        elif a.cmd == "geocode-stores":
            import httpx

            with httpx.Client(timeout=30) as http:
                print(_json(geocode_stores(session, http, settings.geocoder_user_agent, only_codes=a.store).to_dict()))
        elif a.cmd == "events-sync":
            import httpx

            with httpx.Client(timeout=60) as http:
                report = events_sync(session, date.fromisoformat(a.start), date.fromisoformat(a.end), http, settings,
                                     include_calendar=not a.no_calendar)
            _print_results(report.results)
            print(_json({k: v for k, v in report.to_dict().items() if k != "results"}))
        elif a.cmd == "heartbeat-events":
            drafts = hb.heartbeat_events(session, a.min_gap or settings.heartbeat_gap_minutes, open_gap_after_minutes=a.open_after)
            r = upsert_events(session, drafts, "heartbeat")
            _print_results([r])
            for d in drafts:
                print(f"  {d.store_code} {d.event_type:12s} {d.severity:8s} {d.start_time:%Y-%m-%d %H:%M} -> {d.end_time:%H:%M}  {d.description}")
        elif a.cmd == "sheets-sync":
            import httpx

            with httpx.Client(timeout=60) as http:
                report = sheets_sync(session, http, settings, set(a.only) if a.only else None)
            _print_results(report.results)
            print(_json({k: v for k, v in report.to_dict().items() if k != "results"}))
        elif a.cmd == "sheets-headers":
            import httpx

            with httpx.Client(timeout=60) as http:
                print(_json(show_headers(http, a.location, a.tab)))
        elif a.cmd == "weather-sync":
            import httpx

            with httpx.Client(timeout=60) as http:
                report = weather_sync(
                    session, http, date.fromisoformat(a.start), date.fromisoformat(a.end), settings.geocoder_user_agent,
                    store_codes=a.store, include_alerts=not a.no_alerts,
                    forecast_url=settings.open_meteo_forecast_url, archive_url=settings.open_meteo_archive_url, nws_url=settings.nws_alerts_url,
                )
            _print_results(report.results)
            print(_json({k: v for k, v in report.to_dict().items() if k != "results"}))
        elif a.cmd == "heartbeat-status":
            for st in hb.status(session):
                print(f"{st.store:10s} {st.kind:8s} last seen {st.last_seen_at:%Y-%m-%d %H:%M}  silent {st.minutes_silent} min")
        elif a.cmd == "headset-sync":
            from app.integrations.headset.client import client_from_settings
            from app.integrations.headset.pull import headset_sync

            record = None if a.no_record else (a.record or settings.headset_data_dir)
            report = headset_sync(
                client_from_settings(), session, date.fromisoformat(a.start), date.fromisoformat(a.end),
                store_filter=a.stores, include_inventory=not a.no_inventory, full_catalog=a.full_catalog,
                include_products=not a.no_products, include_discounts=not a.no_discounts, record_dir=record,
            )
            _print_results(report.results)
            print(_json({k: v for k, v in report.to_dict().items() if k != "results"}))
        else:
            from app.api.routes import resolve_as_of
            from app.api.external_routes import resolve_store

            as_of = resolve_as_of(session, date.fromisoformat(a.as_of) if a.as_of else None)
            if a.cmd == "weekly":
                print(_json(weekly_comparison(session, as_of, a.store)))
            elif a.cmd == "discounts":
                print(_json(discount_report(session, week_containing(as_of), a.store)))
            elif a.cmd == "market":
                print(_json({"market": market_context(session, as_of), "pressure": competitor_pressure(session, week_containing(as_of))}))
            else:
                store = resolve_store(session, a.store)
                print(_json({
                    "events": event_findings(session, store, as_of),
                    "weather": {k: v for k, v in weather_intelligence(session, store, as_of).items() if k != "days"},
                    "forecast": proactive_forecast(session, store, as_of),
                }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
