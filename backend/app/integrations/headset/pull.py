"""Pull a date range from Headset into Aurora.

    sync = headset_sync(source, session, start, end, store_filter="FL -", record_dir="data/headset")

Order of operations (each step is an envelope handed to app.importers.headset):
  1. stores            one call
  2. inventory         one call per store for in-stock SKUs; with full_catalog=True one
                       call per store per category so zero-stock SKUs get their
                       category / brand / vendor too (a sales row only carries sku + name)
  3. store_days        trend grain=day x store, chunked so rows stay under the limit
  4. products          one call per store per day  (the expensive part)
  5. discounts         one call per store per day
  6. reconcile         product lines vs store-day totals, reported not enforced

Re-running the same range is idempotent: every importer upserts on natural keys.
`record_dir` writes each envelope as JSON so the pull can be replayed with
`headset-import-dir` without touching Headset again. Keep that directory out
of git; it is your sales data.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from app.importers.csv_importer import ImportResult
from app.importers.headset import Envelope, import_envelope, reconcile
from app.integrations.headset.client import ALL_MEASURES, HeadsetSource

ROW_LIMIT = 1000


@dataclass
class SyncReport:
    stores: list[str] = field(default_factory=list)
    results: list[ImportResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reconciliation: list[dict] = field(default_factory=list)
    calls: int = 0

    def to_dict(self) -> dict:
        return {
            "stores": self.stores,
            "calls": self.calls,
            "results": [r.to_dict() for r in self.results],
            "warnings": self.warnings,
            "reconciliation_mismatches": [r for r in self.reconciliation if r["coverage"] == "mismatch"],
            "reconciliation_missing": [r["store"] + " " + r["date"] for r in self.reconciliation if r["coverage"] == "missing"],
            "reconciled_days": len(self.reconciliation),
        }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def _days(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _looker_range(start: date, end: date) -> str:
    """Looker's 'A to B' is inclusive of A and EXCLUSIVE of B, so B is end + 1 day."""
    return f"{start.isoformat()} to {(end + timedelta(days=1)).isoformat()}"


class Recorder:
    def __init__(self, directory: str | Path | None):
        self.directory = Path(directory) if directory else None
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, payload: dict) -> None:
        if not self.directory:
            return
        (self.directory / f"{name}.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _envelope(kind: str, result: dict, **meta) -> dict:
    return {"kind": kind, **{k: (v.isoformat() if isinstance(v, date) else v) for k, v in meta.items() if v is not None}, "result": result}


def headset_sync(
    source: HeadsetSource,
    session: Session,
    start: date,
    end: date,
    store_filter: str | None = None,
    include_inventory: bool = True,
    full_catalog: bool = False,
    include_products: bool = True,
    include_discounts: bool = True,
    record_dir: str | Path | None = None,
    snapshot_date: date | None = None,
) -> SyncReport:
    if end < start:
        raise ValueError("end precedes start")
    report = SyncReport()
    rec = Recorder(record_dir)

    def run(name: str, payload: dict) -> ImportResult:
        report.calls += 1
        rec.write(name, payload)
        r = import_envelope(session, Envelope.from_dict(payload))
        report.results.append(r)
        return r

    # 1. stores
    stores_result = source.get_stores()
    run("stores", _envelope("stores", stores_result))
    names = [re.sub(r"\s+", " ", s["name"]).strip() for s in stores_result.get("stores", [])]
    if store_filter:
        needle = store_filter.casefold()
        names = [n for n in names if needle in n.casefold()]
    report.stores = names
    if not names:
        report.warnings.append("no stores matched; nothing else pulled")
        return report

    # 2. inventory
    if include_inventory:
        snap = snapshot_date or date.today()
        for name in names:
            if full_catalog:
                result = None
            else:
                result = source.get_inventory(storeNames=[name], inStockOnly=True, limit=ROW_LIMIT)
            if result is None or result.get("hasMore"):
                # Walk the catalog one category at a time (the per-product page is
                # capped at ROW_LIMIT rows and a store carries thousands of SKUs).
                groups = source.get_inventory(storeNames=[name], groupBy="category", limit=ROW_LIMIT)
                rows = []
                for g in groups.get("rows", []):
                    cat = g.get("category")
                    if not cat:
                        continue
                    kwargs = dict(storeNames=[name], categories=[cat], limit=ROW_LIMIT)
                    if not full_catalog:
                        kwargs["inStockOnly"] = True
                    part = source.get_inventory(**kwargs)
                    if part.get("hasMore"):
                        report.warnings.append(f"inventory {name} / {cat}: more than {ROW_LIMIT} SKUs, page truncated")
                    rows.extend(part.get("rows", []))
                result = {"rows": rows, "hasMore": False}
            run(f"inventory__{_slug(name)}__{snap.isoformat()}", _envelope("inventory", result, store_name=name, snapshot_date=snap))

    # 3. store-day totals, chunked so stores x days <= ROW_LIMIT
    days_per_chunk = max(1, ROW_LIMIT // max(1, len(names)))
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + timedelta(days=days_per_chunk - 1))
        result = source.sales_trend(
            grain="day", dimension="store", storeNames=names, soldDate=_looker_range(chunk_start, chunk_end),
            measures=ALL_MEASURES, limit=ROW_LIMIT,
        )
        if result.get("hasMore"):
            report.warnings.append(f"store_days {chunk_start}..{chunk_end}: truncated at {ROW_LIMIT} rows")
        run(f"store_days__{chunk_start.isoformat()}__{chunk_end.isoformat()}", _envelope("store_days", result))
        chunk_start = chunk_end + timedelta(days=1)

    # 4 + 5. per store per day
    for name in names:
        for day in _days(start, end):
            if include_products:
                result = source.sales_by_dimension(
                    dimension="product", storeNames=[name], soldDate=day.isoformat(), measures=ALL_MEASURES, limit=ROW_LIMIT,
                )
                if result.get("hasMore"):
                    report.warnings.append(f"products {name} {day}: more than {ROW_LIMIT} SKUs sold, page truncated")
                run(f"products__{_slug(name)}__{day.isoformat()}", _envelope("products", result, store_name=name, sold_date=day))
            if include_discounts:
                result = source.sales_by_dimension(
                    dimension="discount_name", storeNames=[name], soldDate=day.isoformat(),
                    measures=["total_revenue", "total_units", "total_discounts", "transaction_count"], limit=ROW_LIMIT,
                )
                if result.get("hasMore"):
                    report.warnings.append(f"discounts {name} {day}: more than {ROW_LIMIT} codes, page truncated")
                run(f"discounts__{_slug(name)}__{day.isoformat()}", _envelope("discounts", result, store_name=name, sold_date=day))

    # 6. reconcile
    if include_products:
        report.reconciliation = [r for r in reconcile(session) if start.isoformat() <= r["date"] <= end.isoformat()]
    return report
