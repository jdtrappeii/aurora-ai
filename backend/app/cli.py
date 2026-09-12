"""Aurora command line.

    python -m app.cli init-db
    python -m app.cli import-dir ../sample_data
    python -m app.cli weekly [--as-of 2026-09-11] [--store MAIN]
    python -m app.cli external [--as-of 2026-09-11] [--store MAIN]
"""
import argparse
import json
from datetime import date
from decimal import Decimal

from app.analytics.comparisons import weekly_comparison
from app.analytics.external import event_findings, proactive_forecast, weather_intelligence
from app.db import SessionLocal, init_db
from app.importers.csv_importer import import_directory


def _json(obj) -> str:
    return json.dumps(obj, indent=2, default=lambda o: str(o) if isinstance(o, Decimal) else o)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aurora")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    p_imp = sub.add_parser("import-dir")
    p_imp.add_argument("directory")
    for name in ("weekly", "external"):
        p = sub.add_parser(name)
        p.add_argument("--as-of", default=None)
        p.add_argument("--store", default=None)
    a = ap.parse_args(argv)

    init_db()
    with SessionLocal() as session:
        if a.cmd == "init-db":
            print("database ready")
        elif a.cmd == "import-dir":
            for r in import_directory(session, a.directory):
                print(f"{r.kind:16s} inserted={r.inserted:6d} updated={r.updated:6d} errors={len(r.errors)}")
                for e in r.errors[:10]:
                    print("   !", e)
        else:
            from app.api.routes import resolve_as_of
            from app.api.external_routes import resolve_store

            as_of = resolve_as_of(session, date.fromisoformat(a.as_of) if a.as_of else None)
            if a.cmd == "weekly":
                print(_json(weekly_comparison(session, as_of, a.store)))
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
