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
    for name in ("weekly", "external", "discounts"):
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
