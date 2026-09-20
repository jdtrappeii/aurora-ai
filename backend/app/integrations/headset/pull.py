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
    replayed: int = 0

    def to_dict(self) -> dict:
        return {
            "stores": self.stores,
            "calls": self.calls,
            "replayed_from_recordings": self.replayed,
            "results": [r.to_dict() for r in self.results],
            "warnings": self.warnings,
            "reconciliation_mismatches": [r for r in self.reconciliation if r["coverage"] == "mismatch"],
            "reconciliation_missing_count": sum(1 for r in self.reconciliation if r["coverage"] == "missing"),
            "reconciliation_missing_sample": [r["store"] + " " + r["date"] for r in self.reconciliation if r["coverage"] == "missing"][:10],
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

    def read(self, name: str) -> dict | None:
        """A previously recorded envelope, so an interrupted pull resumes instead of re-asking Headset."""
        if not self.directory:
            return None
        p = self.directory / f"{name}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return None


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
    detail_days: int | None = None,
    resume: bool = True,
    parallel: int = 1,
    source_factory=None,
) -> SyncReport:
    """detail_days limits the per-store-per-day product and discount pulls to the
    last N days of the range (store-day totals still cover the whole range).
    resume replays envelopes already recorded for this range instead of calling
    again. parallel > 1 pulls stores concurrently; each worker gets its own
    client from source_factory (the MCP session is not shareable)."""
    if end < start:
        raise ValueError("end precedes start")
    report = SyncReport()
    rec = Recorder(record_dir)

    def run(name: str, payload: dict) -> ImportResult:
        rec.write(name, payload)
        r = import_envelope(session, Envelope.from_dict(payload))
        report.results.append(r)
        return r

    def fetch(name: str, call, *args, **kwargs) -> tuple[dict, bool]:
        """(result, replayed): the recorded result when resuming, else a live call."""
        if resume:
            saved = rec.read(name)
            if saved is not None and "result" in saved:
                report.replayed += 1
                return saved["result"], True
        report.calls += 1
        return call(*args, **kwargs), False

    # 1. stores
    stores_result = source.get_stores()
    report.calls += 1
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
                report.calls += 1
            if result is None or result.get("hasMore"):
                # Walk the catalog one category at a time (the per-product page is
                # capped at ROW_LIMIT rows and a store carries thousands of SKUs).
                groups = source.get_inventory(storeNames=[name], groupBy="category", limit=ROW_LIMIT)
                report.calls += 1
                rows = []
                for g in groups.get("rows", []):
                    cat = g.get("category")
                    if not cat:
                        continue
                    kwargs = dict(storeNames=[name], categories=[cat], limit=ROW_LIMIT)
                    if not full_catalog:
                        kwargs["inStockOnly"] = True
                    part = source.get_inventory(**kwargs)
                    report.calls += 1
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
        sd_name = f"store_days__{chunk_start.isoformat()}__{chunk_end.isoformat()}"
        result, _ = fetch(sd_name, source.sales_trend,
                          grain="day", dimension="store", storeNames=names, soldDate=_looker_range(chunk_start, chunk_end),
                          measures=ALL_MEASURES, limit=ROW_LIMIT)
        if result.get("hasMore"):
            report.warnings.append(f"store_days {chunk_start}..{chunk_end}: truncated at {ROW_LIMIT} rows")
        run(sd_name, _envelope("store_days", result))
        chunk_start = chunk_end + timedelta(days=1)

    # 4 + 5. per store per day, inside the detail window
    detail_start = max(start, end - timedelta(days=detail_days - 1)) if detail_days else start
    detail_days_list = list(_days(detail_start, end))

    def pull_store(name: str, src: HeadsetSource) -> list[tuple[str, dict, str | None]]:
        """Every product/discount envelope for one store: (name, payload, warning)."""
        out: list[tuple[str, dict, str | None]] = []
        for day in detail_days_list:
            if include_products:
                pname = f"products__{_slug(name)}__{day.isoformat()}"
                result, _ = fetch(pname, src.sales_by_dimension,
                                  dimension="product", storeNames=[name], soldDate=day.isoformat(), measures=ALL_MEASURES, limit=ROW_LIMIT)
                warn = f"products {name} {day}: more than {ROW_LIMIT} SKUs sold, page truncated" if result.get("hasMore") else None
                out.append((pname, _envelope("products", result, store_name=name, sold_date=day), warn))
            if include_discounts:
                dname = f"discounts__{_slug(name)}__{day.isoformat()}"
                result, _ = fetch(dname, src.sales_by_dimension,
                                  dimension="discount_name", storeNames=[name], soldDate=day.isoformat(),
                                  measures=["total_revenue", "total_units", "total_discounts", "transaction_count"], limit=ROW_LIMIT)
                warn = f"discounts {name} {day}: more than {ROW_LIMIT} codes, page truncated" if result.get("hasMore") else None
                out.append((dname, _envelope("discounts", result, store_name=name, sold_date=day), warn))
        return out

    def absorb(batch):
        for ename, payload, warn in batch:
            if warn:
                report.warnings.append(warn)
            run(ename, payload)

    if detail_days_list and (include_products or include_discounts):
        if parallel > 1 and source_factory is not None:
            from concurrent.futures import ThreadPoolExecutor
            import threading
            local = threading.local()

            def worker(name: str):
                if not hasattr(local, "src"):
                    local.src = source_factory()
                return pull_store(name, local.src)

            with ThreadPoolExecutor(max_workers=parallel) as pool:
                for batch in pool.map(worker, names):   # ordered, so imports stay deterministic
                    absorb(batch)
        else:
            for name in names:
                absorb(pull_store(name, source))

    # 6. reconcile
    if include_products:
        report.reconciliation = [r for r in reconcile(session) if start.isoformat() <= r["date"] <= end.isoformat()]
    return report
